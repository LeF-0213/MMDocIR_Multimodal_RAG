"""저장된 두 실행의 입력·설정·소스·모델·출력·평가·비용을 비교하는 CLI다.

기존 parsing.v1과 공통 experiment.v1을 읽고 JSON·Markdown을 같은 결과에서 생성한다.
기본 동작은 콘솔 요약이며 --output-dir를 지정하면 입력 폴더 밖에 새 파일을 만든다.
모델 import·다운로드·기존 기록 수정은 하지 않는다. 정답이 없으면 품질은 미측정이다.
"""

# 표준 라이브러리: argparse는 입력 경로, Counter는 상태 집계, difflib는 실제 소스 diff,
# hashlib는 기존 문서명 ID 매핑, json은 콘솔 요약, re는 페이지 파일 형식을 확인한다.
import argparse
from difflib import unified_diff
import hashlib
import json
import re
# pathlib는 경로, datetime은 비교 시각을 기록한다.
from pathlib import Path
from datetime import datetime, timezone

# 내부 모듈: common은 표준 입출력·비밀 처리, schema는 기록 파일 이름,
# evaluate는 원문 정답 기반 순수 평가다.
from common.files import file_hash, inside, json_bytes, read_json, write_json
from common.redaction import clean
from .schema import COMPARE_SCHEMA, FINISHED_FILE, MANIFEST_FILE, RECORD_SCHEMA
from .evaluate import evaluate_pair, validate_settings
# 기존 규칙 검사는 의미 정확도 평가와 별도로 재사용한다. 이 모듈도 모델 의존성이 없다.
from parsing.quality_checks import LIMITS, RULE_VERSION, inspect_page

# 파일명·정규식은 기존 parsing.v1 저장 형식의 정의이며 실행 경로 기본값이 아니다.
LEGACY_RUN_FILE = "run.json"
LEGACY_SUMMARY_FILE = "summary.json"
LEGACY_DOCUMENT_DIRECTORY = "documents"
LEGACY_SOURCE_FILE = "source.pdf"
PAGE_FILENAME = re.compile(r"page_(\d+)\.json$")
REPORT_JSON = "comparison.json"
REPORT_MARKDOWN = "comparison.md"


def differences(before, after, path=()):
    """키 부재·null·bool/int를 구분하고 목록 순서를 보존하는 재귀 비교다."""
    result = []
    if type(before) is dict and type(after) is dict:
        for key in sorted(before.keys() | after.keys()):
            route = (*path, key)
            if key not in before:
                result.append({"path": list(route), "kind": "added", "after": after[key]})
            elif key not in after:
                result.append({"path": list(route), "kind": "removed", "before": before[key]})
            else:
                result.extend(differences(before[key], after[key], route))
    elif type(before) is not type(after) or before != after:
        result.append({"path": list(path), "kind": "type_changed" if type(before) is not type(after) else "changed",
                       "before": before, "after": after})
    return result


def _optional(root, name):
    path = inside(root, name)
    return read_json(path) if path.is_file() else None


