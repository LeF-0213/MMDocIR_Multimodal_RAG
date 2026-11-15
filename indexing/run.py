"""임베딩 실행의 벡터를 Qdrant 컬렉션에 넣고, 저장된 벡터와 검색 동작을 라벨 없이 확인하는 진입점이다.

전체 위치: PDF → 파싱 → 청킹 → 임베딩(outputs/embedding_*) → 인덱싱〔현재〕(Qdrant + outputs/indexing_*) → 검색·평가
입력: configs/indexing.json(임베딩 실행 폴더들, Qdrant 위치, 거리 함수, HNSW·검색 설정)
처리: 임베딩 실행 하나 → 컬렉션 하나. 컬렉션 이름은 임베딩 실행 폴더 이름이다. 이미 있으면 덮어쓰지 않고 실패한다.
확인(정답 라벨 없이 계산):
  - 개수: 컬렉션 포인트 수 = 레코드 수
  - 왕복: Qdrant에서 다시 읽은 벡터와 vectors.npy의 최대 절대 차이
  - 자기 검색: 각 벡터로 정확 검색했을 때 1위가 자기 자신인 비율. 중복 벡터가 있으면 1 미만일 수 있다
  - ANN 이웃 일치율: 같은 질의로 정확 검색과 ANN 검색의 상위 k 교집합 비율. 로컬 모드는 ANN을 만들지 않으므로 기록만 한다
출력: 새 폴더 outputs/indexing_<UTC 시각>/ run.json(설정·입력 해시·코드·환경), summary.json(컬렉션별 확인 결과)
실행 부작용: Qdrant 저장 위치(로컬 모드면 설정의 path 폴더)에 컬렉션 파일이 생긴다. 이 폴더는 저장소에 넣지 않는다.
--check는 설정과 입력 파일 존재만 확인하고 Qdrant에 쓰지 않는다.
"""

# 표준 라이브러리: argparse는 CLI, json은 JSONL·출력, platform·sys는 환경 기록, datetime은 UTC 실행 이름,
# importlib.metadata는 설치 버전, Path는 경로, perf_counter는 경과 시간이다.
import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

# NumPy: vectors.npy 읽기와 왕복 차이 계산.
import numpy as np
# qdrant-client 모델: 컬렉션 벡터 설정(VectorParams·Distance), HNSW 설정, 포인트, 검색 파라미터.
from qdrant_client import models

# 내부 공통 모듈: 엄격한 JSON·배타적 저장·해시.
import common
from common.files import file_hash, read_json, write_json

# 내부 인덱싱 모듈: 연결·이름·포인트 ID 규칙.
from .store import collection_name, connect, point_id, record_id

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "indexing.json"
CONFIG_SCHEMA = "project.indexing.v1"
CONFIG_FIELDS = frozenset({"schema_version", "embedding_runs", "output_root", "qdrant", "distance", "hnsw",
                           "upsert_batch_size", "check"})
HNSW_FIELDS = frozenset({"m", "ef_construct", "full_scan_threshold"})
CHECK_FIELDS = frozenset({"top_k", "hnsw_ef"})
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
RECORDS_FILE, VECTORS_FILE, SUMMARY_FILE, RUN_FILE = "records.jsonl", "vectors.npy", "summary.json", "run.json"
RECORDED_PACKAGES = ("qdrant-client", "numpy")
# 레코드에서 페이로드로 옮기지 않는 필드. 벡터 행 번호는 임베딩 실행 안에서만 의미가 있어 따로 이름을 붙인다.
PAYLOAD_EXCLUDED = frozenset({"vector_index"})


