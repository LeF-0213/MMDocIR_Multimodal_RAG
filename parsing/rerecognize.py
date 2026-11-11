"""끝난 VL 파싱 실행에서 생성 한도에 닿은 블록만 높은 해상도로 다시 인식하는 후처리 단계다.

전체 위치: PDF → 파싱(outputs/parsing_vl_*) → 재인식〔현재〕(outputs/rerecognition_vl_*) → 청킹
입력: configs/rerecognition.json(입력 실행·모델·생성 한도·DPI·프롬프트·채택 규칙), 입력 실행의
      selection.json·페이지 JSON·documents/<문서 ID>/source.pdf(파싱 때 복사한 원본 사본).
출력: 새 폴더 outputs/rerecognition_vl_<UTC 시각>/
  run.json      설정 내용·해시, 입력 실행 run.json 해시, 모델 파일 해시, 코드 해시, 환경
  blocks.jsonl  대상 블록마다 한 줄. 원래 출력의 해시·토큰 수, 시도별 DPI·입력 크기·토큰 수·신호, 채택 결과
  crops/        시도마다 모델에 넣은 영역 이미지(RGB PNG). 원본과 눈으로 대조하는 용도
  summary.json  대상·채택·미해결 수와 경과 시간

왜 필요한가(docs/parsing_log/PARSING_ROOT_CAUSES.md 2절):
- VL 반복 출력 3건은 출력이 생성 한도 4,096토큰에 정확히 닿은 image·chart 블록이었다.
- 설치된 PaddleX 로컬 경로는 repetition_penalty를 무시하므로 생성 설정으로는 반복을 끊을 수 없다.
- Toyota p2는 같은 모델·프롬프트·입력 방식에서 해상도만 576 DPI로 올리면 반복 없이 원문을 옮겼다.

처리 규칙:
- 대상: 모델과 같은 tokenizer.json으로 센 출력 토큰 수가 (생성 한도 - 여유분) 이상인 블록.
  디코딩한 글자를 다시 인코딩하면 생성 토큰 수와 몇 개 차이 날 수 있어 여유분을 둔다(실험에서 4,095개 관찰).
- 표·수식 등 파이프라인이 프롬프트 외 전처리(표 안 그림 토큰화, 여백 자르기)를 하는 라벨은 그 전처리를
  재현하지 않으므로 다시 인식하지 않고 사유를 남긴다.
- 입력: 실행 폴더의 원본 PDF 사본에서 블록 PDF 영역만 지정 DPI로 렌더링한다. 파이프라인과 같은 채널 순서
  (BGR 배열을 변환 없이 전달)로 넣는다. 픽셀 하한·상한도 파이프라인 기본값과 같게 둔다.
- 채택: 생성이 한도 전에 끝났고 설정한 반복·길이 신호(quality_checks.content_findings)가 없을 때만.
  모든 DPI에서 실패하면 unresolved로 남기고, 원래 출력을 고치거나 지우지 않는다.

실행 부작용: 모델 가중치 로딩(약 2GB)과 블록당 수 분~수십 분의 CPU 추론. 모델 폴더에 파일이 없으면
PaddleX가 내려받을 수 있다. 입력 실행 폴더는 읽기만 하고, 출력 폴더는 새로 만들며 덮어쓰지 않는다.
--check는 설정·입력·대상 목록만 출력하고 모델 로딩·파일 생성을 하지 않는다.
cpu_threads 설정은 PaddleX 정적 그래프 실행기에만 적용되고 VL 인식 모델(동적 그래프)에는 전달되지 않아
여기서 받지 않는다(같은 문서 조사 과정에서 확인).
"""

# 표준 라이브러리: argparse는 CLI, datetime은 UTC 실행 이름, importlib.metadata는 설치 버전,
# json은 JSONL 직렬화·콘솔 출력, Path는 경로, platform·sys는 실행 환경, perf_counter는 경과 시간이다.
import argparse
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import sys
from time import perf_counter

