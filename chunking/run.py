"""파싱 실행 폴더를 읽어 구조 기반 청크를 만들고 새 청킹 실행 폴더에 저장하는 진입점이다.

전체 위치: PDF → 파싱(outputs/parsing_*) → 청킹〔현재〕(outputs/chunking_*) → 임베딩
입력: configs/chunking.json(입력 파싱 실행·재인식 결과·선정 목록·토크나이저·크기·라벨 역할), 페이지 JSON.
      재인식 결과(outputs/rerecognition_vl_*)는 입력 실행의 run.json 해시가 일치할 때만 적용한다.
      채택된 블록은 재인식 글자로 청크를 만들고, 복구하지 못한 블록은 청크에서 빼고 사유를 남긴다.
출력: 입력 실행마다 새 폴더 outputs/chunking_<파서>_<UTC 시각>/
  run.json        설정 내용·해시, 입력 실행·페이지 JSON 해시, 토크나이저 출처, 코드 해시, 환경
  chunks.jsonl    청크 한 줄에 하나. 텍스트·토큰 수·원래 블록 ID·좌표·검사 신호
  excluded.jsonl  청크에 넣지 않은 블록과 사유
  summary.json    페이지·블록·청크·토큰 통계와 누락·미배정 확인

실행 부작용: 처음 실행하면 토크나이저·설정 JSON을 Hugging Face 캐시에 내려받는다(가중치 제외).
입력 파싱 결과는 읽기만 하고, 출력 폴더는 새로 만들며 기존 폴더를 덮어쓰지 않는다.
--check는 설정·입력 경로·페이지 파일 존재만 확인하고 다운로드·파일 생성을 하지 않는다.
"""

# 표준 라이브러리: argparse는 CLI, json은 JSONL 직렬화·콘솔 출력, platform·sys는 실행 환경,
# statistics는 토큰 분포, Counter는 사유·라벨 집계, datetime은 UTC 실행 이름,
# importlib.metadata는 설치 버전 조회, Path는 경로, perf_counter는 경과 시간이다.
import argparse
from collections import Counter
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import statistics
import sys
from time import perf_counter

# 내부 공통 모듈: 엄격한 JSON 읽기·배타적 저장·해시를 parsing·experiments와 같은 규칙으로 쓴다.
import common
from common.files import file_hash, read_json, write_json

# 내부 파싱 모듈: load_selection은 선정 목록 검증, quality_checks는 반복·병합·표 격자 신호를 만든다.
# 청킹은 신호를 계산하지 않고 파싱 검사의 결과를 그대로 받아 청크에 표시한다.
# result_store는 파싱 실행 폴더의 문서 ID·페이지 파일 이름 규칙이다. 파싱이 저장한 규칙 그대로 읽는다.
from parsing.pdf_source import load_selection
from parsing.quality_checks import CONTENT_RULES, RULE_VERSION, content_findings, inspect_page
from parsing.result_store import document_id, document_path, page_stem

# 내부 청킹 모듈: structure는 페이지별 청크 생성, tokens는 토크나이저 로딩을 맡는다.
from .structure import build_page_chunks, label_roles

# 기본 설정 위치다. --config로 바꿀 수 있고 실제 경로·해시는 run.json에 남긴다.
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "chunking.json"
# 아래는 설정·출력 형식의 정의다.
CONFIG_SCHEMA = "project.chunking.v1"
CONFIG_FIELDS = frozenset({"schema_version", "input_runs", "rerecognition_runs", "selection", "output_root",
                           "tokenizer", "max_tokens", "overlap_tokens", "caption_max_gap_points",
                           "table_header_rows_without_th", "kind_markers", "labels", "flag_rules"})
# 재인식 결과 폴더의 파일 이름과 상태 값이다(parsing/rerecognize.py의 출력 형식).
REREC_RUN_FILE, REREC_BLOCKS_FILE = "run.json", "blocks.jsonl"
# 재인식 상태별 청킹 처리다. 채택은 글자 대체, 나머지는 반복 출력이 그대로이므로 청크에서 뺀다.
REREC_ACCEPTED = "accepted"
REREC_EXCLUSION_REASONS = {"unresolved": "generation_limit_unrecovered",
                           "skipped_unsupported_label": "generation_limit_not_rerecognized"}