def load_run(root, selection=None):
    """페이지 추가 실행·실패·미완성 파일을 숨기지 않고 읽는다. 모든 파일은 읽기 전용이다."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("입력 실행 폴더가 없습니다.")
    legacy = _optional(root, LEGACY_RUN_FILE)
    manifest = _optional(root, MANIFEST_FILE)
    if any(value is not None and not isinstance(value, dict) for value in (legacy, manifest)):
        raise ValueError("실행 manifest는 JSON 객체여야 합니다.")
    if legacy is None and manifest is None:
        raise ValueError("지원하는 실행 manifest가 없습니다.")
    if manifest and manifest.get("schema_version") != RECORD_SCHEMA:
        raise ValueError("지원하지 않는 공통 실행 스키마입니다.")
    if legacy and legacy.get("schema_version") != "project.parsing.v1":
        raise ValueError("지원하지 않는 과거 파싱 스키마입니다.")
    finished = _optional(root, FINISHED_FILE)
    if finished is not None and not isinstance(finished, dict):
        raise ValueError("종료 manifest는 JSON 객체여야 합니다.")
    if finished and (not manifest or finished.get("run_id") != manifest.get("run_id")):
        raise ValueError("시작·종료 실행 ID가 다릅니다.")
    summary = _optional(root, LEGACY_SUMMARY_FILE)
    if summary is not None and not isinstance(summary, dict):
        raise ValueError("실행 요약은 JSON 객체여야 합니다.")
    pages = {}; problems = []; expected = set(); source_hashes = {}
    if selection is not None:
        if not isinstance(selection, dict) or not isinstance(selection.get("documents"), list) or not selection["documents"]:
            raise ValueError("선정 목록에 문서 목록이 필요합니다.")
        if selection.get("page_numbering") != "pdf_viewer_1_based":
            raise ValueError("선정 목록의 페이지 기준을 확인해야 합니다.")
        for item in selection["documents"]:
            if not isinstance(item.get("document_name"), str) or not item["document_name"]:
                raise ValueError("선정 목록에는 비어 있지 않은 문서명이 필요합니다.")
            if not isinstance(item.get("page_numbers"), list) or not item["page_numbers"]:
                raise ValueError("선정 목록에는 페이지 순번 목록이 필요합니다.")
            doc_id = hashlib.sha256(item["document_name"].encode("utf-8")).hexdigest()
            for number in item["page_numbers"]:
                if type(number) is not int or number < 1 or (doc_id, number) in expected:
                    raise ValueError("선정 목록 페이지 순번·중복 오류")
                expected.add((doc_id, number))
    document_root = root / LEGACY_DOCUMENT_DIRECTORY
    files = sorted(document_root.glob("*/page_*.json")) if document_root.exists() else []
    for path in files:
        match = PAGE_FILENAME.fullmatch(path.name)
        if not match:
            continue
        key = (path.parent.name, int(match.group(1)))
        if selection is not None and key not in expected:
            continue
        try:
            path = inside(root, path.relative_to(root))
            record = read_json(path)
            if not isinstance(record, dict):
                raise ValueError("페이지 결과는 JSON 객체여야 합니다.")
            if key[1] < 1 or (record.get("document_id"), record.get("page_number")) != key:
                raise ValueError("페이지 파일 경로와 내부 ID 불일치")
            if record.get("page_numbering") != "pdf_viewer_1_based":
                raise ValueError("페이지 번호 기준 불일치")
            blocks = record.get("parsing", {}).get("blocks", [])
            if not isinstance(blocks, list) or any(not isinstance(b, dict) or not isinstance(b.get("content"), str) for b in blocks):
                raise ValueError("페이지 블록 구조·내용 타입 오류")
            record["_input_json"] = path.relative_to(root).as_posix()
            record["_input_json_sha256"] = file_hash(path)
            # 추가 페이지 실행은 루트 manifest를 덮어쓴 것으로 간주하지 않는다.
            supplemental = path.with_name(path.stem + "_run.json")
            record["_page_execution"] = ({"path": supplemental.relative_to(root).as_posix(),
                "sha256": file_hash(inside(root, supplemental.relative_to(root))),
                "record": read_json(inside(root, supplemental.relative_to(root)))} if supplemental.is_file() else None)
            source = path.parent / LEGACY_SOURCE_FILE
            if key[0] not in source_hashes:
                source_hashes[key[0]] = file_hash(inside(root, source.relative_to(root))) if source.is_file() else None
            record["_actual_source_sha256"] = source_hashes[key[0]]
            if source_hashes[key[0]] and source_hashes[key[0]] != record.get("source_sha256"):
                record["_integrity_error"] = "실제 PDF 해시와 기록 불일치"
                problems.append({"page": list(key), "reason": record["_integrity_error"]})
            png = record.get("page_image", {})
            if png.get("path"):
                image = inside(root, png["path"])
                record["_actual_png_sha256"] = file_hash(image) if image.is_file() else None
                if record["_actual_png_sha256"] != png.get("sha256"):
                    problems.append({"page": list(key), "reason": "PNG 부재 또는 해시 불일치"})
                    record["_integrity_error"] = "PNG 부재 또는 해시 불일치"
                if png["path"] != path.with_suffix(".png").relative_to(root).as_posix():
                    record["_integrity_error"] = "다른 페이지의 PNG 참조"
                    problems.append({"page": list(key), "reason": record["_integrity_error"]})
            if "render" in record:
                try:
                    record["_rules"] = {"status": "inspected", "version": RULE_VERSION,
                                        "limits": LIMITS, "result": inspect_page(record)}
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    record["_rules"] = {"status": "unavailable", "reason": str(exc)}
            pages[key] = record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append({"page": list(key), "reason": str(exc)})
            pages[key] = {"document_id": key[0], "page_number": key[1], "status": "unresolved",
                          "_load_error": str(exc), "parsing": {"blocks": [], "status": "unresolved"}}
    if selection is None:
        expected = set(pages)
    for key in sorted(expected - pages.keys()):
        problems.append({"page": list(key), "reason": "예정 페이지 결과 없음"})
    for artifact in (finished or {}).get("artifacts", []):
        try:
            if file_hash(inside(root, artifact["path"])) != artifact["sha256"]:
                raise ValueError("결과물 해시 불일치")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            problems.append({"artifact_id": artifact.get("artifact_id"), "reason": str(exc)})
    requested = (manifest or {}).get("requested")
    if requested is None:
        requested = {k: (legacy or {}).get(k) for k in (
            "parser", "parser_options", "predict_options", "dpi", "all_pages", "environment")}
    return {"root": root, "legacy": legacy, "manifest": manifest, "finished": finished,
            "summary": summary, "pages": pages, "expected": expected, "problems": problems,
            "requested": clean(requested),
            "execution_status": finished.get("status") if finished else
                ("interrupted_or_unfinished" if manifest else (summary or {}).get("status", "unknown"))}


def source_diff(before, after):
    old = (before["manifest"] or {}).get("sources", {}).get("files", {})
    new = (after["manifest"] or {}).get("sources", {}).get("files", {})
    if not before["manifest"] or not after["manifest"]:
        return {"status": "unavailable", "reason": "과거 실행의 실제 소스 복사본 없음",
                "legacy_code_hash_changes": differences((before["legacy"] or {}).get("code_sha256", {}),
                                                        (after["legacy"] or {}).get("code_sha256", {})), "files": []}
    files = []
    for name in sorted(old.keys() | new.keys()):
        a, b = old.get(name), new.get(name)
        entry = {"path": name, "kind": "added" if a is None else "removed" if b is None else "unchanged"}
        contents = []
        try:
            for run, item in ((before, a), (after, b)):
                if item is None:
                    contents.append("")
                elif item.get("status") == "ok":
                    path = inside(run["root"], item["snapshot"])
                    if file_hash(path) != item["sha256"]:
                        raise ValueError("소스 복사본 해시 불일치")
                    contents.append(path.read_text(encoding="utf-8"))
                else:
                    raise ValueError("수집이 보류되거나 실패한 소스")
            if a and b and a["sha256"] != b["sha256"]:
                entry["kind"] = "changed"
            entry["status"] = "verified"
            entry["unified_diff"] = "".join(unified_diff(contents[0].splitlines(keepends=True),
                contents[1].splitlines(keepends=True), fromfile=f"before/{name}", tofile=f"after/{name}"))
        except (OSError, ValueError, KeyError, UnicodeError) as exc:
            entry.update(status="unavailable", reason=str(exc), unified_diff=None)
        files.append(entry)
    return {"status": "compared", "files": files}


def _cost(run):
    final = run["finished"] or {}
    summary = run["summary"] or {}
    return {"elapsed_seconds": final.get("elapsed_seconds", summary.get("elapsed_seconds")),
            "resources": final.get("resources"),
            "model_prediction_calls": sum(s.get("details", {}).get("kind") == "model_call"
                                          for s in (final.get("resources") or {}).get("scopes", [])) if final else None,
            "page_elapsed_seconds": [{"page": list(k), "seconds": v.get("elapsed_seconds")}
                                     for k, v in sorted(run["pages"].items())]}


def _cost_difference(before, after):
    old, new = _cost(before), _cost(after)
    result = {}
    for name, a, b in (
        ("elapsed_seconds", old["elapsed_seconds"], new["elapsed_seconds"]),
        ("model_prediction_calls", old["model_prediction_calls"], new["model_prediction_calls"]),
        ("main_observed_peak_rss_bytes", (old["resources"] or {}).get("main_observed_peak_rss_bytes"),
         (new["resources"] or {}).get("main_observed_peak_rss_bytes"))):
        result[name] = {"before": a, "after": b, "delta": b - a if a is not None and b is not None else None,
                        "comparison_kind": "observed_not_causal"}
    return result


def _models(run):
    """수집 시간·캐시 절대 경로는 모델 내용 변경과 구분한다."""
    manifest = (run["finished"] or {}).get("models")
    if manifest is None: return None
    return {"status": manifest["status"], "models": [
        {k: v for k, v in model.items() if k not in ("collection_seconds", "directory")}
        for model in manifest["models"]], "coverage": manifest.get("coverage")}


def _artifact_inventory(run):
    """실행마다 달라지는 UUID와 실제 파일 내용의 변화를 구분한다. 원본 연결은 별도 보존한다."""
    final = run["finished"] or {}
    return sorted(({k: item.get(k) for k in ("kind", "path", "sha256", "identity")}
                   for item in final.get("artifacts", [])), key=lambda item: json_bytes(item))


def compare_runs(before, after, gold=None, evaluation=None):
    keys = before["expected"] | after["expected"] | before["pages"].keys() | after["pages"].keys()
    input_rows = []; output_rows = []; blocked = set()
    for key in sorted(keys):
        a, b = before["pages"].get(key), after["pages"].get(key)
        row = {"page": list(key), "before_present": a is not None, "after_present": b is not None}
        if a and b:
            same_pdf = a.get("source_sha256") is not None and a.get("source_sha256") == b.get("source_sha256")
            row.update(same_pdf=same_pdf,
                       same_model_image=a.get("_actual_png_sha256") is not None and
                           a.get("_actual_png_sha256") == b.get("_actual_png_sha256"),
                       before_source_sha256=a.get("source_sha256"), after_source_sha256=b.get("source_sha256"))
            if not same_pdf: blocked.add(key)
        input_rows.append(row)
        outputs = []
        for page in (a, b):
            parsing = (page or {}).get("parsing", {})
            raw = parsing.get("raw") or {}
            if isinstance(raw, dict): raw = raw.get("res", raw)
            outputs.append({"status": (page or {}).get("status", "missing"),
                "parser_status": parsing.get("status"), "block_count": len(parsing.get("blocks", [])),
                "content": [{k: block.get(k) for k in ("label", "content", "bbox_pdf_points", "model_block_id")}
                            for block in parsing.get("blocks", [])],
                "observed_settings": raw.get("model_settings") if isinstance(raw, dict) else None,
                "rules": (page or {}).get("_rules"),
                "page_execution": (page or {}).get("_page_execution")})
        output_rows.append({"page": list(key), "same_source_for_quality": key not in blocked,
                            "before": outputs[0], "after": outputs[1],
                            "observed_setting_changes": differences(outputs[0]["observed_settings"], outputs[1]["observed_settings"]),
                            "output_changes": differences(outputs[0]["content"], outputs[1]["content"])})
    quality = {"status": "unmeasured", "reason": "원문 확인한 정답 파일이 전달되지 않음"}
    if gold is not None:
        if evaluation is None:
            raise ValueError("정답 평가에는 --evaluation-config가 필요합니다.")
        stages = {(r["manifest"] or {}).get("stage", "parsing") for r in (before, after)}
        if stages == {"parsing"}:
            quality = evaluate_pair(before["pages"], after["pages"], gold, evaluation)
        else:
            quality = {"status": "unmeasured", "reason": "문장·표 평가기는 parsing 단계에만 적용"}
    return {"schema_version": COMPARE_SCHEMA, "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline": str(before["root"]), "candidate": str(after["root"]),
        "stage": {"before": (before["manifest"] or {}).get("stage", "parsing"),
                  "after": (after["manifest"] or {}).get("stage", "parsing")},
        "execution_status": {"before": before["execution_status"], "after": after["execution_status"]},
        "summary": {"page_count": len(keys), "common_pages": sum(r["before_present"] and r["after_present"] for r in input_rows),
                    "different_pdf_pages": len(blocked), "quality_status": quality["status"],
                    "before_problems": len(before["problems"]), "after_problems": len(after["problems"])},
        "input_comparison": input_rows,
        "input_problems": {"before": before["problems"], "after": after["problems"]},
        "requested_setting_changes": differences(before["requested"], after["requested"]),
        "environment_changes": differences((before["manifest"] or {}).get("environment", (before["legacy"] or {}).get("environment")),
                                           (after["manifest"] or {}).get("environment", (after["legacy"] or {}).get("environment"))),
        "resolved_setting_changes": differences((before["finished"] or {}).get("resolved"), (after["finished"] or {}).get("resolved")),
        "source_comparison": source_diff(before, after),
        "model_changes": differences(_models(before), _models(after)),
        "cost": {"before": _cost(before), "after": _cost(after)},
        "cost_changes": _cost_difference(before, after),
        "artifacts": {side: (run["finished"] or {}).get("artifacts", []) for side, run in (("before", before), ("after", after))},
        "artifact_changes": differences(_artifact_inventory(before), _artifact_inventory(after)),
        "reused_inputs": {side: (run["finished"] or {}).get("input_artifacts", [])
                          for side, run in (("before", before), ("after", after))},
        "output_comparison": output_rows, "evaluation_settings": evaluation, "quality": quality}


def markdown(report):
    """동일한 JSON 값에서 보고서를 만들고 원문이 표·코드 블록 구문을 깨지 않게 한다."""
    def inline(value):
        return str(value).replace("|", "&#124;").replace("\n", " ").replace("`", "&#96;")
    lines = ["# 실행 전후 비교 보고서", "", f"기준 실행: {inline(report['baseline'])}",
             f"비교 실행: {inline(report['candidate'])}", "", "## 요약", "", "| 항목 | 값 |", "|---|---|"]
    lines += [f"| {inline(key)} | {inline(value)} |" for key, value in sorted(report["summary"].items())]
    lines += ["", "## 실행 설정 변경", "", "| 설정 경로 | 변경 종류 | 전 | 후 |", "|---|---|---|---|"]
    for item in report["requested_setting_changes"]:
        lines.append(f"| {inline('/'.join(item['path']))} | {item['kind']} | {inline(item.get('before', '(키 없음)'))} | {inline(item.get('after', '(키 없음)'))} |")
    lines += ["", "## 소스 변경", ""]
    source = report["source_comparison"]
    if source["status"] != "compared":
        lines.append(inline(source.get("reason")))
    for item in source["files"]:
        if item["kind"] == "unchanged" and item["status"] == "verified": continue
        lines += [f"### {inline(item['path'])}", "", f"{item['kind']} / {item['status']}", ""]
        if item.get("unified_diff"):
            # 역따옴표보다 긴 fence를 선택해 원문 소스가 보고서 구문을 닫지 못하게 한다.
            longest = max((len(v) for v in re.findall(r"`+", item["unified_diff"])), default=0)
            fence = "`" * max(3, longest + 1)
            lines += [fence + "diff", item["unified_diff"], fence, ""]
        elif item.get("reason"): lines.append(inline(item["reason"]))
    lines += ["", "## 품질", "", f"상태: {report['quality']['status']}", ""]
    if "reason" in report["quality"]: lines.append(inline(report["quality"]["reason"]))
    if "summary" in report["quality"]:
        lines += ["```json", json_bytes(report["quality"]["summary"]).decode().rstrip(), "```"]
    lines += ["", "## 비용·모델·입력 근거", "",
              "상세 관측값·모델 파일 목록·출력 차이·매칭 근거는 같은 폴더의 comparison.json에 보존한다.",
              "RSS 최대값은 관측 최대값이며 블록 위치 점수는 문장 위치의 정밀 검증과 구분한다.", ""]
    return "\n".join(lines)


def run_comparison(baseline, candidate, *, selection=None, gold=None, evaluation_config=None, output_dir=None):
    """CLI와 파싱 종료 훅이 같은 비교·저장 규칙을 사용한다. 입력 파일은 수정하지 않는다."""
    paths = {key: Path(value) for key, value in (("selection", selection), ("gold", gold),
              ("evaluation_config", evaluation_config)) if value is not None}
    if gold is not None and evaluation_config is None:
        raise ValueError("정답 평가에는 evaluation_config가 필요합니다.")
    selected = read_json(paths["selection"]) if selection is not None else None
    evaluation = validate_settings(read_json(paths["evaluation_config"])) if evaluation_config is not None else None
    before, after = load_run(baseline, selected), load_run(candidate, selected)
    report = compare_runs(before, after, read_json(paths["gold"]) if gold is not None else None, evaluation)
    report["input_files"] = {key: {"path": str(path.resolve()), "sha256": file_hash(path)}
                             for key, path in paths.items()}
    if output_dir is not None:
        output = Path(output_dir).resolve()
        if any(output.is_relative_to(run["root"]) for run in (before, after)):
            raise ValueError("보고서는 두 입력 실행 폴더 밖에 저장해야 합니다.")
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / REPORT_JSON, clean(report))
        with (output / REPORT_MARKDOWN).open("x", encoding="utf-8") as stream:
            stream.write(markdown(clean(report)))
    return report


def main(argv=None):
    cli = argparse.ArgumentParser(description="저장된 실행의 설정·소스·품질·비용 전후 비교")
    cli.add_argument("--baseline", type=Path, required=True)
    cli.add_argument("--candidate", type=Path, required=True)
    cli.add_argument("--selection", type=Path)
    cli.add_argument("--gold", type=Path)
    cli.add_argument("--evaluation-config", type=Path)
    cli.add_argument("--output-dir", type=Path)
    args = cli.parse_args(argv)
    if args.gold and not args.evaluation_config:
        cli.error("--gold를 사용하면 --evaluation-config도 지정해야 합니다.")
    try:
        report = run_comparison(args.baseline, args.candidate, selection=args.selection, gold=args.gold,
                                evaluation_config=args.evaluation_config, output_dir=args.output_dir)
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"비교 실행 실패: {clean(str(exc))}\n")


if __name__ == "__main__":
    raise SystemExit(main())
