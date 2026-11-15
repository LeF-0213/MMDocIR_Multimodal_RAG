"""검색 실행의 순위를 정답 파일(qrels)과 비교해 페이지 단위 검색 점수와 질의별 실패 단계를 내는 진입점이다.

전체 위치: … → 검색·리랭킹(outputs/retrieval_*) → 평가〔현재〕(outputs/evaluation_retrieval_*)
입력: configs/evaluation_retrieval.json
  rankings.jsonl  검색 실행의 질의별 순위(채널별·결합·리랭킹)
  qrels.jsonl     한 줄에 {"query_id", "page_id", "relevance"}. relevance > 0이면 관련 페이지다.
평가 단위는 페이지다. 관련성은 이진(0/1)으로 본다. 정의(질의 하나, 관련 페이지 집합 R, 순위 목록 L):
  Recall@k = |R ∩ L[:k]| / |R|
  Hit@k    = R ∩ L[:k]가 비어 있지 않으면 1
  MRR@k    = 첫 관련 페이지의 순위가 r ≤ k이면 1/r, 없으면 0
  nDCG@k   = Σ_{i≤k, L[i]∈R} 1/log2(i+1) ÷ Σ_{i≤min(k,|R|)} 1/log2(i+1)
  모든 지표는 질의 평균이다. 관련 페이지가 없는 질의는 평균에서 빼고 개수를 따로 센다.
실패 단계(최종 순위 기준, 질의마다 첫 관련 페이지 하나로 판정):
  not_in_candidates   어느 채널도 k_retrieve 안에서 찾지 못함 → 파싱·청킹·임베딩·질의 표현 쪽
  outside_rerank      결합 순위가 k_rerank 밖이라 리랭커가 보지 못함 → 후보 수·결합 쪽
  ranked_below_final  리랭킹 창 안에 있었지만 최종 순위가 k_final 밖 → 리랭킹 쪽
  hit                 최종 순위가 k_final 이내
출력: 새 폴더 outputs/evaluation_retrieval_<UTC 시각>/ run.json, per_query.jsonl, summary.json
"""

# 표준 라이브러리: argparse는 CLI, json은 JSONL·출력, math는 로그, Counter는 실패 단계 집계,
# datetime은 UTC 실행 이름, Path는 경로다.
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path

# 내부 공통 모듈: 엄격한 JSON·배타적 저장·해시.
from common.files import file_hash, read_json, write_json

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "evaluation_retrieval.json"
CONFIG_SCHEMA = "project.evaluation_retrieval.v1"
CONFIG_FIELDS = frozenset({"schema_version", "retrieval_run", "qrels", "output_root", "k_values", "k_final"})
QREL_FIELDS = frozenset({"query_id", "page_id", "relevance"})
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
RANKINGS_FILE, RUN_FILE, PER_QUERY_FILE, SUMMARY_FILE = "rankings.jsonl", "run.json", "per_query.jsonl", "summary.json"


def recall_at(ranking, relevant, k):
    return len(relevant & set(ranking[:k])) / len(relevant)


def mrr_at(ranking, relevant, k):
    for rank, page in enumerate(ranking[:k], start=1):
        if page in relevant:
            return 1.0 / rank
    return 0.0


def ndcg_at(ranking, relevant, k):
    gain = sum(1.0 / math.log2(i + 1) for i, page in enumerate(ranking[:k], start=1) if page in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(k, len(relevant)) + 1))
    return gain / ideal


def first_rank(ranking, relevant):
    return next((rank for rank, page in enumerate(ranking, start=1) if page in relevant), None)


def system_rankings(row):
    """채점할 순위 목록들. 채널별, RRF 결합, 리랭킹(있을 때)."""
    systems = {f"channel:{name}": [p["page_id"] for p in pages] for name, pages in row["channels"].items()}
    systems["fused_rrf"] = [p["page_id"] for p in row["fused"]]
    if row["reranked"] is not None:
        systems["fused_rrf+rerank"] = [p["page_id"] for p in row["reranked"]]
    return systems