# 외부 라이브러리: numpy는 모델 입력 배열, pymupdf(PyMuPDF)는 블록 영역을 지정 DPI로 다시 렌더링,
# PIL(Pillow)은 시도 이미지 저장, tokenizers는 모델의 tokenizer.json으로 출력 토큰 수를 센다.
import numpy as np
from PIL import Image
import pymupdf
from tokenizers import Tokenizer

# 내부 모듈: common은 엄격한 JSON 읽기·배타적 저장·해시, pdf_source는 선정 목록 검증,
# quality_checks는 원래 출력과 재인식 출력을 같은 반복·길이 규칙으로 판정한다.
import common
from common.files import digest, file_hash, read_json, write_json
from .pdf_source import load_selection
from .quality_checks import RULE_VERSION, content_findings

# 기본 설정 위치다. --config로 바꿀 수 있고 실제 경로·해시는 run.json에 남긴다.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "rerecognition.json"
# 아래는 설정·출력 형식의 정의다.
CONFIG_SCHEMA = "project.rerecognition.v1"
CONFIG_FIELDS = frozenset({"schema_version", "input_run", "output_root", "model", "generation_limit_tokens",
                           "limit_margin_tokens", "max_new_tokens", "render_dpis", "min_pixels", "max_pixels",
                           "channel_order", "prompts", "unsupported_labels", "rejection_rules"})
MODEL_FIELDS = frozenset({"model_name", "model_dir", "device"})
CHANNEL_ORDERS = ("bgr", "rgb")
SUPPORTED_PARSER = "vl"
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
RUN_FILE, BLOCKS_FILE, SUMMARY_FILE, CROPS_DIRECTORY = "run.json", "blocks.jsonl", "summary.json", "crops"
TOKENIZER_FILE = "tokenizer.json"
SELECTION_FILE = "selection.json"
# PDF 1 point = 1/72 inch. DPI를 렌더링 배율로 바꾸는 정의값이다.
POINTS_PER_INCH = 72
# 버전을 기록할 패키지다. 재인식 출력에 영향을 주는 모델 실행·렌더링·토큰 계산 패키지만 남긴다.
RECORDED_PACKAGES = ("paddlex", "paddleocr", "paddlepaddle", "pymupdf", "tokenizers", "numpy", "pillow")
ACCEPTED, UNRESOLVED, SKIPPED = "accepted", "unresolved", "skipped_unsupported_label"


def load_config(path: Path) -> tuple[dict, dict]:
    """설정 오타·잘못된 값을 모델 로딩 전에 거부하고 상대 경로를 설정 파일 기준으로 푼다."""
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
        raise ValueError(f"재인식 설정의 스키마·필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
    if not isinstance(config["model"], dict) or set(config["model"]) != MODEL_FIELDS:
        raise ValueError(f"model 필드를 확인하세요: {sorted(MODEL_FIELDS)}")
    for key in ("generation_limit_tokens", "max_new_tokens", "min_pixels", "max_pixels"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key}는 1 이상의 정수여야 합니다.")
    if type(config["limit_margin_tokens"]) is not int or not 0 <= config["limit_margin_tokens"] < config["generation_limit_tokens"]:
        raise ValueError("limit_margin_tokens는 0 이상 generation_limit_tokens 미만의 정수여야 합니다.")
    if config["min_pixels"] > config["max_pixels"]:
        raise ValueError("min_pixels는 max_pixels보다 클 수 없습니다.")
    dpis = config["render_dpis"]
    if not isinstance(dpis, list) or not dpis or any(type(d) is not int or d < 1 for d in dpis):
        raise ValueError("render_dpis에는 1 이상의 정수 DPI가 하나 이상 필요합니다.")
    if config["channel_order"] not in CHANNEL_ORDERS:
        raise ValueError(f"channel_order는 {CHANNEL_ORDERS} 중 하나여야 합니다.")
    prompts = config["prompts"]
    if (not isinstance(prompts, dict) or set(prompts) != {"default", "by_label"}
            or not isinstance(prompts["default"], str) or not isinstance(prompts["by_label"], dict)):
        raise ValueError("prompts에는 default 문자열과 by_label 객체가 필요합니다.")
    for key in ("unsupported_labels", "rejection_rules"):
        if not isinstance(config[key], list) or not all(isinstance(v, str) for v in config[key]):
            raise ValueError(f"{key}는 문자열 목록이어야 합니다.")
    base = path.parent
    paths = {"input_run": (base / config["input_run"]).resolve(),
             "output_root": (base / config["output_root"]).resolve(),
             "model_dir": Path(config["model"]["model_dir"]).expanduser().resolve()}
    return config, {"path": str(path), "sha256": file_hash(path), "paths": paths}


class TokenCounter:
    """모델의 tokenizer.json으로 출력 글자를 다시 인코딩해 토큰 수를 센다. 특수 토큰은 넣지 않는다."""

    def __init__(self, tokenizer_path: Path):
        self.path = Path(tokenizer_path)
        self.tokenizer = Tokenizer.from_file(str(self.path))
        self.record = {"path": str(self.path), "sha256": file_hash(self.path)}

    def count(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False).ids)


