"""청킹 결과(텍스트)와 파싱 페이지 이미지(이미지)를 임베딩해 새 임베딩 실행 폴더에 저장하는 진입점이다.

전체 위치: PDF → 파싱 → 청킹(outputs/chunking_*) → 임베딩〔현재〕(outputs/embedding_*) → 인덱싱 → 검색·평가
입력: configs/embedding.json
  text   채널: 청킹 실행 폴더들의 chunks.jsonl. 청크 하나가 벡터 하나다. 모델은 OpenRouter 원격 모델.
  image  채널: 파싱 실행 하나의 페이지 이미지(PNG). 페이지 하나가 벡터 하나다. 모델은 로컬 Qwen3-VL-Embedding.
  두 채널은 모델과 표현 공간이 다르므로 벡터를 섞지 않고 실행 폴더·컬렉션을 따로 둔다.
출력: 입력마다 새 폴더 outputs/embedding_<채널>_<입력 이름>_<UTC 시각>/
  run.json        설정·입력 해시·코드 해시·환경
  records.jsonl   벡터 행 순서대로 원본 ID(청크·페이지·문서)와 좌표 없는 메타데이터
  vectors.npy     float32 (행 수, 차원). 행마다 L2 정규화
  summary.json    개수·차원·정규화 전 노름 분포·시간·호출 비용
실행 부작용: text는 네트워크 호출과 과금, image는 가중치 다운로드(처음 한 번)와 큰 메모리(fp32 2B 모델에서 약 13GB 관측)가 생긴다.
--check는 설정·입력 파일·API 키 환경 변수의 존재만 확인하고 모델 호출·파일 생성을 하지 않는다.
"""

# 표준 라이브러리: argparse는 CLI, json은 JSONL·출력, platform·sys는 환경 기록, datetime은 UTC 실행 이름,
# importlib.metadata는 설치 버전, os는 키 존재 확인, Path는 경로, perf_counter는 경과 시간이다.
import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import platform
import sys
from time import perf_counter

# NumPy: 벡터 저장(.npy)과 노름 분포 계산.
import numpy as np
# Pillow: 페이지 PNG를 열어 모델에 넘긴다. 파싱 단계가 저장한 이미지를 다시 렌더링하지 않고 그대로 쓴다.
from PIL import Image

# 내부 공통 모듈: 엄격한 JSON 읽기·배타적 저장·해시·입력 폴더 경계 검사.
import common
from common.files import digest, file_hash, inside, read_json, write_json
# 내부 파싱 모듈: 선정 목록 검증만 가져온다. 파싱 내부 구현에는 기대지 않는다.
from parsing.pdf_source import load_selection

# 내부 임베딩 모듈: 모델 어댑터와 정규화.
from .encoders import OpenRouterTextEncoder, QwenVLEncoder

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "embedding.json"
# 아래는 설정·출력 형식의 정의다.
CONFIG_SCHEMA = "project.embedding.v1"
CONFIG_FIELDS = frozenset({"schema_version", "selection", "output_root", "channels", "text", "image"})
TEXT_FIELDS = frozenset({"chunking_runs", "model"})
TEXT_MODEL_FIELDS = frozenset({"model_id", "api_base", "api_key_env", "batch_size", "timeout_seconds",
                               "max_retries", "retry_backoff_seconds", "query_instruction"})
IMAGE_FIELDS = frozenset({"page_image_run", "model"})
IMAGE_MODEL_FIELDS = frozenset({"repo_id", "revision", "device", "dtype", "cpu_threads",
                                "document_prompt", "query_prompt"})
CHANNELS = ("text", "image")
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
RUN_FILE, RECORDS_FILE, VECTORS_FILE, SUMMARY_FILE = "run.json", "records.jsonl", "vectors.npy", "summary.json"
CHUNKS_FILE, INPUT_RUN_FILE = "chunks.jsonl", "run.json"
# 청크에서 검색 결과 추적에 필요한 필드만 records에 옮긴다. 본문은 청킹 실행에 이미 있으므로 복사하지 않는다.
CHUNK_FIELDS = ("chunk_id", "kind", "document_id", "document_name", "page_id", "page_number",
                "token_count", "heading", "flags")
RECORDED_PACKAGES = ("numpy", "pillow", "torch", "transformers", "sentence-transformers")