TOKENIZER_FIELDS = frozenset({"repo_id", "revision", "tokenizer_file", "model_config_file", "add_special_tokens"})
LABEL_FIELDS = frozenset({"title_levels", "text", "table", "chart", "visual", "caption", "excluded"})
MARKER_FIELDS = frozenset({"table", "chart", "visual", "caption"})
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
PARSING_RUN_FILE = "run.json"
RUN_FILE, CHUNKS_FILE, EXCLUDED_FILE, SUMMARY_FILE = "run.json", "chunks.jsonl", "excluded.jsonl", "summary.json"
# 토큰 분포 요약에 쓰는 분위수 개수다. 10등분의 마지막 경계가 상위 10% 경계(p90)다.
QUANTILE_PARTS = 10
# 버전을 기록할 패키지다. 토큰 수에 영향을 주는 토크나이저 관련 패키지만 남긴다.
RECORDED_PACKAGES = ("tokenizers", "huggingface_hub")


def load_config(path: Path) -> tuple[dict, dict]:
    """설정 오타·잘못된 타입을 처리 전에 거부하고 상대 경로를 설정 파일 기준으로 푼다."""
    path = Path(path).resolve()
    config = read_json(path)
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS or config["schema_version"] != CONFIG_SCHEMA:
        raise ValueError(f"청킹 설정의 스키마·필드를 확인하세요: {sorted(CONFIG_FIELDS)}")
    if set(config["tokenizer"]) != TOKENIZER_FIELDS or type(config["tokenizer"]["add_special_tokens"]) is not bool:
        raise ValueError(f"tokenizer 필드를 확인하세요: {sorted(TOKENIZER_FIELDS)}")
    if set(config["labels"]) != LABEL_FIELDS or set(config["kind_markers"]) != MARKER_FIELDS:
        raise ValueError("labels·kind_markers 필드를 확인하세요.")
    label_roles(config["labels"])
    for key in ("max_tokens", "table_header_rows_without_th"):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(f"{key}는 1 이상의 정수여야 합니다.")
    if type(config["overlap_tokens"]) is not int or not 0 <= config["overlap_tokens"] < config["max_tokens"]:
        raise ValueError("overlap_tokens는 0 이상 max_tokens 미만의 정수여야 합니다.")
    if type(config["caption_max_gap_points"]) not in (int, float) or config["caption_max_gap_points"] < 0:
        raise ValueError("caption_max_gap_points는 0 이상의 숫자여야 합니다.")
    if not isinstance(config["input_runs"], list) or not config["input_runs"]:
        raise ValueError("input_runs에는 파싱 실행 폴더가 하나 이상 필요합니다.")
    if not isinstance(config["rerecognition_runs"], list):
        raise ValueError("rerecognition_runs는 재인식 결과 폴더 목록이어야 합니다(없으면 빈 목록).")
    base = path.parent
    paths = {"input_runs": [(base / item).resolve() for item in config["input_runs"]],
             "rerecognition_runs": [(base / item).resolve() for item in config["rerecognition_runs"]],
             "selection": (base / config["selection"]).resolve(),
             "output_root": (base / config["output_root"]).resolve()}
    return config, {"path": str(path), "sha256": file_hash(path), "paths": paths}


def page_flags(record: dict, rules: set) -> tuple[dict, str]:
    """파싱 규칙 검사의 의심 신호를 요소별 집합으로 바꾼다. 검사 실패는 숨기지 않고 상태로 돌려준다."""
    try:
        result = inspect_page(record)
    except (ValueError, TypeError, KeyError) as exc:
        return {}, f"unavailable: {type(exc).__name__}"
    flags: dict[str, set] = {}
    for finding in result["findings"]:
        if finding["rule"] not in rules or finding["status"] != "suspected":
            continue
        # 병합 그룹 신호는 요소 하나가 아니라 그룹 구성원 목록에 붙는다.
        members = finding["evidence"].get("members") or [finding["element_id"]]
        for element_id in members:
            if element_id:
                flags.setdefault(element_id, set()).add(finding["rule"])
    return flags, "inspected"