def prompt_for(label: str, config: dict) -> str | None:
    """라벨의 VLM 프롬프트다. 전처리를 재현하지 않는 라벨은 None이다."""
    if label in config["unsupported_labels"]:
        return None
    return config["prompts"]["by_label"].get(label, config["prompts"]["default"])


def find_targets(input_run: Path, config: dict, counter: TokenCounter) -> list[dict]:
    """선정 페이지의 블록 중 출력이 생성 한도에 닿은 블록을 찾는다. 파일은 읽기만 한다."""
    selection = load_selection(input_run / SELECTION_FILE)
    threshold = config["generation_limit_tokens"] - config["limit_margin_tokens"]
    targets = []
    for item in selection["documents"]:
        doc_id = digest(item["document_name"].encode("utf-8"))
        for number in sorted(item["page_numbers"]):
            path = input_run / "documents" / doc_id / f"page_{number:05d}.json"
            if not path.is_file():
                continue
            record = read_json(path)
            for block in (record.get("parsing") or {}).get("blocks", []):
                tokens = counter.count(block["content"])
                if tokens < threshold:
                    continue
                targets.append({
                    "element_id": block["element_id"], "page_id": record["page_id"],
                    "document_id": record["document_id"], "document_name": record["document_name"],
                    "page_number": record["page_number"], "page_index": record["page_index"],
                    "label": block["label"], "bbox_pdf_points": block["bbox_pdf_points"],
                    "bbox_image_pixels": block.get("bbox_image_pixels"),
                    "page_json": path.relative_to(input_run).as_posix(), "page_json_sha256": file_hash(path),
                    "source_sha256": record["source_sha256"],
                    "original": {"content_sha256": digest(block["content"].encode("utf-8")), "tokens": tokens,
                                 "characters": len(block["content"]),
                                 "findings": [rule for rule, _ in content_findings(block["content"], block["label"])]},
                    "prompt": prompt_for(block["label"], config),
                })
    return targets


def render_region(pdf_path: Path, page_index: int, bbox_points: list, dpi: int) -> np.ndarray:
    """PDF 한 페이지의 사각형 영역을 지정 DPI로 렌더링해 RGB uint8 배열(높이×너비×3)을 반환한다."""
    scale = dpi / POINTS_PER_INCH
    with pymupdf.open(pdf_path) as document:
        # alpha=False: 투명 채널 없이 흰 배경 RGB로 받는다. 파싱 단계 페이지 렌더링과 같은 색 공간이다.
        pixmap = document[page_index].get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                                                 clip=pymupdf.Rect(bbox_points), alpha=False,
                                                 colorspace=pymupdf.csRGB)
        return np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3).copy()


