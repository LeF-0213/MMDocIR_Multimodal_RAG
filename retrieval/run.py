"""질의 파일을 읽어 채널별 Qdrant 검색 → 페이지 집계 → RRF 결합 → 리랭킹을 실행하고 순위를 저장하는 진입점이다.

전체 위치: … → 인덱싱(Qdrant, outputs/indexing_*) → 검색·리랭킹〔현재〕(outputs/retrieval_*) → 평가(evaluation)
입력: configs/retrieval.json
  queries.jsonl  한 줄에 {"query_id", "document_name", "question"}. 정답 페이지 같은 평가 라벨은 이 파일에 두지 않는다.
                 document_name은 질문이 걸린 문서다. MMDocIR처럼 문서 하나에 대한 질문이면 평가 라벨이 아니라 과제 입력이다.
  indexing 실행  컬렉션 이름과 그 컬렉션을 만든 임베딩 실행. 질의는 그 임베딩 실행과 같은 모델·지시문으로 인코딩한다.
처리:
  1. 채널(컬렉션)마다 질의를 인코딩하고, 질문의 문서에 속한 레코드만 남기는 payload 필터를 걸어
     상위 k_retrieve개를 검색한다(정확 검색 여부는 설정). 다른 문서의 페이지는 후보에 들어오지 않는다.
     모델을 하나씩 올리고 내린다. 16GB 환경에서 2B 모델 둘을 동시에 올리지 않기 위해서다.
  2. 청크 결과는 페이지별 최고 점수로 모은다. 페이지 결과는 그대로 쓴다.
  3. 채널별 페이지 순위를 RRF로 결합한다.
  4. 결합 상위 k_rerank 페이지를 리랭커로 다시 점수 매긴다. 나머지는 결합 순서대로 뒤에 붙인다.
출력: 새 폴더 outputs/retrieval_<UTC 시각>/
  run.json         설정·입력 해시·코드·환경
  rankings.jsonl   질의마다 채널별·결합·리랭킹 순위와 점수
  summary.json     시간·호출 비용·모델 기록
실행 부작용: 텍스트 채널은 질의 수만큼 원격 호출과 과금, 이미지 채널·리랭커는 로컬 모델 로딩과 큰 메모리.
--check는 설정·입력 파일만 확인하고 모델을 부르지 않는다.
"""

# 표준 라이브러리: argparse는 CLI, Counter는 문서별 질의 수, gc는 모델 해제 뒤 메모리 회수, json은 JSONL·출력,
# datetime은 UTC 실행 이름, Path는 경로, perf_counter는 경과 시간이다.
import argparse
from collections import Counter
from datetime import datetime, timezone
import gc
import json
from pathlib import Path
from time import perf_counter

# qdrant-client 모델: 검색 파라미터(정확 검색 여부·HNSW 탐색량)와 payload 필터(문서 범위 제한).
from qdrant_client import models
# Pillow: 리랭커에 넘길 페이지 이미지를 연다.
from PIL import Image

# 내부 공통 모듈: 엄격한 JSON·배타적 저장·해시.
from common.files import file_hash, read_json, write_json
# 내부 임베딩 모듈: 질의 인코더(문서를 인코딩한 것과 같은 어댑터), 환경 기록, 페이지 이미지 경로 확인.
from embedding.encoders import OpenRouterTextEncoder, QwenVLEncoder
from embedding.run import environment, page_image_inputs
# 내부 인덱싱 모듈: Qdrant 연결 규칙.
from indexing.store import connect
# 내부 파싱 모듈: 선정 목록 검증. 질문의 문서명 확인과 리랭킹 이미지 경로 확인에 함께 쓴다.
from parsing.pdf_source import load_selection

# 내부 검색 모듈: 페이지 집계·RRF, 리랭커.
from .fusion import pages_by_max, reciprocal_rank_fusion
from .rerank import QwenVLReranker

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "retrieval.json"
CONFIG_SCHEMA = "project.retrieval.v1"
CONFIG_FIELDS = frozenset({"schema_version", "indexing_run", "queries", "selection", "output_root", "qdrant",
                           "collections", "k_retrieve", "search", "fusion", "rerank"})
SEARCH_FIELDS = frozenset({"exact", "hnsw_ef"})
FUSION_FIELDS = frozenset({"method", "k"})
RERANK_FIELDS = frozenset({"enabled", "k_rerank", "page_image_run", "model"})
RERANK_MODEL_FIELDS = frozenset({"repo_id", "revision", "device", "dtype", "cpu_threads", "instruction"})
QUERY_FIELDS = frozenset({"query_id", "document_name", "question"})
# 문서 필터가 보는 payload 필드. 임베딩 레코드(페이지·청크 모두)가 파싱 단계의 문서명을 이 이름으로 싣는다.
DOCUMENT_FIELD = "document_name"
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
RUN_FILE, RANKINGS_FILE, SUMMARY_FILE = "run.json", "rankings.jsonl", "summary.json"