def load_rerecognition(input_run: Path, candidates: list[Path]) -> tuple[dict | None, dict]:
    """입력 실행에 해당하는 재인식 결과를 찾아 {element_id: 처리} 사전으로 바꾼다.

    재인식 기록의 input_run_json_sha256이 입력 실행 run.json의 해시와 같아야 같은 실행으로 본다.
    경로 이름만 같고 내용이 다른 실행에 다른 결과를 덮어쓰지 않기 위해서다. 둘 이상 맞으면 거부한다.
    반환값의 처리에는 페이지 JSON 해시를 함께 담아, 적용할 때 그 페이지가 바뀌지 않았는지 확인한다.
    """
    run_hash = file_hash(input_run / PARSING_RUN_FILE)
    matches = []
    for folder in candidates:
        record = read_json(folder / REREC_RUN_FILE)
        if record.get("input_run_json_sha256") == run_hash:
            matches.append((folder, record))
    if not matches:
        return None, {}
    if len(matches) > 1:
        raise ValueError(f"한 입력 실행에 재인식 결과가 여럿입니다: {[str(f) for f, _ in matches]}")
    folder, record = matches[0]
    blocks_path = folder / REREC_BLOCKS_FILE
    overrides = {}
    for line in blocks_path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        source = {"run": folder.name, "status": row["status"],
                  "dpi": next((a["dpi"] for a in row["attempts"] if a["accepted"]), None),
                  "original_content_sha256": row["original"]["content_sha256"]}
        if row["status"] == REREC_ACCEPTED:
            item = {"content": row["content"], "source": {**source, "content_sha256": row["content_sha256"]}}
        elif row["status"] in REREC_EXCLUSION_REASONS:
            item = {"exclude": REREC_EXCLUSION_REASONS[row["status"]], "source": source}
        else:
            raise ValueError(f"알 수 없는 재인식 상태입니다: {row['status']!r}")
        overrides[row["element_id"]] = {**item, "label": row["label"], "page_json": row["page_json"],
                                        "page_json_sha256": row["page_json_sha256"]}
    info = {"path": str(folder), "run_json_sha256": file_hash(folder / REREC_RUN_FILE),
            "blocks_jsonl_sha256": file_hash(blocks_path), "blocks": len(overrides)}
    return info, overrides


def replacement_flags(flags: dict, overrides: dict, rules: set) -> dict:
    """대체 글자를 쓰는 블록은 원래 출력에서 계산한 반복·길이 신호를 지우고 새 글자로 다시 계산한다."""
    updated = {key: set(value) for key, value in flags.items()}
    for element_id, item in overrides.items():
        if "content" not in item:
            continue
        kept = {rule for rule in updated.get(element_id, set()) if rule not in CONTENT_RULES}
        fresh = {rule for rule, _ in content_findings(item["content"], item.get("label", "")) if rule in rules}
        updated[element_id] = kept | fresh
    return {key: value for key, value in updated.items() if value}


def distribution(values: list[int]) -> dict:
    """토큰 수 분포 요약이다. 값이 하나뿐이면 분위수 대신 그 값을 쓴다."""
    if not values:
        return {"count": 0}
    p90 = statistics.quantiles(values, n=QUANTILE_PARTS)[-1] if len(values) > 1 else values[0]
    return {"count": len(values), "min": min(values), "median": statistics.median(values),
            "p90": p90, "max": max(values), "total": sum(values)}


def code_hashes() -> dict:
    """청크 결과에 영향을 주는 코드의 해시다. 청킹 전체와 사용한 파싱 검사·공통 모듈을 포함한다."""
    root = Path(__file__).resolve().parents[1]
    files = sorted(Path(__file__).parent.glob("*.py")) + sorted(Path(common.__file__).parent.glob("*.py"))
    files += [root / "parsing" / "quality_checks.py", root / "parsing" / "pdf_source.py"]
    return {path.relative_to(root).as_posix(): file_hash(path) for path in files}


def environment() -> dict:
    packages = {}
    for name in RECORDED_PACKAGES:
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {"python": sys.version, "platform": platform.platform(), "packages": packages}


def selected_pages(input_run: Path, selection: dict):
    """선정 목록 순서대로 (문서명, 페이지 번호, 페이지 JSON 경로)를 낸다. 파일 존재는 호출부가 확인한다."""
    for item in selection["documents"]:
        doc_id = document_id(item["document_name"])
        for number in sorted(item["page_numbers"]):
            yield item["document_name"], number, input_run / document_path(doc_id, f"{page_stem(number)}.json")


