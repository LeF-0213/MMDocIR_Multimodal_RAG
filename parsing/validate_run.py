"""
기존 파싱 실행 폴더를 읽고 품질 검사 보고서를 만드는 실행 모듈이다.
선정 목록의 문서·페이지만 읽는다. 목록을 지정하지 않으면 실행 폴더에 저장된 selection.json,
즉 그 실행이 실제로 쓴 목록을 쓴다. 다른 목록을 지정하면 저장된 목록과의 차이를 요약에 남겨,
검사 범위가 실행과 달라졌는데도 드러나지 않는 일을 막는다.
기본 실행은 콘솔 출력만 한다. --output 지정 시 입력 실행 폴더 밖에 새 JSON 하나를 만든다.
모델 로딩·설치·네트워크 호출·기존 파일 덮어쓰기를 하지 않는다.

원본 해시는 --pdf-dir의 원본과 대조한다. 지정하지 않으면 실행 폴더에 보관한 source.pdf 사본을
대조하며, 이 경우 사본의 무결성만 확인하고 원래 배포 파일과 같은지는 확인하지 않는다.
"""

# 표준 라이브러리: argparse는 CLI, json은 보고서를 처리한다.
# re는 블록 내용의 <img src> 참조를 찾고, sys는 실행 환경, Counter는 요약,
# datetime은 UTC 실행 시각을 기록한다.
# Path는 경로·파일 접근을, perf_counter는 검사 소요 시간 측정을 맡는다.
import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

# 내부 공통 모듈은 엄격한 JSON 읽기·해시, 검사 모듈은 페이지별 측정값·검토 항목을 맡는다.
from common import files as common_files
from common.files import file_hash, read_json
from .quality_checks import LIMITS, RULE_VERSION, inspect_page, review_status
# pdf_source는 선정 목록의 필드·번호 기준 검증을, result_store는 실행 폴더의 ID·파일 이름 규칙을 맡는다.
from . import pdf_source, result_store
from .pdf_source import load_selection
from .result_store import (PAGE_NUMBERING, SELECTION_FILE, SOURCE_PDF_FILE, document_id, document_path,
                           page_identifier, page_stem)

# 모델 출력 HTML의 그림 참조다. 속성 순서와 따옴표 종류만 허용 범위로 둔다.
# PaddleX 출력에는 alt="Image"" 같은 깨진 속성이 있어 src 값만 읽는다.
IMAGE_SOURCE = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def image_reference_findings(root, record):
    """블록 내용이 참조하는 그림이 저장 기록에 있고 파일 해시가 맞는지 검사한다."""
    root = Path(root).resolve()
    parsing = record["parsing"]
    saved = {item["reference"]: item
             for item in (parsing.get("result_images") or {}).get("items", [])}
    findings = []
    for block in parsing["blocks"]:
        for reference in sorted(set(IMAGE_SOURCE.findall(block["content"]))):
            item = saved.get(reference)
            reason, actual = None, None
            if item is None:
                reason = "저장 기록에 참조 그림이 없음"
            else:
                path = (root / item["path"]).resolve()
                if not path.is_relative_to(root):
                    reason = "실행 폴더 밖 경로"
                else:
                    try:
                        actual = file_hash(path)
                    except OSError as exc:
                        reason = str(exc)
                    else:
                        if actual != item["sha256"]:
                            reason = "그림 파일 해시 불일치"
            if reason:
                findings.append({
                    "rule": "html_image_reference", "status": "confirmed_error",
                    "element_id": block["element_id"],
                    "evidence": {"reference": reference, "reason": reason,
                                 "expected": item and item.get("sha256"), "actual": actual},
                })
    return findings


def selection_pages(selection):
    """선정 목록의 (문서명, 페이지) 집합이다. 문서·페이지 순서와 JSON 공백 차이는 무시한다."""
    return {(item["document_name"], number) for item in selection["documents"]
            for number in item["page_numbers"]}


