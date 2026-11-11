"""파싱 시작 시 비교 대상을 고정하고 종료 후 공통 비교기를 호출한다.

입력은 파싱 CLI 설정과 검증한 선정 목록이다. 같은 상위 폴더의 종료 실행을 읽어
파서·선정 범위가 같은 최근 실행을 찾으며 모델이나 정답을 추정하지 않는다.
종료 후 보고서는 입력 폴더 밖에 만들고, 비교 상태는 현재 실행 안에 따로 남긴다.
기록·추론 구간을 닫은 뒤 실행하므로 보고서 생성 비용은 파싱 자원 측정에 포함하지 않는다.
"""

# 표준 라이브러리: datetime은 기록된 UTC 시각 정렬, pathlib는 입력·출력 경계를 확인한다.
from datetime import datetime, timezone
from pathlib import Path

# 내부 모듈: common은 엄격한 읽기·해시·비밀 제거, schema는 기록 파일 이름,
# compare는 동일한 전후 보고서 생성, evaluate는 추론 전에 정답·평가 설정을 검증한다.
# pdf_source는 선정 목록 형식을 검증한다.
from common.files import file_hash, read_json, write_json
from common.redaction import clean
from .schema import FINISHED_FILE, MANIFEST_FILE
from .compare import LEGACY_RUN_FILE, LEGACY_SUMMARY_FILE, REPORT_JSON, REPORT_MARKDOWN, load_run, run_comparison
from .evaluate import validate_gold, validate_settings
from parsing.pdf_source import load_selection

# 프로젝트 루트 기준 기본 설정 경로다. 실행 인자로 교체할 수 있고 실제 경로·해시는 기록한다.
# 설치된 이 모듈의 위치를 기준으로 잡아 다른 현재 디렉터리에서도 같은 설정을 사용한다.
CONFIG_DIRECTORY = Path(__file__).resolve().parents[1] / "configs"
DEFAULT_RECORDING_CONFIG = CONFIG_DIRECTORY / "experiment_recording.json"
DEFAULT_EVALUATION_CONFIG = CONFIG_DIRECTORY / "experiment_evaluation.json"
# 아래 이름·접미사는 자동 보고서 저장 형식의 정의이며 실제 출력 경로는 CLI로 바꿀 수 있다.
AUTO_COMPARISON_FILE = "auto_comparison.json"
AUTO_COMPARISON_SCHEMA = "project.parsing_auto_comparison.v1"
COMPARISON_DIRECTORY_SUFFIX = "_comparison"
SELECTION_FILE = "selection.json"
TERMINAL_STATUSES = frozenset({"completed", "completed_with_errors"})
# 파싱 결과가 남은 페이지 상태의 정의다. failed는 비교할 출력이 없으므로 제외한다.
USABLE_PAGE_STATUSES = ("ok", "empty", "partial")


def _selection_key(selection):
    """문서·페이지의 입력 순서와 JSON 공백을 제외한 선정 범위를 비교한다."""
    return tuple(sorted((item["document_name"], tuple(sorted(item["page_numbers"])))
                        for item in selection["documents"]))


def _baseline_rejection(summary):
    """자동 기준으로 쓸 수 없는 이유를 반환한다. 일부 페이지 실패 실행은 비교 근거가 있어 허용한다."""
    if summary.get("status") not in TERMINAL_STATUSES:
        return "종료되지 않은 실행"
    if summary.get("fatal_error"):
        return "치명 오류·중단으로 끝난 실행"
    counts = summary.get("page_status_counts") or {}
    if summary["status"] != "completed" and not any(counts.get(name) for name in USABLE_PAGE_STATUSES):
        return "처리된 페이지가 없는 실행"
    return None


def _evidence(root):
    """대상 선택 뒤 기준 실행의 메타데이터가 바뀌면 자동 비교를 보류할 해시다."""
    return {name: file_hash(root / name) for name in
            (LEGACY_RUN_FILE, LEGACY_SUMMARY_FILE, SELECTION_FILE, MANIFEST_FILE, FINISHED_FILE)
            if (root / name).is_file()}