def load_config(path):
    """설정 오타·잘못된 타입을 처리 전에 거부하고 상대 경로를 설정 파일 기준으로 푼다."""
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
        raise ValueError(f"임베딩 설정의 스키마·필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
    if not set(config["channels"]) <= set(CHANNELS) or not config["channels"]:
        raise ValueError(f"channels는 {CHANNELS} 중에서 고르세요.")
    for key, fields, model_fields in (("text", TEXT_FIELDS, TEXT_MODEL_FIELDS),
                                      ("image", IMAGE_FIELDS, IMAGE_MODEL_FIELDS)):
        if set(config[key]) != fields or set(config[key]["model"]) != model_fields:
            raise ValueError(f"{key} 설정 필드를 확인하세요: {sorted(fields)}, model {sorted(model_fields)}")
    for key in ("batch_size", "max_retries"):
        if type(config["text"]["model"][key]) is not int or config["text"]["model"][key] < 0:
            raise ValueError(f"text.model.{key}는 0 이상의 정수여야 합니다.")
    if type(config["image"]["model"]["cpu_threads"]) is not int or config["image"]["model"]["cpu_threads"] < 1:
        raise ValueError("image.model.cpu_threads는 1 이상의 정수여야 합니다.")
    base = path.parent
    paths = {"selection": (base / config["selection"]).resolve(),
             "output_root": (base / config["output_root"]).resolve(),
             "chunking_runs": [(base / p).resolve() for p in config["text"]["chunking_runs"]],
             "page_image_run": (base / config["image"]["page_image_run"]).resolve()}
    return config, {"path": str(path), "sha256": file_hash(path), "paths": paths}


def code_hashes():
    """벡터에 영향을 주는 코드의 해시다."""
    root = Path(__file__).resolve().parents[1]
    files = sorted(Path(__file__).parent.glob("*.py")) + sorted(Path(common.__file__).parent.glob("*.py"))
    return {p.relative_to(root).as_posix(): file_hash(p) for p in files}


def environment():
    packages = {}
    for name in RECORDED_PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def text_inputs(chunking_run):
    """청킹 실행에서 (records, texts, 입력 기록)을 만든다. 입력 파일 해시로 나중 단계가 같은 입력인지 확인한다."""
    run = read_json(chunking_run / INPUT_RUN_FILE)
    rows = [json.loads(line) for line in (chunking_run / CHUNKS_FILE).read_text(encoding="utf-8").splitlines()]
    records = [{key: row[key] for key in CHUNK_FIELDS} for row in rows]
    texts = [row["text"] for row in rows]
    if len({r["chunk_id"] for r in records}) != len(records):
        raise ValueError(f"청크 ID가 중복됩니다: {chunking_run}")
    source = {"chunking_run": str(chunking_run), "input_parser": run["input_parser"],
              "chunks_sha256": file_hash(chunking_run / CHUNKS_FILE),
              "run_json_sha256": file_hash(chunking_run / INPUT_RUN_FILE)}
    return records, texts, source, run["input_parser"]


def page_image_inputs(parsing_run, selection):
    """선정 페이지의 렌더링 이미지 경로·해시를 모은다. 기록된 해시와 다르면 거부한다."""
    records, paths, missing = [], [], []
    for item in selection["documents"]:
        doc_id = digest(item["document_name"].encode("utf-8"))
        for number in sorted(item["page_numbers"]):
            page_json = parsing_run / "documents" / doc_id / f"page_{number:05d}.json"
            if not page_json.is_file():
                missing.append({"document_name": item["document_name"], "page_number": number})
                continue
            page = read_json(page_json)
            image = inside(parsing_run, page["page_image"]["path"])
            if file_hash(image) != page["page_image"]["sha256"]:
                raise ValueError(f"페이지 이미지 해시가 기록과 다릅니다: {image}")
            render = page["render"]
            records.append({"page_id": page["page_id"], "document_id": page["document_id"],
                            "document_name": page["document_name"], "page_number": page["page_number"],
                            "page_image": page["page_image"]["path"], "page_image_sha256": page["page_image"]["sha256"],
                            "dpi": render["dpi"], "width": render["width"], "height": render["height"]})
            paths.append(image)
    source = {"parsing_run": str(parsing_run), "run_json_sha256": file_hash(parsing_run / INPUT_RUN_FILE),
              "missing_pages": missing}
    return records, paths, source


def norm_summary(norms):
    return {"min": float(np.min(norms)), "median": float(np.median(norms)), "max": float(np.max(norms))}


def save_run(output_root, name, run_record, records, vectors, summary):
    """결과 폴더를 새로 만들고 run → records·vectors → summary 순서로 쓴다. 기존 폴더는 덮어쓰지 않는다."""
    stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
    output = output_root / f"embedding_{name}_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / RUN_FILE, run_record)
    with (output / RECORDS_FILE).open("x", encoding="utf-8") as stream:
        for index, record in enumerate(records):
            stream.write(json.dumps({"vector_index": index, **record}, ensure_ascii=False, allow_nan=False) + "\n")
    with (output / VECTORS_FILE).open("xb") as stream:
        np.save(stream, vectors.astype(np.float32), allow_pickle=False)
    summary = {**summary, "output_dir": str(output), "vectors_sha256": file_hash(output / VECTORS_FILE),
               "records_sha256": file_hash(output / RECORDS_FILE)}
    write_json(output / SUMMARY_FILE, summary)
    return summary


def base_record(config, context, channel):
    return {"schema_version": CONFIG_SCHEMA, "channel": channel,
            "started_at_utc": datetime.now(timezone.utc).isoformat(),
            "config": config, "config_path": context["path"], "config_sha256": context["sha256"],
            "code_sha256": code_hashes(), "environment": environment()}


def embed_text(config, context):
    results = []
    encoder = OpenRouterTextEncoder(config["text"]["model"])
    for chunking_run in context["paths"]["chunking_runs"]:
        started = perf_counter()
        records, texts, source, parser = text_inputs(chunking_run)
        calls_before = len(encoder.calls)
        vectors, norms = encoder.encode_documents(texts)
        run_record = {**base_record(config, context, "text"), "input": source}
        summary = {"schema_version": CONFIG_SCHEMA, "status": "completed", "channel": "text",
                   "input": source, "vectors": int(vectors.shape[0]), "dimension": int(vectors.shape[1]),
                   "normalized": True, "norm_before_normalization": norm_summary(norms),
                   "elapsed_seconds": perf_counter() - started,
                   "model": encoder.record() | {"calls_this_run": len(encoder.calls) - calls_before}}
        results.append(save_run(context["paths"]["output_root"], f"text_{parser}", run_record, records,
                                vectors, summary))
    return results


def embed_images(config, context, selection):
    started = perf_counter()
    records, paths, source = page_image_inputs(context["paths"]["page_image_run"], selection)
    encoder = QwenVLEncoder(config["image"]["model"])
    images = []
    for path in paths:
        with Image.open(path) as image:
            images.append(image.convert("RGB"))
    vectors, norms = encoder.encode_images(images)
    for record, seconds in zip(records, encoder.item_seconds):
        record["encode_seconds"] = seconds
    run_record = {**base_record(config, context, "image"), "input": source}
    summary = {"schema_version": CONFIG_SCHEMA,
               "status": "completed" if not source["missing_pages"] else "completed_with_problems",
               "channel": "image", "input": source, "vectors": int(vectors.shape[0]),
               "dimension": int(vectors.shape[1]), "normalized": True,
               "norm_before_normalization": norm_summary(norms),
               "elapsed_seconds": perf_counter() - started, "model": encoder.record()}
    return [save_run(context["paths"]["output_root"], "image_page", run_record, records, vectors, summary)]


def main(argv=None):
    cli = argparse.ArgumentParser(description="청크 텍스트·페이지 이미지 임베딩")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="임베딩 설정 JSON")
    cli.add_argument("--channel", choices=CHANNELS, action="append",
                     help="설정의 channels 대신 실행할 채널. 여러 번 지정 가능")
    cli.add_argument("--check", action="store_true", help="설정·입력·키 존재 확인만. 모델 호출·파일 생성 없음")
    options = cli.parse_args(argv)
    try:
        config, context = load_config(options.config)
        channels = options.channel or config["channels"]
        selection = load_selection(context["paths"]["selection"])
        if options.check:
            report = {"status": "configuration_valid", "channels": channels,
                      "chunking_runs": [{"path": str(p), "chunks_file": (p / CHUNKS_FILE).is_file()}
                                        for p in context["paths"]["chunking_runs"]],
                      "page_image_run": str(context["paths"]["page_image_run"]),
                      "api_key_env": config["text"]["model"]["api_key_env"],
                      "api_key_present": bool(os.environ.get(config["text"]["model"]["api_key_env"], "").strip())}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        results = []
        if "text" in channels:
            results += embed_text(config, context)
        if "image" in channels:
            results += embed_images(config, context, selection)
        print(json.dumps([{k: r[k] for k in ("status", "channel", "output_dir", "vectors", "dimension")}
                          for r in results], ensure_ascii=False, indent=2))
        return 0 if all(r["status"] == "completed" for r in results) else 1
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        cli.exit(2, f"임베딩 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