def compare_saved_selection(root, selection_path, selection):
    """검사에 쓴 목록을 실행 당시 저장한 목록과 비교한다. 어느 쪽이 맞는지는 판정하지 않는다."""
    saved_path = root / SELECTION_FILE
    if Path(selection_path).resolve() == saved_path.resolve():
        return {"status": "run_saved"}
    if not saved_path.is_file():
        return {"status": "no_saved_selection"}
    try:
        saved = selection_pages(load_selection(saved_path))
    except (OSError, ValueError) as exc:
        return {"status": "unreadable", "reason": str(exc)}
    given = selection_pages(selection)
    # 저장할 때 들여쓰기·키 순서가 바뀔 수 있어 파일 해시가 아닌 문서·페이지 내용으로 비교한다.
    return {"status": "matched" if given == saved else "different",
            "only_in_given": [list(page) for page in sorted(given - saved)],
            "only_in_saved": [list(page) for page in sorted(saved - given)]}


def inspect_run(root, selection_path, pdf_dir=None):
    """지정한 선정 목록의 페이지를 검사하고 원본 참조·해시를 남긴다.

    pdf_dir가 None이면 실행 폴더의 documents/<문서 ID>/source.pdf 사본을 대조한다.
    """
    if not root.is_dir():
        raise ValueError("파싱 실행 폴더가 존재하지 않습니다.")
    selection = read_json(selection_path)
    if not isinstance(selection, dict):
        raise ValueError("선정 목록은 객체여야 합니다.")
    if selection.get("page_numbering") != PAGE_NUMBERING:
        raise ValueError("선정 목록의 페이지 번호 기준을 확인해야 합니다.")
    documents = selection["documents"]
    if not isinstance(documents, list) or not documents:
        raise ValueError("선정 문서 목록이 비어 있거나 잘못됐습니다.")
    results = []
    seen_documents, seen_elements = set(), set()
    for document in documents:
        name = document["document_name"]
        numbers = document["page_numbers"]
        if (not isinstance(name, str) or not name or Path(name).name != name or name in (".", "..")
                or "\\" in name or "/" in name):
            raise ValueError("문서명은 경로가 아닌 파일명이어야 합니다.")
        if name in seen_documents:
            raise ValueError("선정 목록에 같은 문서가 중복됐습니다.")
        seen_documents.add(name)
        if (not isinstance(numbers, list) or not numbers
                or any(type(n) is not int or n < 1 for n in numbers)
                or len(numbers) != len(set(numbers))):
            raise ValueError("페이지 번호에는 중복 없는 양의 정수가 필요합니다.")
        doc_id = document_id(name)
        source_path = (pdf_dir / f"{name}.pdf" if pdf_dir is not None
                       else root / document_path(doc_id, SOURCE_PDF_FILE))
        try:
            source_hash = file_hash(source_path)
            source_error = None
        except OSError as exc:
            source_hash, source_error = None, str(exc)

        for number in sorted(numbers):
            path = root / document_path(doc_id, f"{page_stem(number)}.json")
            page_id = page_identifier(doc_id, number)
            report = {
                "page_id": page_id, "processing_status": None,
                "review_status": "needs_review", "block_count": None,
                "tables": [], "findings": [],
            }
            try:
                record = read_json(path)
                report = inspect_page(record)
                report["input_json_sha256"] = file_hash(path)
                expected_png = path.with_suffix(".png").relative_to(root).as_posix()
                if record["page_image"]["path"] != expected_png:
                    report["findings"].append({
                        "rule": "image_reference", "status": "confirmed_error",
                        "element_id": None,
                        "evidence": {"expected": expected_png,
                                     "actual": record["page_image"]["path"]},
                    })
                ids = {b["element_id"] for b in record["parsing"]["blocks"]}
                for eid in sorted(ids & seen_elements):
                    report["findings"].append({
                        "rule": "duplicate_id", "status": "confirmed_error",
                        "element_id": eid,
                        "evidence": {"reason": "다른 페이지에서도 사용된 ID"},
                    })
                seen_elements.update(ids)
                report["findings"].extend(image_reference_findings(root, record))
                expected = {
                    "document_name": name, "document_id": doc_id,
                    "page_number": number, "page_id": page_id,
                    "page_numbering": PAGE_NUMBERING,
                    "page_index": number - 1,
                }
                for key, value in expected.items():
                    if record.get(key) != value:
                        report["findings"].append({
                            "rule": "page_identity", "status": "confirmed_error",
                            "element_id": None,
                            "evidence": {"field": key, "expected": value,
                                         "actual": record.get(key)},
                        })
                for label, actual, expected_hash, error in [
                    ("source_hash", source_hash, record.get("source_sha256"), source_error),
                    ("image_hash", None, record["page_image"]["sha256"], None),
                ]:
                    if label == "image_hash":
                        try:
                            actual = file_hash(path.with_suffix(".png"))
                        except OSError as exc:
                            error = str(exc)
                    report[label] = actual
                    if actual is None or actual != expected_hash:
                        report["findings"].append({
                            "rule": label,
                            "status": "unresolved" if actual is None else "confirmed_error",
                            "element_id": None,
                            "evidence": {"expected": expected_hash, "actual": actual,
                                         "reason": error},
                        })
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                report["findings"].append({
                    "rule": "page_inspection", "status": "unresolved",
                    "element_id": None,
                    "evidence": {"reason": str(exc)},
                })
            report.update({
                "expected_page_id": page_id,
                "document_name": name, "page_number": number,
                "input_json": str(path),
                "review_status": review_status(report["findings"]),
            })
            results.append(report)
    counts = Counter(
        item["rule"] for page in results for item in page["findings"]
    )
    statuses = Counter(page["review_status"] for page in results)
    return {
        "schema_version": "project.parsing_quality.v1",
        "rule_version": RULE_VERSION, "limits": dict(LIMITS),
        "input_run": str(root), "selection": str(selection_path),
        "selection_sha256": file_hash(selection_path),
        "pdf_directory": str(pdf_dir) if pdf_dir is not None else None,
        # run_copy는 실행 당시 보관한 사본과의 대조라 원본 배포 파일의 동일성 확인이 아니다.
        "source_reference": "pdf_directory" if pdf_dir is not None else "run_copy",
        "summary": {
            "page_count": len(results),
            # run_saved는 실행 폴더의 목록으로 검사했다는 뜻이다. different면 검사 범위가 실행과 다르다.
            "selection_check": compare_saved_selection(root, selection_path, selection),
            # 의심·확정 오류가 있는 페이지와 확인하지 못한 항목만 있는 페이지를 따로 센다.
            "pages_needing_review": statuses["needs_review"],
            "pages_unresolved": statuses["unresolved"],
            "finding_counts": dict(counts),
            "quality_accuracy": None,
        },
        "pages": results,
    }