def load_model(config: dict, model_dir: Path):
    """PaddleX 인식 모델 하나만 불러온다. 지연 import로 --check와 테스트는 모델 라이브러리를 읽지 않는다."""
    from paddlex import create_model
    return create_model(model_name=config["model"]["model_name"], model_dir=str(model_dir),
                        device=config["model"]["device"])


def recognize(model, image_rgb: np.ndarray, prompt: str, config: dict) -> str:
    """영역 이미지를 파이프라인과 같은 채널 순서·픽셀 범위로 모델에 넣고 생성 글자를 받는다."""
    # 파이프라인은 BGR 페이지 배열에서 자른 블록을 변환 없이 넘긴다. 같은 입력을 만들려면 채널을 뒤집는다.
    image = image_rgb[..., ::-1].copy() if config["channel_order"] == "bgr" else image_rgb
    result = next(iter(model.predict([{"image": image, "query": prompt}],
                                     max_new_tokens=config["max_new_tokens"], min_pixels=config["min_pixels"],
                                     max_pixels=config["max_pixels"], use_cache=True, skip_special_tokens=True)))
    return result["result"]


def judge(text: str, label: str, counter: TokenCounter, config: dict) -> dict:
    """한도 전에 끝났는지와 반복·길이 신호를 판정한다. 둘 다 통과해야 채택한다."""
    tokens = counter.count(text)
    ended = tokens < config["generation_limit_tokens"] - config["limit_margin_tokens"]
    findings = [rule for rule, _ in content_findings(text, label) if rule in config["rejection_rules"]]
    return {"output_tokens": tokens, "ended_before_limit": ended, "findings": findings,
            "accepted": ended and not findings}


def code_hashes() -> dict:
    root = Path(__file__).resolve().parents[1]
    files = [Path(__file__), Path(__file__).parent / "quality_checks.py", Path(__file__).parent / "pdf_source.py"]
    files += sorted(Path(common.__file__).parent.glob("*.py"))
    return {path.relative_to(root).as_posix(): file_hash(path) for path in files}