def failure_stage(row, relevant, k_rerank, k_final):
    final = [p["page_id"] for p in (row["reranked"] or row["fused"])]
    fused_rank = first_rank([p["page_id"] for p in row["fused"]], relevant)
    final_rank = first_rank(final, relevant)
    if fused_rank is None:
        return "not_in_candidates", None, None
    if final_rank is not None and final_rank <= k_final:
        return "hit", fused_rank, final_rank
    if row["reranked"] is not None and fused_rank > k_rerank:
        return "outside_rerank", fused_rank, final_rank
    return "ranked_below_final", fused_rank, final_rank


def main(argv=None):
    cli = argparse.ArgumentParser(description="페이지 단위 검색 점수")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="평가 설정 JSON")
    options = cli.parse_args(argv)
    try:
        path = options.config.resolve()
        config = read_json(path)
        if set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
            raise ValueError(f"평가 설정 필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
        base = path.parent
        retrieval_run = (base / config["retrieval_run"]).resolve()
        qrels_path = (base / config["qrels"]).resolve()
        retrieval_summary = read_json(retrieval_run / SUMMARY_FILE)
        if file_hash(retrieval_run / RANKINGS_FILE) != retrieval_summary["rankings_sha256"]:
            raise ValueError("rankings.jsonl 해시가 검색 요약과 다릅니다.")
        k_rerank = read_json(retrieval_run / RUN_FILE)["config"]["rerank"]["k_rerank"]
        relevant = {}
        for line in qrels_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                if set(item) != QREL_FIELDS:
                    raise ValueError(f"qrels 필드를 확인하세요: {sorted(QREL_FIELDS)}")
                if item["relevance"] > 0:
                    relevant.setdefault(item["query_id"], set()).add(item["page_id"])
        rows = [json.loads(line) for line in (retrieval_run / RANKINGS_FILE).read_text(encoding="utf-8").splitlines()]

        per_query, totals, stages = [], {}, Counter()
        without_relevant = []
        for row in rows:
            pages = relevant.get(row["query_id"])
            if not pages:
                without_relevant.append(row["query_id"])
                continue
            scores = {}
            for system, ranking in system_rankings(row).items():
                metrics = {f"recall@{k}": recall_at(ranking, pages, k) for k in config["k_values"]}
                metrics |= {f"hit@{k}": float(bool(pages & set(ranking[:k]))) for k in config["k_values"]}
                top = max(config["k_values"])
                metrics |= {f"mrr@{top}": mrr_at(ranking, pages, top), f"ndcg@{top}": ndcg_at(ranking, pages, top)}
                scores[system] = metrics
                for name, value in metrics.items():
                    totals.setdefault(system, {}).setdefault(name, []).append(value)
            stage, fused_rank, final_rank = failure_stage(row, pages, k_rerank, config["k_final"])
            stages[stage] += 1
            per_query.append({"query_id": row["query_id"], "relevant_pages": sorted(pages), "stage": stage,
                              "fused_rank": fused_rank, "final_rank": final_rank, "scores": scores})

        stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
        output = (base / config["output_root"]).resolve() / f"evaluation_retrieval_{stamp}"
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / RUN_FILE, {"schema_version": CONFIG_SCHEMA, "config": config, "config_path": str(path),
                                       "config_sha256": file_hash(path), "retrieval_run": str(retrieval_run),
                                       "rankings_sha256": retrieval_summary["rankings_sha256"],
                                       "qrels_path": str(qrels_path), "qrels_sha256": file_hash(qrels_path),
                                       "code_sha256": {Path(__file__).name: file_hash(Path(__file__))}})
        with (output / PER_QUERY_FILE).open("x", encoding="utf-8") as stream:
            for item in per_query:
                stream.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
        summary = {"schema_version": CONFIG_SCHEMA, "queries_scored": len(per_query),
                   "queries_without_relevant": without_relevant, "k_rerank": k_rerank, "k_final": config["k_final"],
                   "metrics": {system: {name: sum(v) / len(v) for name, v in values.items()}
                               for system, values in totals.items()},
                   "failure_stages": dict(stages), "output_dir": str(output)}
        write_json(output / SUMMARY_FILE, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"평가 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