def main():
    cli = argparse.ArgumentParser(description="저장된 파싱 결과의 규칙 검사")
    cli.add_argument("--run-dir", type=Path, required=True)
    # 기본값을 코드에 경로로 두지 않고, 실행 폴더에 저장된 그 실행의 목록을 쓴다.
    cli.add_argument("--selection", type=Path,
                     help="검사할 선정 목록. 생략 시 실행 폴더의 selection.json(실행 당시 목록)")
    # 원본 PDF 위치는 환경마다 다르므로 기본 경로를 두지 않는다. 생략하면 실행 폴더의 사본을 쓴다.
    cli.add_argument("--pdf-dir", type=Path, help="원본 PDF 폴더. 생략 시 실행 폴더의 source.pdf 사본과 대조")
    cli.add_argument("--output", type=Path)
    args = cli.parse_args()
    root = args.run_dir.resolve()
    if args.output and args.output.resolve().is_relative_to(root):
        cli.error("검사 보고서는 입력 실행 폴더 밖에 저장해야 합니다.")
    selection_path = (args.selection or root / SELECTION_FILE).resolve()
    if not selection_path.is_file():
        cli.error(f"선정 목록이 없습니다: {selection_path}. 실행 폴더에 {SELECTION_FILE}이 없으면 --selection으로 지정하세요.")
    started = perf_counter()
    try:
        report = inspect_run(root, selection_path,
                             args.pdf_dir.resolve() if args.pdf_dir else None)
        report["checked_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["python"] = sys.version
        report["elapsed_seconds"] = perf_counter() - started
        report["code_sha256"] = {
            path.name: file_hash(path)
            for path in (Path(__file__), Path(__file__).with_name("quality_checks.py"),
                         Path(pdf_source.__file__), Path(result_store.__file__), Path(common_files.__file__))
        }
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            # x는 파일이 존재하면 실패한다. 부모 폴더를 자동 생성하지 않는다.
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(serialized + "\n")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        if args.output:
            print(f"보고서 저장: {args.output}")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        cli.exit(2, f"검사 실행 실패: {exc}\n")


if __name__ == "__main__":
    main()