def environment() -> dict:
    packages = {}
    for name in RECORDED_PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def model_files(model_dir: Path) -> list[dict]:
    """모델 폴더의 파일별 크기·SHA-256이다. 가중치가 바뀌면 재인식 결과를 비교할 수 없으므로 남긴다."""
    return [{"path": p.relative_to(model_dir).as_posix(), "size": p.stat().st_size, "sha256": file_hash(p)}
            for p in sorted(model_dir.rglob("*")) if p.is_file()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def rerecognize(input_run: Path, config: dict, context: dict, counter: TokenCounter, model) -> dict:
    """대상 블록을 다시 인식해 새 폴더에 저장하고 요약을 반환한다."""
    started = perf_counter()
    stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
    output = context["paths"]["output_root"] / f"rerecognition_{SUPPORTED_PARSER}_{stamp}"
    (output / CROPS_DIRECTORY).mkdir(parents=True, exist_ok=False)
    model_dir = context["paths"]["model_dir"]
    write_json(output / RUN_FILE, {
        "schema_version": CONFIG_SCHEMA, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": config, "config_path": context["path"], "config_sha256": context["sha256"],
        "input_run": str(input_run), "input_run_json_sha256": file_hash(input_run / RUN_FILE),
        "model": {**config["model"], "resolved_model_dir": str(model_dir), "files": model_files(model_dir)},
        "tokenizer": counter.record, "quality_rule_version": RULE_VERSION,
        "code_sha256": code_hashes(), "environment": environment()})

    rows = []
    for target in find_targets(input_run, config, counter):
        row = {**target, "id_origin": "project_generated", "attempts": [], "status": None,
               "content": None, "content_sha256": None}
        if target["prompt"] is None:
            row["status"] = SKIPPED
            rows.append(row)
            continue
        pdf_path = input_run / "documents" / target["document_id"] / "source.pdf"
        if file_hash(pdf_path) != target["source_sha256"]:
            raise ValueError(f"원본 PDF 사본의 해시가 페이지 기록과 다릅니다: {pdf_path}")
        for dpi in config["render_dpis"]:
            image = render_region(pdf_path, target["page_index"], target["bbox_pdf_points"], dpi)
            crop_name = f"{target['element_id'].replace(':', '_')}_{dpi}dpi.png"
            Image.fromarray(image).save(output / CROPS_DIRECTORY / crop_name)
            began = perf_counter()
            text = recognize(model, image, target["prompt"], config)
            verdict = judge(text, target["label"], counter, config)
            row["attempts"].append({"dpi": dpi, "input_size": [int(image.shape[1]), int(image.shape[0])],
                                    "crop": f"{CROPS_DIRECTORY}/{crop_name}", "seconds": perf_counter() - began,
                                    "text_sha256": digest(text.encode("utf-8")), "characters": len(text),
                                    "text": text, **verdict})
            print(json.dumps({"element_id": target["element_id"], "dpi": dpi,
                              **{k: verdict[k] for k in ("output_tokens", "ended_before_limit", "findings", "accepted")}},
                             ensure_ascii=False), flush=True)
            if verdict["accepted"]:
                row.update(status=ACCEPTED, content=text, content_sha256=digest(text.encode("utf-8")))
                break
        if row["status"] is None:
            row["status"] = UNRESOLVED
        rows.append(row)

    write_jsonl(output / BLOCKS_FILE, rows)
    counts = {status: sum(r["status"] == status for r in rows) for status in (ACCEPTED, UNRESOLVED, SKIPPED)}
    summary = {"schema_version": CONFIG_SCHEMA, "status": "completed",
               "finished_at_utc": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": perf_counter() - started, "input_run": str(input_run), "output_dir": str(output),
               "targets": len(rows), **counts,
               "blocks": [{"element_id": r["element_id"], "label": r["label"], "status": r["status"],
                           "original_tokens": r["original"]["tokens"],
                           "attempts": [{k: a[k] for k in ("dpi", "output_tokens", "ended_before_limit", "findings")}
                                        for a in r["attempts"]]} for r in rows]}
    write_json(output / SUMMARY_FILE, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="VL 생성 한도 블록의 고해상도 재인식")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="재인식 설정 JSON")
    cli.add_argument("--input-run", type=Path, help="설정의 input_run 대신 처리할 VL 파싱 실행 폴더")
    cli.add_argument("--check", action="store_true", help="설정·입력·대상 확인만. 모델 로딩·파일 생성 없음")
    options = cli.parse_args(argv)
    try:
        config, context = load_config(options.config)
        if options.input_run:
            context["paths"]["input_run"] = options.input_run.resolve()
            config = {**config, "input_run": str(context["paths"]["input_run"])}
        input_run = context["paths"]["input_run"]
        parser = read_json(input_run / RUN_FILE).get("parser")
        if parser != SUPPORTED_PARSER:
            raise ValueError(f"VL 파싱 실행만 처리합니다(입력 parser={parser!r}).")
        counter = TokenCounter(context["paths"]["model_dir"] / TOKENIZER_FILE)
        if options.check:
            targets = find_targets(input_run, config, counter)
            print(json.dumps({"status": "configuration_valid", "input_run": str(input_run),
                              "targets": [{k: t[k] for k in ("element_id", "label", "prompt")}
                                          | {"original_tokens": t["original"]["tokens"]} for t in targets]},
                             ensure_ascii=False, indent=2))
            return 0
        model = load_model(config, context["paths"]["model_dir"])
        summary = rerecognize(input_run, config, context, counter, model)
        print(json.dumps({k: summary[k] for k in ("output_dir", "targets", ACCEPTED, UNRESOLVED, SKIPPED)},
                         ensure_ascii=False))
        return 0 if not summary[UNRESOLVED] else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"재인식 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