def write_jsonl(path: Path, rows: list[dict]) -> None:
    """한 줄에 객체 하나씩 새 파일로 저장한다. NaN을 거부하고 기존 파일은 덮어쓰지 않는다."""
    with path.open("x", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def chunk_run(input_run: Path, config: dict, context: dict, selection: dict, counter) -> dict:
    """파싱 실행 하나를 청킹해 새 출력 폴더에 저장하고 요약을 반환한다."""
    started = perf_counter()
    parsing_run = read_json(input_run / PARSING_RUN_FILE)
    parser = parsing_run["parser"]
    rerecognition, overrides = load_rerecognition(input_run, context["paths"]["rerecognition_runs"])
    stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
    output = context["paths"]["output_root"] / f"chunking_{parser}_{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    write_json(output / RUN_FILE, {
        "schema_version": CONFIG_SCHEMA, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "strategy": "layout_structure",
        "config": config, "config_path": context["path"], "config_sha256": context["sha256"],
        "input_run": str(input_run), "input_parser": parser,
        "input_run_json_sha256": file_hash(input_run / PARSING_RUN_FILE),
        "selection_path": str(context["paths"]["selection"]),
        "selection_sha256": file_hash(context["paths"]["selection"]),
        "rerecognition": rerecognition,
        "tokenizer": counter.record, "quality_rule_version": RULE_VERSION,
        "code_sha256": code_hashes(), "environment": environment()})

    rules = set(config["flag_rules"])
    chunks, excluded, pages = [], [], []
    missing, unreadable, unassigned = [], [], []
    totals = Counter()
    unmapped = Counter()
    applied_overrides: dict = {}
    blocks_total = blocks_in_chunks = blocks_heading_only = 0
    for name, number, path in selected_pages(input_run, selection):
        if not path.is_file():
            missing.append({"document_name": name, "page_number": number})
            continue
        try:
            record = read_json(path)
        except (OSError, UnicodeError, ValueError) as exc:
            unreadable.append({"document_name": name, "page_number": number, "reason": type(exc).__name__})
            continue
        flags, flag_status = page_flags(record, rules)
        page_json = path.relative_to(input_run).as_posix()
        page_overrides = {eid: item for eid, item in overrides.items() if item["page_json"] == page_json}
        for item in page_overrides.values():
            # 재인식 이후 페이지 JSON이 바뀌었다면 대상 블록의 원래 출력이 달라졌을 수 있어 적용하지 않는다.
            if item["page_json_sha256"] != file_hash(path):
                raise ValueError(f"재인식 이후 페이지 JSON이 바뀌었습니다: {page_json}")
        applied_overrides.update(page_overrides)
        flags = replacement_flags(flags, page_overrides, rules)
        page_chunks, page_excluded, stats = build_page_chunks(record, config, counter, flags, parser,
                                                              page_overrides)
        source = {"page_json": path.relative_to(input_run).as_posix(), "page_json_sha256": file_hash(path),
                  "page_status": record.get("status"), "quality_flags": flag_status}
        for chunk in page_chunks:
            chunk["source"] = source
        for item in page_excluded:
            item.update(page_id=record.get("page_id"), page_json=source["page_json"])
        chunks += page_chunks
        excluded += page_excluded
        pages.append({"page_id": record.get("page_id"), "chunks": len(page_chunks),
                      "excluded": len(page_excluded), "quality_flags": flag_status})
        for key in ("captions_attached", "captions_as_text", "title_only_chunks", "hard_splits",
                    "table_parse_failures"):
            totals[key] += stats.get(key, 0)
        unmapped.update(stats.get("unmapped_labels", {}))
        # 모든 블록이 청크 내용·제목 문맥·제외 기록 중 한 곳에 있어야 한다. 빠진 블록은 숨기지 않는다.
        content_ids = {e for c in page_chunks for e in c["element_ids"]}
        heading_ids = {e for c in page_chunks for e in c["heading_element_ids"]}
        excluded_ids = {e["element_id"] for e in page_excluded}
        # 파싱 실패 페이지는 페이지 단위 제외 기록(element_id=None) 하나로 남기므로 블록 대조에서 뺀다.
        page_not_parsed = None in excluded_ids
        for block in (record.get("parsing") or {}).get("blocks", []):
            blocks_total += 1
            element_id = block["element_id"]
            if element_id in content_ids:
                blocks_in_chunks += 1
            elif element_id in heading_ids:
                blocks_heading_only += 1
            elif element_id not in excluded_ids and not page_not_parsed:
                unassigned.append(element_id)

    write_jsonl(output / CHUNKS_FILE, chunks)
    write_jsonl(output / EXCLUDED_FILE, excluded)
    tokens = [c["token_count"] for c in chunks]
    limit = counter.model_position_limit
    summary = {
        "schema_version": CONFIG_SCHEMA,
        "status": "completed" if not (missing or unreadable or unassigned) else "completed_with_problems",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": perf_counter() - started,
        "input_run": str(input_run), "input_parser": parser, "output_dir": str(output),
        "pages": {"selected": sum(len(d["page_numbers"]) for d in selection["documents"]),
                  "chunked": len(pages), "missing": missing, "unreadable": unreadable,
                  "quality_flags_unavailable": sum(p["quality_flags"] != "inspected" for p in pages)},
        "blocks": {"total": blocks_total, "in_chunks": blocks_in_chunks, "heading_only": blocks_heading_only,
                   "excluded": len([e for e in excluded if e["element_id"]]), "unassigned": unassigned},
        "chunks": {"total": len(chunks), "by_kind": dict(Counter(c["kind"] for c in chunks)),
                   "flagged": sum(bool(c["flags"]) for c in chunks),
                   "by_flag": dict(Counter(f for c in chunks for f in c["flags"])),
                   "split_groups": sum(c["split"]["index"] == 0 and c["split"]["count"] > 1 for c in chunks)},
        "tokens": {"all": distribution(tokens),
                   "unflagged": distribution([c["token_count"] for c in chunks if not c["flags"]]),
                   "max_tokens": config["max_tokens"],
                   "over_max_tokens": sum(t > config["max_tokens"] for t in tokens),
                   "model_position_limit": limit,
                   "over_model_limit": sum(t > limit for t in tokens) if limit else None},
        "excluded_by_reason": dict(Counter(e["reason"] for e in excluded)),
        # 선정 범위 밖 페이지의 재인식 결과는 적용하지 않으므로 적용 수와 기록 수를 따로 센다.
        "rerecognition": None if rerecognition is None else {
            **rerecognition, "applied": len(applied_overrides),
            "replaced": sum("content" in item for item in applied_overrides.values()),
            "excluded": sum("exclude" in item for item in applied_overrides.values())},
        "unmapped_labels": dict(unmapped),
        **{key: totals[key] for key in ("captions_attached", "captions_as_text", "title_only_chunks",
                                         "hard_splits", "table_parse_failures")},
        "per_page": pages,
    }
    write_json(output / SUMMARY_FILE, summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="파싱 결과의 구조 기반 청킹")
    cli.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="청킹 설정 JSON")
    cli.add_argument("--input-run", type=Path, action="append",
                     help="설정의 input_runs 대신 처리할 파싱 실행 폴더. 여러 번 지정 가능")
    cli.add_argument("--check", action="store_true", help="설정·입력 확인만. 다운로드·파일 생성 없음")
    options = cli.parse_args(argv)
    try:
        config, context = load_config(options.config)
        if options.input_run:
            context["paths"]["input_runs"] = [path.resolve() for path in options.input_run]
            config = {**config, "input_runs": [str(path) for path in context["paths"]["input_runs"]]}
        selection = load_selection(context["paths"]["selection"])
        inputs = []
        for run in context["paths"]["input_runs"]:
            if not (run / PARSING_RUN_FILE).is_file():
                raise ValueError(f"파싱 실행 폴더가 아닙니다: {run}")
            found = sum(path.is_file() for _, _, path in selected_pages(run, selection))
            rerecognition, _ = load_rerecognition(run, context["paths"]["rerecognition_runs"])
            inputs.append({"input_run": str(run), "parser": read_json(run / PARSING_RUN_FILE).get("parser"),
                           "selected_pages_found": found, "rerecognition": rerecognition})
        if options.check:
            print(json.dumps({"status": "configuration_valid", "config": context["path"],
                              "inputs": inputs, "output_root": str(context["paths"]["output_root"])},
                             ensure_ascii=False, indent=2))
            return 0
        from .tokens import TokenCounter
        counter = TokenCounter(config["tokenizer"])
        results = []
        for run in context["paths"]["input_runs"]:
            summary = chunk_run(run, config, context, selection, counter)
            results.append({key: summary[key] for key in ("status", "input_parser", "output_dir")}
                           | {"chunks": summary["chunks"]["total"], "tokens": summary["tokens"]["all"]})
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0 if all(r["status"] == "completed" for r in results) else 1
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"청킹 실패: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