def load_config(path):
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
        raise ValueError(f"인덱싱 설정의 스키마·필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
    if set(config["hnsw"]) != HNSW_FIELDS or set(config["check"]) != CHECK_FIELDS:
        raise ValueError("hnsw·check 필드를 확인하세요.")
    if config["distance"] not in models.Distance.__members__:
        raise ValueError(f"distance는 {sorted(models.Distance.__members__)} 중 하나여야 합니다.")
    base = path.parent
    paths = {"output_root": (base / config["output_root"]).resolve(),
             "embedding_runs": [(base / p).resolve() for p in config["embedding_runs"]], "base": base}
    return config, {"path": str(path), "sha256": file_hash(path), "paths": paths}


def environment():
    packages = {}
    for name in RECORDED_PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def load_embedding_run(run):
    """임베딩 실행의 요약에 적힌 해시와 실제 파일이 같을 때만 읽는다."""
    summary = read_json(run / SUMMARY_FILE)
    for key, name in (("vectors_sha256", VECTORS_FILE), ("records_sha256", RECORDS_FILE)):
        if file_hash(run / name) != summary[key]:
            raise ValueError(f"{name} 해시가 임베딩 요약과 다릅니다: {run}")
    records = [json.loads(line) for line in (run / RECORDS_FILE).read_text(encoding="utf-8").splitlines()]
    vectors = np.load(run / VECTORS_FILE, allow_pickle=False)
    if vectors.shape[0] != len(records) or any(r["vector_index"] != i for i, r in enumerate(records)):
        raise ValueError(f"벡터 행과 레코드 순서가 맞지 않습니다: {run}")
    return summary, records, vectors


def search_ids(client, name, vector, limit, params):
    hits = client.query_points(name, query=vector.tolist(), limit=limit, search_params=params).points
    return [hit.id for hit in hits]


def index_run(client, store, run, config):
    started = perf_counter()
    summary, records, vectors = load_embedding_run(run)
    name = collection_name(run)
    if client.collection_exists(name):
        raise ValueError(f"컬렉션이 이미 있습니다. 덮어쓰지 않습니다: {name}")
    client.create_collection(
        name, vectors_config=models.VectorParams(size=int(vectors.shape[1]),
                                                 distance=models.Distance[config["distance"]]),
        hnsw_config=models.HnswConfigDiff(**config["hnsw"]))
    ids = [point_id(record_id(r)) for r in records]
    if len(set(ids)) != len(ids):
        raise ValueError(f"포인트 ID가 중복됩니다: {run}")
    batch = config["upsert_batch_size"]
    for start in range(0, len(records), batch):
        points = [models.PointStruct(id=ids[i], vector=vectors[i].tolist(),
                                     payload={k: v for k, v in records[i].items() if k not in PAYLOAD_EXCLUDED}
                                     | {"vector_index": i, "embedding_run": run.name})
                  for i in range(start, min(start + batch, len(records)))]
        client.upsert(name, points=points, wait=True)
    upsert_seconds = perf_counter() - started

    count = client.count(name, exact=True).count
    stored = client.retrieve(name, ids=ids, with_vectors=True)
    by_id = {str(p.id): np.asarray(p.vector, dtype=np.float32) for p in stored}
    roundtrip = max(float(np.max(np.abs(by_id[i] - vectors[k]))) for k, i in enumerate(ids))

    top_k = min(config["check"]["top_k"], len(ids))
    exact = models.SearchParams(exact=True)
    ann = models.SearchParams(hnsw_ef=config["check"]["hnsw_ef"], exact=False)
    self_top1, overlap = 0, []
    check_started = perf_counter()
    for k, vector in enumerate(vectors):
        exact_ids = search_ids(client, name, vector, top_k, exact)
        ann_ids = search_ids(client, name, vector, top_k, ann)
        self_top1 += exact_ids[0] == ids[k]
        overlap.append(len(set(exact_ids) & set(ann_ids)) / top_k)
    info = client.get_collection(name)
    return {"collection": name, "embedding_run": str(run), "channel": summary["channel"],
            "embedding_summary_sha256": file_hash(run / SUMMARY_FILE),
            "points": count, "records": len(records), "dimension": int(vectors.shape[1]),
            "distance": config["distance"], "roundtrip_max_abs_diff": roundtrip,
            "self_retrieval_top1": f"{self_top1}/{len(ids)}",
            "ann_neighbor_agreement": {"top_k": top_k, "hnsw_ef": config["check"]["hnsw_ef"],
                                       "mean": float(np.mean(overlap)), "min": float(np.min(overlap)),
                                       # 로컬 모드는 HNSW를 만들지 않아 ANN 요청도 전수 비교로 처리된다.
                                       "ann_index_used": store["mode"] == "server" and info.indexed_vectors_count > 0},
            "indexed_vectors_count": info.indexed_vectors_count,
            "status": "completed" if count == len(records) else "count_mismatch",
            "upsert_seconds": upsert_seconds, "check_seconds": perf_counter() - check_started}


def main(argv=None):
    cli = argparse.ArgumentParser(description="임베딩 벡터의 Qdrant 인덱싱")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="인덱싱 설정 JSON")
    cli.add_argument("--embedding-run", type=Path, action="append",
                     help="설정의 embedding_runs 대신 인덱싱할 임베딩 실행 폴더. 여러 번 지정 가능")
    cli.add_argument("--check", action="store_true", help="설정·입력 확인만. Qdrant에 쓰지 않음")
    options = cli.parse_args(argv)
    try:
        config, context = load_config(options.config)
        runs = [p.resolve() for p in options.embedding_run] if options.embedding_run else context["paths"]["embedding_runs"]
        for run in runs:
            if not (run / SUMMARY_FILE).is_file():
                raise ValueError(f"완료된 임베딩 실행 폴더가 아닙니다: {run}")
        if options.check:
            print(json.dumps({"status": "configuration_valid", "embedding_runs": [str(r) for r in runs],
                              "qdrant": config["qdrant"]}, ensure_ascii=False, indent=2))
            return 0
        started = perf_counter()
        client, store = connect(config["qdrant"], context["paths"]["base"])
        stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
        output = context["paths"]["output_root"] / f"indexing_{stamp}"
        output.mkdir(parents=True, exist_ok=False)
        root = Path(__file__).resolve().parents[1]
        code = sorted(Path(__file__).parent.glob("*.py")) + sorted(Path(common.__file__).parent.glob("*.py"))
        write_json(output / RUN_FILE, {
            "schema_version": CONFIG_SCHEMA, "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "config": config | {"embedding_runs": [str(r) for r in runs]}, "config_path": context["path"],
            "config_sha256": context["sha256"], "store": store,
            "code_sha256": {p.relative_to(root).as_posix(): file_hash(p) for p in code},
            "environment": environment()})
        collections = [index_run(client, store, run, config) for run in runs]
        client.close()
        summary = {"schema_version": CONFIG_SCHEMA,
                   "status": "completed" if all(c["status"] == "completed" for c in collections) else "completed_with_problems",
                   "finished_at_utc": datetime.now(timezone.utc).isoformat(),
                   "elapsed_seconds": perf_counter() - started, "store": store, "output_dir": str(output),
                   "collections": collections}
        write_json(output / SUMMARY_FILE, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["status"] == "completed" else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"인덱싱 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