def prepare_comparison(args, selection):
    """파일 생성·모델 로딩 전에 대상·출력·정답을 검증한다. 자동 선택의 근거도 반환한다."""
    candidate = args.output_dir.resolve()
    plan = {"schema_version": AUTO_COMPARISON_SCHEMA, "status": "pending", "candidate": str(candidate),
            "mode": "explicit" if args.compare_baseline else "latest_compatible_sibling",
            "baseline": None, "search_root": str(candidate.parent), "all_pages": args.all_pages, "ignored": []}
    if args.no_auto_compare:
        return {**plan, "status": "disabled", "reason": "--no-auto-compare 지정"}
    if args.comparison_gold:
        validate_gold(read_json(args.comparison_gold))
        validate_settings(read_json(args.comparison_evaluation_config))
    plan["gold"] = str(args.comparison_gold.resolve()) if args.comparison_gold else None
    plan["evaluation_config"] = (str(args.comparison_evaluation_config.resolve())
                                  if args.comparison_gold else None)
    if args.compare_baseline:
        baseline = args.compare_baseline.resolve()
        if baseline == candidate:
            raise ValueError("기준 실행과 새 실행은 서로 다른 폴더여야 합니다.")
        # 명시한 기준은 다른 파서도 허용한다. 잘못된 실행 경로는 추론 전에 거부한다.
        load_run(baseline, None if args.all_pages else selection)
    else:
        matches = []
        folders = sorted(candidate.parent.iterdir()) if candidate.parent.exists() else []
        for folder in folders:
            if folder.is_symlink() or not folder.is_dir() or folder.resolve() == candidate or not (folder / LEGACY_RUN_FILE).is_file():
                continue
            try:
                metadata = read_json(folder / LEGACY_RUN_FILE)
                if metadata.get("schema_version") != "project.parsing.v1":
                    continue
                if metadata.get("parser") != args.parser or metadata.get("all_pages", False) != args.all_pages:
                    continue
                if _selection_key(load_selection(folder / SELECTION_FILE)) != _selection_key(selection):
                    continue
                summary = read_json(folder / LEGACY_SUMMARY_FILE)
                rejection = _baseline_rejection(summary)
                if rejection:
                    raise ValueError(rejection)
                if (folder / MANIFEST_FILE).exists():
                    final = read_json(folder / FINISHED_FILE)
                    start = read_json(folder / MANIFEST_FILE)
                    if final.get("status") not in TERMINAL_STATUSES or final.get("run_id") != start.get("run_id"):
                        raise ValueError("공통 종료 기록·실행 ID 불일치")
                stamp = datetime.fromisoformat(metadata["started_at_utc"])
                if stamp.tzinfo is None or stamp > datetime.now(timezone.utc):
                    raise ValueError("시작 시각의 시간대 누락 또는 미래 시각")
                matches.append((stamp, folder.resolve()))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                plan["ignored"].append({"path": str(folder), "reason": str(exc)})
        if not matches:
            return {**plan, "status": "skipped", "reason": "같은 파서·선정 범위의 종료 실행 없음"}
        matches.sort(key=lambda item: item[0], reverse=True)
        if len(matches) > 1 and matches[0][0] == matches[1][0]:
            return {**plan, "status": "skipped", "reason": "최근 시작 시각이 같은 기준 실행이 여럿임"}
        _, baseline = matches[0]
    output = (args.comparison_output_dir or candidate.with_name(candidate.name + COMPARISON_DIRECTORY_SUFFIX)).resolve()
    if any(output.is_relative_to(root) for root in (baseline, candidate)):
        raise ValueError("비교 보고서는 기준·새 실행 폴더 밖에 저장해야 합니다.")
    if output.exists():
        raise FileExistsError(f"비교 보고서 폴더가 이미 있습니다: {output}")
    return {**plan, "baseline": str(baseline), "baseline_evidence": _evidence(baseline),
            "output_dir": str(output)}


def finish_comparison(plan, root, *, interrupted=False, secrets=()):
    """파싱 결과를 보존한 채 비교 성공·생략·실패를 기록한다. 비교 실패는 호출부에 알린다."""
    result = dict(plan)
    if interrupted and result["status"] == "pending":
        result.update(status="skipped", reason="파싱 중단으로 자동 비교 생략")
    if result["status"] == "pending":
        try:
            baseline = Path(result["baseline"])
            if _evidence(baseline) != result["baseline_evidence"]:
                raise ValueError("선택 이후 기준 실행의 메타데이터가 바뀌었습니다.")
            report = run_comparison(baseline, root, selection=None if result["all_pages"] else Path(root) / SELECTION_FILE,
                gold=result["gold"], evaluation_config=result["evaluation_config"], output_dir=result["output_dir"])
            output = Path(result["output_dir"])
            result.update(status="completed", summary=report["summary"],
                          report_files={name: {"path": str(output / name), "sha256": file_hash(output / name)}
                                        for name in (REPORT_JSON, REPORT_MARKDOWN)})
        except Exception as exc:
            result.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)})
    result = clean(result, secrets)
    write_json(Path(root) / AUTO_COMPARISON_FILE, result)
    return result