def load_config(path):
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
        raise ValueError(f"검색 설정의 스키마·필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
    if (set(config["search"]) != SEARCH_FIELDS or set(config["fusion"]) != FUSION_FIELDS
            or set(config["rerank"]) != RERANK_FIELDS or set(config["rerank"]["model"]) != RERANK_MODEL_FIELDS):
        raise ValueError("search·fusion·rerank 필드를 확인하세요.")
    if config["fusion"]["method"] != "rrf":
        raise ValueError("현재 지원하는 결합 방식은 rrf뿐입니다.")
    for key, value in (("k_retrieve", config["k_retrieve"]), ("k_rerank", config["rerank"]["k_rerank"])):
        if type(value) is not int or value < 1:
            raise ValueError(f"{key}는 1 이상의 정수여야 합니다.")
    base = path.parent
    paths = {key: (base / config[key]).resolve() for key in ("indexing_run", "queries", "selection", "output_root")}
    paths["page_image_run"] = (base / config["rerank"]["page_image_run"]).resolve()
    paths["base"] = base
    return config, {"path": str(path), "sha256": file_hash(path), "paths": paths}


def load_queries(path, documents):
    """질의만 읽는다. 평가 라벨 필드가 섞여 있거나 선정 목록에 없는 문서를 가리키면 거부한다.

    documents: 선정 목록의 문서명 집합. 목록 밖 문서는 인덱스에 없어서, 필터를 걸면 후보가 0개가 되고
    실패 원인이 검색 품질처럼 보이게 된다. 그래서 검색 전에 막는다.
    """
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if set(row) != QUERY_FIELDS:
            raise ValueError(f"질의 파일에는 {sorted(QUERY_FIELDS)}만 둡니다: {sorted(row)}")
        if row["document_name"] not in documents:
            raise ValueError(f"선정 목록에 없는 문서입니다: {row['query_id']} → {row['document_name']}")
    if len({r["query_id"] for r in rows}) != len(rows):
        raise ValueError("query_id가 중복됩니다.")
    return rows


def channel_inputs(indexing_run, names):
    """인덱싱 요약에서 컬렉션별 임베딩 실행과 채널을 찾고, 그 임베딩 설정의 모델 항목을 가져온다."""
    summary = read_json(indexing_run / SUMMARY_FILE)
    by_name = {c["collection"]: c for c in summary["collections"]}
    channels = []
    for name in names:
        if name not in by_name:
            raise ValueError(f"인덱싱 실행에 없는 컬렉션입니다: {name}")
        item = by_name[name]
        embedding_run = Path(item["embedding_run"])
        if file_hash(embedding_run / SUMMARY_FILE) != item["embedding_summary_sha256"]:
            raise ValueError(f"인덱싱 이후 임베딩 요약이 바뀌었습니다: {embedding_run}")
        embedding_config = read_json(embedding_run / RUN_FILE)["config"]
        channels.append({"collection": name, "channel": item["channel"], "embedding_run": str(embedding_run),
                         "model": embedding_config[item["channel"]]["model"]})
    return channels, {"indexing_summary_sha256": file_hash(indexing_run / SUMMARY_FILE), "store": summary["store"]}


def document_filter(document_name):
    """한 문서의 레코드만 남기는 Qdrant 필터. must 안의 조건을 모두 만족하는 포인트만 검색 대상이 된다.

    MatchValue는 payload 값이 정확히 같은지 본다(부분 일치 아님). 필터는 유사도 계산 전에 후보를 줄이므로
    상위 k_retrieve개가 모두 그 문서에서 나온다. 검색 뒤에 거르면 다른 문서가 상위를 차지할 때 후보가 모자란다.
    """
    return models.Filter(must=[models.FieldCondition(key=DOCUMENT_FIELD,
                                                     match=models.MatchValue(value=document_name))])


def search_channel(client, channel, queries, config):
    """채널 하나의 질의를 인코딩·검색해 질의별 페이지 순위를 돌려준다. 끝나면 모델을 해제한다."""
    started = perf_counter()
    encoder = (OpenRouterTextEncoder if channel["channel"] == "text" else QwenVLEncoder)(channel["model"])
    vectors, _ = encoder.encode_queries([q["question"] for q in queries])
    encode_seconds = perf_counter() - started
    params = models.SearchParams(exact=config["search"]["exact"], hnsw_ef=config["search"]["hnsw_ef"])
    rankings = []
    search_started = perf_counter()
    for query, vector in zip(queries, vectors):
        hits = client.query_points(channel["collection"], query=vector.tolist(), limit=config["k_retrieve"],
                                   query_filter=document_filter(query["document_name"]),
                                   search_params=params, with_payload=True).points
        rankings.append(pages_by_max([(h.payload["page_id"], float(h.score),
                                       h.payload.get("chunk_id") or h.payload["page_id"]) for h in hits]))
    record = encoder.record() | {"query_encode_seconds": encode_seconds,
                                 "search_seconds": perf_counter() - search_started}
    del encoder
    gc.collect()
    return rankings, record


def main(argv=None):
    cli = argparse.ArgumentParser(description="채널별 검색·RRF 결합·리랭킹")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="검색 설정 JSON")
    cli.add_argument("--check", action="store_true", help="설정·입력 확인만. 모델 호출 없음")
    options = cli.parse_args(argv)
    try:
        config, context = load_config(options.config)
        paths = context["paths"]
        selection = load_selection(paths["selection"])
        queries = load_queries(paths["queries"], {d["document_name"] for d in selection["documents"]})
        queries_per_document = dict(Counter(q["document_name"] for q in queries))
        channels, indexing = channel_inputs(paths["indexing_run"], config["collections"])
        if options.check:
            print(json.dumps({"status": "configuration_valid", "queries": len(queries),
                              "queries_per_document": queries_per_document,
                              "channels": [{k: c[k] for k in ("collection", "channel")} for c in channels]},
                             ensure_ascii=False, indent=2))
            return 0
        started = perf_counter()
        stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
        output = paths["output_root"] / f"retrieval_{stamp}"
        output.mkdir(parents=True, exist_ok=False)
        root = Path(__file__).resolve().parents[1]
        code = [p for folder in ("retrieval", "embedding", "indexing", "common") for p in sorted((root / folder).glob("*.py"))]
        write_json(output / RUN_FILE, {
            "schema_version": CONFIG_SCHEMA, "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "config": config, "config_path": context["path"], "config_sha256": context["sha256"],
            "queries_path": str(paths["queries"]), "queries_sha256": file_hash(paths["queries"]),
            "indexing_run": str(paths["indexing_run"]), **indexing,
            "channels": [{k: c[k] for k in ("collection", "channel", "embedding_run")} for c in channels],
            "code_sha256": {p.relative_to(root).as_posix(): file_hash(p) for p in code},
            "environment": environment()})

        client, _ = connect(config["qdrant"], paths["base"])
        per_channel, channel_records = {}, {}
        for channel in channels:
            per_channel[channel["collection"]], channel_records[channel["collection"]] = search_channel(
                client, channel, queries, config)
        client.close()

        fused = [reciprocal_rank_fusion([[p["page_id"] for p in per_channel[c["collection"]][i]] for c in channels],
                                        config["fusion"]["k"]) for i in range(len(queries))]
        reranked, rerank_record = [None] * len(queries), None
        if config["rerank"]["enabled"]:
            records, image_paths, _ = page_image_inputs(paths["page_image_run"], selection)
            image_by_page = {r["page_id"]: p for r, p in zip(records, image_paths)}
            reranker = QwenVLReranker(config["rerank"]["model"])
            for i, query in enumerate(queries):
                window = [page for page, _ in fused[i][:config["rerank"]["k_rerank"]]]
                images = []
                for page in window:
                    with Image.open(image_by_page[page]) as image:
                        images.append(image.convert("RGB"))
                scores = reranker.score(query["question"], images)
                order = sorted(zip(window, scores, range(1, len(window) + 1)), key=lambda x: (-x[1], x[2]))
                reranked[i] = ([{"page_id": p, "rerank_score": s, "fused_rank": r} for p, s, r in order]
                               + [{"page_id": p, "rerank_score": None, "fused_rank": r}
                                  for r, (p, _) in enumerate(fused[i], start=1) if r > len(window)])
                print(json.dumps({"reranked": i + 1, "of": len(queries)}), flush=True)
            rerank_record = reranker.record()
            del reranker
            gc.collect()

        with (output / RANKINGS_FILE).open("x", encoding="utf-8") as stream:
            for i, query in enumerate(queries):
                row = {"query_id": query["query_id"], "document_name": query["document_name"],
                       "question": query["question"],
                       "channels": {name: rankings[i] for name, rankings in per_channel.items()},
                       "fused": [{"page_id": p, "score": s} for p, s in fused[i]], "reranked": reranked[i]}
                stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        summary = {"schema_version": CONFIG_SCHEMA, "status": "completed", "queries": len(queries),
                   "queries_per_document": queries_per_document, "search_scope": f"payload {DOCUMENT_FIELD} = 질문의 문서",
                   "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                   "elapsed_seconds": perf_counter() - started, "output_dir": str(output),
                   "rankings_sha256": file_hash(output / RANKINGS_FILE),
                   "channels": channel_records, "rerank": rerank_record}
        write_json(output / SUMMARY_FILE, summary)
        print(json.dumps({k: summary[k] for k in ("status", "queries", "output_dir", "elapsed_seconds")},
                         ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        cli.exit(2, f"검색 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
