"""중단된 파싱 실행 폴더를 이어서 처리하는 재개 모듈이다.

전체 위치: PDF → 파싱〔중단 실행 재개〕 → 원본 대조·품질 검사 → 청킹
입력: 중단된 실행 폴더(run.json·selection.json·documents/…)와 원본 PDF 위치.
출력: 같은 실행 폴더에 남은 페이지 결과·검토 PDF·document.json·summary.json을 채운다.
재개 기록은 resume/segment_<번호>/ 아래에 남기고, 끝나지 않은 페이지의 잔여 파일은
지우지 않고 같은 세그먼트의 partial/ 아래로 옮긴다.

설계 이유
- 기존 parsing/·common/ 파일을 고치지 않고 이 파일만 추가했다. 그래서 중단 전 run.json의
  code_sha256과 현재 코드 해시를 파일별로 비교했을 때 "이 모듈만 추가됐다"는 사실로
  페이지를 만든 코드가 같음을 확인할 수 있다. 대신 run.py의 문서·실행 반복문과 비슷한 코드를
  이 파일에 다시 두었다. 페이지 처리(process_page)·저장(save_page)은 run.py 함수를 그대로 호출한다.
- 설정은 현재 설정 파일이 아니라 중단된 실행의 run.json에서 복원한다. 파서·옵션·DPI·선정 목록·
  패키지 버전·코드가 다르면 재개를 거부한다. 원본 PDF 위치만 바꿀 수 있고, 내용은 해시로 대조한다.
- 이미 저장된 failed 페이지·문서도 실행 결과로 보고 다시 처리하지 않는다.

실행 부작용: 원본 PDF 읽기(로컬·HTTP), 모델 로딩·캐시, 실행 폴더 안의 새 파일 생성과
잔여 파일 이동이 발생한다. plan_resume()·restore_arguments()·check_compatibility()는 읽기만 한다.
실험 기록·비교는 모르며 experiments.resume_stage가 기록기를 인자로 넘겨 연결한다.
"""

# 표준 라이브러리: argparse.Namespace는 run.py 함수가 받는 실행 인자 형태를 복원한다.
# Counter는 상태별 페이지 수, datetime·timezone은 UTC 기록, Path는 경로, perf_counter는 경과 시간이다.
import argparse
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

# 내부 공통 모듈: digest·file_hash는 원본·결과 파일 동일성, inside는 기록된 상대 경로가
# 실행 폴더 밖으로 나가지 않는지 확인, read_json은 엄격한 JSON 읽기를 맡는다.
# clean·diagnostic_clean은 저장 전 민감정보 제거다.
from common.files import digest, file_hash, inside, read_json
from common.redaction import clean, diagnostic_clean

# 내부 파싱 모듈: pdf_source는 선정 목록 검증, result_store는 배타적 저장을 맡는다.
# run은 페이지 처리·저장 함수와 실행 환경·코드 해시 조회를 제공한다. 같은 함수를 써야
# 재개한 페이지가 처음 실행한 페이지와 같은 규칙으로 만들어진다.
from .pdf_source import load_selection
from .result_store import save_bytes, save_json
from .run import (DocumentContext, environment_record, measure, process_page, read_source,
                  save_page, source_hashes)

# 아래 이름은 실행 폴더 저장 형식의 정의다. run.py가 같은 이름으로 파일을 만든다.
RUN_SCHEMA = "project.parsing.v1"
RUN_FILE = "run.json"
SUMMARY_FILE = "summary.json"
SELECTION_FILE = "selection.json"
DOCUMENT_FILE = "document.json"
SOURCE_FILE = "source.pdf"
DOCUMENTS_DIRECTORY = "documents"
# 재개 기록 형식의 정의다. 최초 실행을 1번 세그먼트로 세고 재개는 2번부터 만든다.
RESUME_SCHEMA = "project.parsing_resume.v1"
RESUME_DIRECTORY = "resume"
SEGMENT_PREFIX = "segment_"
SEGMENT_START_FILE = "segment.json"
SEGMENT_FINISH_FILE = "segment_finished.json"
PARTIAL_DIRECTORY = "partial"
FIRST_SEGMENT = 1
# 저장된 페이지가 가질 수 있는 최종 상태다. failed도 실행 결과이므로 재사용한다.
FINAL_PAGE_STATUSES = frozenset({"ok", "empty", "partial", "failed"})
# 중단 전 실행에 없던 코드 파일 중 허용하는 것은 이 모듈뿐이다.
RESUME_MODULE = Path(__file__).name


class ResumeError(ValueError):
    """재개 조건을 만족하지 않는다. 결과를 섞지 않도록 처리를 시작하거나 계속하지 않는다."""


def page_stem(number: int) -> str:
    """run.py와 같은 페이지 파일 이름 규칙이다."""
    return f"page_{number:05d}"


def restore_arguments(root: Path, *, pdf_directory: Path | None = None,
                      base_url: str | None = None) -> tuple[argparse.Namespace, dict, dict]:
    """중단된 실행의 run.json·selection.json에서 실행 인자를 복원한다. 파일은 읽기만 한다."""
    root = Path(root).resolve()
    metadata = read_json(root / RUN_FILE)
    if not isinstance(metadata, dict) or metadata.get("schema_version") != RUN_SCHEMA:
        raise ResumeError("지원하는 파싱 실행 폴더가 아닙니다(run.json 스키마 확인).")
    summary_path = root / SUMMARY_FILE
    if summary_path.is_file():
        summary = read_json(summary_path)
        # 중단(KeyboardInterrupt 등)·치명 오류로 끝난 실행만 이어서 처리한다.
        # 정상 종료한 실행을 재개하면 끝난 결과를 다른 세그먼트와 섞게 된다.
        if not isinstance(summary, dict) or not summary.get("fatal_error"):
            raise ResumeError("이미 종료된 실행입니다. 새 실행 폴더로 다시 실행하세요.")
    selection = load_selection(root / SELECTION_FILE)
    parser = metadata.get("parser")
    options = metadata.get("parser_options") or {}
    if parser not in ("vl", "structure"):
        raise ResumeError(f"알 수 없는 파서입니다: {parser!r}")
    if pdf_directory is not None and base_url is not None:
        raise ResumeError("원본 위치는 로컬 폴더와 HTTP 주소 중 하나만 지정합니다.")
    if pdf_directory is None and base_url is None:
        # 과거 실행에는 source_kind가 없을 수 있다. 그때는 기록된 HTTP 주소를 쓴다.
        if metadata.get("source_kind") == "local":
            pdf_directory = Path(metadata["pdf_directory"])
        else:
            base_url = metadata.get("base_url")
        if pdf_directory is None and not base_url:
            raise ResumeError("기록에서 원본 위치를 찾지 못했습니다. 원본 위치를 직접 지정하세요.")
    args = argparse.Namespace(
        selection=root / SELECTION_FILE, base_url=base_url, pdf_directory=pdf_directory,
        output_dir=root, parser=parser, device=options.get("device"),
        cpu_threads=options.get("cpu_threads"), dpi=metadata.get("dpi"),
        formula_model=options.get("formula_recognition_model_name") if parser == "structure" else None,
        timeout=metadata.get("http_timeout_seconds"), all_pages=bool(metadata.get("all_pages")))
    return args, metadata, selection


def check_compatibility(args: argparse.Namespace, metadata: dict, *,
                        pipeline_overrides: dict | None = None,
                        predict_overrides: dict | None = None) -> dict:
    """현재 설정·코드·패키지가 중단 전과 같은지 확인한다. 다르면 ResumeError를 낸다."""
    if args.parser == "vl":
        from . import vl_parser as adapter
        options = adapter.parser_options(args.device, args.cpu_threads)
    else:
        from . import structure_parser as adapter
        options = adapter.parser_options(args.device, args.formula_model, args.cpu_threads)
    # run_parsing()과 같은 순서로 override를 합쳐야 기록된 설정과 비교할 수 있다.
    options.update(pipeline_overrides or {})
    predict = adapter.predict_options()
    predict.update(predict_overrides or {})

    problems = []
    if options != metadata.get("parser_options"):
        problems.append("parser_options가 다릅니다.")
    if predict != metadata.get("predict_options"):
        problems.append("predict_options가 다릅니다.")

    # 코드 해시는 파일별로 비교한다. 기록된 파일이 모두 같고 추가된 파일이 이 모듈뿐이면
    # 페이지를 만든 코드가 같다고 판단한다. 해시를 새로 덮어쓰거나 옛 값을 복사하지 않는다.
    recorded = metadata.get("code_sha256") or {}
    current = source_hashes()
    changed = sorted(name for name in recorded if current.get(name) != recorded[name])
    added = sorted(set(current) - set(recorded))
    unexpected = [name for name in added if name != RESUME_MODULE]
    if not recorded:
        problems.append("중단 전 코드 해시 기록이 없습니다.")
    if changed:
        problems.append(f"중단 전과 다른 코드 파일: {changed}")
    if unexpected:
        problems.append(f"재개 모듈 외에 추가된 코드 파일: {unexpected}")

    environment = environment_record()
    before = (metadata.get("environment") or {})
    package_changes = {name: {"before": (before.get("packages") or {}).get(name), "after": value}
                       for name, value in environment["packages"].items()
                       if (before.get("packages") or {}).get(name) != value}
    if package_changes:
        problems.append(f"패키지 버전이 다릅니다: {package_changes}")
    if before.get("python") != environment["python"]:
        problems.append("Python 버전·빌드가 다릅니다.")
    if problems:
        raise ResumeError("재개할 수 없습니다. " + " ".join(problems))
    return {"code": {"recorded_files": len(recorded), "changed": changed, "added": added},
            "packages": "same", "python": "same",
            "platform": {"before": before.get("platform"), "after": environment["platform"]},
            "parser_options": "same", "predict_options": "same"}


def inspect_saved_page(root: Path, folder: Path, doc_id: str, number: int,
                       source_sha256: str | None) -> tuple[dict | None, str | None]:
    """저장된 페이지 결과를 재사용할 수 있는지 확인한다. (기록, None) 또는 (None, 이유)를 반환한다."""
    path = folder / f"{page_stem(number)}.json"
    if not path.is_file():
        return None, "missing"
    try:
        record = read_json(path)
    except (OSError, UnicodeError, ValueError) as exc:
        # 쓰는 도중 끊긴 JSON은 여기서 걸린다. 내용을 고치지 않고 다시 처리한다.
        return None, f"unreadable_json: {type(exc).__name__}"
    if not isinstance(record, dict):
        return None, "not_object"
    if record.get("page_id") != f"{doc_id}:p{number}" or record.get("page_number") != number:
        return None, "id_mismatch"
    if source_sha256 is not None and record.get("source_sha256") != source_sha256:
        return None, "source_hash_mismatch"
    if record.get("status") not in FINAL_PAGE_STATUSES:
        return None, f"unknown_status: {record.get('status')!r}"
    referenced = []
    if (record.get("page_image") or {}).get("path"):
        referenced.append(record["page_image"])
    referenced += ((record.get("parsing") or {}).get("result_images") or {}).get("items") or []
    for item in referenced:
        try:
            target = inside(root, item["path"])
            if not target.is_file() or file_hash(target) != item.get("sha256"):
                return None, f"file_hash_mismatch: {item['path']}"
        except (KeyError, TypeError, ValueError, OSError) as exc:
            return None, f"file_reference_error: {type(exc).__name__}"
    return record, None


def page_leftovers(folder: Path, number: int) -> list[Path]:
    """끝나지 않은 페이지가 남겼을 수 있는 파일이다. PNG는 모델 추론 전에 저장된다."""
    stem = page_stem(number)
    candidates = (folder / f"{stem}.json", folder / f"{stem}.png", folder / stem)
    return [path for path in candidates if path.exists()]


def set_aside(paths: list[Path], root: Path, partial: Path) -> list[dict]:
    """잔여 파일을 지우지 않고 세그먼트의 partial/ 아래 같은 상대 경로로 옮긴다."""
    moves = []
    for path in paths:
        relative = path.resolve().relative_to(root)
        target = partial / relative
        if target.exists():
            raise ResumeError(f"옮길 위치에 파일이 이미 있습니다: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        # rename은 같은 볼륨 안에서 내용을 복사하지 않고 이름만 바꾼다. 원본 bytes는 그대로다.
        path.rename(target)
        moves.append({"from": relative.as_posix(), "to": target.relative_to(root).as_posix()})
    return moves


def page_summary(context: DocumentContext, number: int, record: dict) -> dict:
    """run.save_page()가 반환하는 문서 요약 항목과 같은 형태를 저장 없이 만든다."""
    return {
        "page_number": number,
        "page_id": record["page_id"],
        "status": record["status"],
        "json_path": f"{DOCUMENTS_DIRECTORY}/{context.doc_id}/{page_stem(number)}.json",
        "native_status": (record.get("native") or {}).get("status"),
        "parser_status": (record.get("parsing") or {}).get("status"),
    }


def register_reused_page(recorder, root: Path, folder: Path, number: int, record: dict,
                         source_artifact: str | None) -> None:
    """재사용한 페이지 파일을 새 세그먼트의 결과물 목록에 해시와 함께 등록한다."""
    if not recorder:
        return
    page_id = record["page_id"]
    parents = [p for p in (source_artifact,) if p]
    image_artifact = None
    if (record.get("page_image") or {}).get("path"):
        image_artifact = recorder.artifact(inside(root, record["page_image"]["path"]), "page_image",
                                           parents=parents, page_id=page_id, resume_reused=True)
    for item in ((record.get("parsing") or {}).get("result_images") or {}).get("items") or []:
        recorder.artifact(inside(root, item["path"]), "parsing_result_image",
                          parents=[p for p in (image_artifact,) if p], page_id=page_id, resume_reused=True)
    recorder.artifact(folder / f"{page_stem(number)}.json", "parsing_page",
                      parents=[p for p in (source_artifact, image_artifact) if p],
                      page_id=page_id, resume_reused=True)


def rebuild_review_page(context: DocumentContext, number: int, document, review, record: dict) -> dict | None:
    """저장된 블록으로 검토 PDF 페이지를 다시 만든다. 저장된 review 기록과 다르면 차이를 반환한다."""
    from .review_pdf import add_review_page

    if number > len(document):
        return None
    rebuilt = add_review_page(review, document, number,
                              {"status": record["status"],
                               "blocks": (record.get("parsing") or {}).get("blocks", [])},
                              f"{DOCUMENTS_DIRECTORY}/{context.doc_id}/{page_stem(number)}.json")
    saved = record.get("review") or {}
    keys = ("review_page_number", "status", "drawn_count", "warnings")
    if any(rebuilt.get(key) != saved.get(key) for key in keys):
        # 페이지 JSON은 고치지 않는다. 검토 PDF와 기록의 차이를 문서 재개 기록에 남긴다.
        return {"page_number": number, "saved": {k: saved.get(k) for k in keys},
                "rebuilt": {k: rebuilt.get(k) for k in keys}}
    return None


def verify_completed_document(entry: dict, root: Path, folder: Path, doc_id: str, recorder) -> None:
    """document.json까지 저장된 문서를 검증하고 결과물로 등록한다. 손상이 있으면 거부한다."""
    if not isinstance(entry, dict) or entry.get("document_id") != doc_id:
        raise ResumeError(f"완료 문서 기록의 ID가 다릅니다: {doc_id}")
    source_sha256 = entry.get("source_sha256")
    source = folder / SOURCE_FILE
    if source_sha256 and (not source.is_file() or file_hash(source) != source_sha256):
        raise ResumeError(f"완료 문서의 원본 사본이 없거나 해시가 다릅니다: {doc_id}")
    review = entry.get("review_pdf")
    if review and not inside(root, review).is_file():
        raise ResumeError(f"완료 문서의 검토 PDF가 없습니다: {review}")
    records = []
    for page in entry.get("pages", []):
        number = page.get("page_number")
        record, reason = inspect_saved_page(root, folder, doc_id, number, source_sha256)
        if record is None:
            raise ResumeError(f"완료 문서의 페이지 결과가 손상됐습니다: {doc_id} p{number} ({reason})")
        records.append((number, record))
    if recorder:
        source_artifact = (recorder.artifact(source, "source_pdf", source_sha256=source_sha256, resume_reused=True)
                           if source_sha256 else None)
        for number, record in records:
            register_reused_page(recorder, root, folder, number, record, source_artifact)
        if review:
            recorder.artifact(inside(root, review), "review_pdf", parents=[p for p in (source_artifact,) if p],
                              document_id=doc_id, resume_reused=True)


def resume_document(item: dict, args: argparse.Namespace, root: Path, adapter, parser, recorder,
                    predict_overrides: dict | None, partial: Path, secrets: tuple) -> tuple[dict, dict]:
    """문서 하나를 이어서 처리한다. 저장된 유효 페이지는 재사용하고 나머지만 모델로 처리한다."""
    from .pdf_parser import error_info, open_pdf

    # PyMuPDF는 검토용 빈 문서를 만들고 원본 페이지 복사본을 관리한다.
    import pymupdf

    name = item["document_name"]
    doc_id = digest(name.encode("utf-8"))
    folder = root / DOCUMENTS_DIRECTORY / doc_id
    report = {"document_id": doc_id, "mode": "resumed_document", "reused_pages": [],
              "processed_pages": [], "invalid_pages": [], "set_aside": [], "review_mismatches": []}

    if (folder / DOCUMENT_FILE).is_file():
        entry = read_json(folder / DOCUMENT_FILE)
        verify_completed_document(entry, root, folder, doc_id, recorder)
        report.update(mode="reused_document", reused_pages=[p["page_number"] for p in entry.get("pages", [])])
        return entry, report

    folder.mkdir(parents=True, exist_ok=True)
    entry = {
        "document_id": doc_id,
        "id_origin": "project_generated_name_sha256",
        "document_name": name,
        "status": "failed",
        "pages": [],
        "errors": [],
    }
    stage = "source_read"
    try:
        data = read_source(entry, args, recorder)
        entry["source_sha256"] = digest(data)
        entry["source_pdf"] = f"{DOCUMENTS_DIRECTORY}/{doc_id}/{SOURCE_FILE}"
        saved_source = folder / SOURCE_FILE
        if saved_source.is_file():
            # 중단 전과 다른 원본으로 남은 페이지를 처리하면 한 문서에 두 원본이 섞인다.
            if file_hash(saved_source) != entry["source_sha256"]:
                raise ResumeError(f"원본 PDF가 중단 전과 다릅니다: {name}")
        else:
            save_bytes(saved_source, data)
        source_artifact = (recorder.artifact(saved_source, "source_pdf", source_sha256=entry["source_sha256"])
                           if recorder else None)

        stage = "open_pdf"
        with open_pdf(data) as document, pymupdf.open() as review:
            entry["source_page_count"] = len(document)
            page_numbers = (list(range(1, len(document) + 1)) if args.all_pages
                            else sorted(item["page_numbers"]))
            entry["requested_page_numbers"] = page_numbers
            entry["review_pdf"] = f"{DOCUMENTS_DIRECTORY}/{doc_id}/review_{args.parser}.pdf"
            context = DocumentContext(args, adapter, parser, recorder, predict_overrides, folder, doc_id, name,
                                      entry["source_sha256"], source_artifact, entry["review_pdf"])

            for number in page_numbers:
                stage = "page_inventory"
                record, reason = inspect_saved_page(root, folder, doc_id, number, entry["source_sha256"])
                if record is not None:
                    stage = "page_reuse"
                    mismatch = rebuild_review_page(context, number, document, review, record)
                    if mismatch:
                        report["review_mismatches"].append(mismatch)
                    register_reused_page(recorder, root, folder, number, record, source_artifact)
                    entry["pages"].append(page_summary(context, number, record))
                    report["reused_pages"].append(number)
                    print(f"[{args.parser}] {name} p{number}: reused ({record['status']})", flush=True)
                    continue
                if reason != "missing":
                    report["invalid_pages"].append({"page_number": number, "reason": reason})
                stage = "set_aside"
                report["set_aside"] += set_aside(page_leftovers(folder, number), root, partial)
                record, image_artifact = process_page(context, number, document, review)
                stage = "page_result_save"
                entry["pages"].append(save_page(context, number, record, image_artifact))
                report["processed_pages"].append(number)
                print(f"[{args.parser}] {name} p{number}: {record['status']}", flush=True)

            stage = "review_save"
            review_path = folder / f"review_{args.parser}.pdf"
            if review_path.exists():
                # 검토 PDF 저장 뒤 document.json 전에 끊긴 경우다. 이전 파일은 옮겨 보존한다.
                report["set_aside"] += set_aside([review_path], root, partial)
            if len(review):
                save_bytes(review_path, review.tobytes(garbage=3, deflate=True))
                if recorder:
                    recorder.artifact(review_path, "review_pdf", parents=[p for p in (source_artifact,) if p],
                                      document_id=doc_id)
            else:
                entry["review_pdf"] = None
            entry["status"] = ("ok" if entry["pages"] and all(p["status"] in ("ok", "empty") for p in entry["pages"])
                               else "partial")
    except ResumeError:
        # 재개 조건 위반은 문서 오류로 숨기지 않고 실행 전체를 멈춘다.
        raise
    except Exception as exc:
        entry["errors"].append({"stage": stage, **error_info(exc)})
        entry["status"] = "failed"

    entry["resume"] = report
    entry = diagnostic_clean(entry, secrets)
    save_json(folder / DOCUMENT_FILE, entry)
    return entry, report


def next_segment(root: Path) -> tuple[int, Path]:
    """새 세그먼트 번호와 폴더를 정한다. 폴더 생성은 호출부가 배타적으로 한다."""
    base = root / RESUME_DIRECTORY
    numbers = []
    if base.is_dir():
        for path in base.iterdir():
            suffix = path.name.removeprefix(SEGMENT_PREFIX)
            if path.is_dir() and path.name.startswith(SEGMENT_PREFIX) and suffix.isdigit():
                numbers.append(int(suffix))
    number = max(numbers, default=FIRST_SEGMENT) + 1
    return number, base / f"{SEGMENT_PREFIX}{number}"


def plan_resume(args: argparse.Namespace, selection: dict, root: Path) -> list[dict]:
    """파일을 바꾸지 않고 문서·페이지별로 재사용·재처리 예정을 보여 준다(--check 용)."""
    root = Path(root).resolve()
    plan = []
    for item in selection["documents"]:
        doc_id = digest(item["document_name"].encode("utf-8"))
        folder = root / DOCUMENTS_DIRECTORY / doc_id
        entry = {"document_name": item["document_name"], "document_id": doc_id}
        if (folder / DOCUMENT_FILE).is_file():
            entry["action"] = "reuse_document"
            plan.append(entry)
            continue
        source = folder / SOURCE_FILE
        source_sha256 = file_hash(source) if source.is_file() else None
        entry["action"] = "resume_document"
        entry["saved_source"] = source_sha256 is not None
        if args.all_pages:
            # 전체 페이지 수는 원본을 열어야 안다. 확인 단계에서는 원본을 읽지 않는다.
            entry["pages"] = "all_pages: 원본을 연 뒤 결정"
            plan.append(entry)
            continue
        pages = []
        for number in sorted(item["page_numbers"]):
            record, reason = (inspect_saved_page(root, folder, doc_id, number, source_sha256)
                              if source_sha256 else (None, "missing_source_copy"))
            pages.append({"page_number": number, "action": "reuse" if record else "process",
                          "reason": reason, "leftovers": [p.name for p in page_leftovers(folder, number)]
                          if record is None else []})
        entry["pages"] = pages
        plan.append(entry)
    return plan


def resume_parsing(args: argparse.Namespace, selection: dict, metadata: dict, compatibility: dict, *,
                   pipeline_overrides: dict | None = None, predict_overrides: dict | None = None,
                   start_recorder=None, secrets: tuple = (), root_files_to_set_aside: tuple = ()) -> dict:
    """중단된 실행을 이어서 처리하고 summary.json을 저장한 뒤 결과를 반환한다.

    start_recorder(root, metadata, record_dir)는 세그먼트 기록을 시작하는 기록기를 반환한다.
    root_files_to_set_aside는 호출부(실험 기록)가 소유한 이전 종료 파일 이름이다.
    """
    root = Path(args.output_dir).resolve()
    adapter_name = "vl_parser" if args.parser == "vl" else "structure_parser"
    if args.parser == "vl":
        from . import vl_parser as adapter
    else:
        from . import structure_parser as adapter

    number, segment = next_segment(root)
    # exist_ok=False로 동시에 두 재개가 같은 세그먼트를 쓰지 못하게 한다.
    segment.mkdir(parents=True, exist_ok=False)
    partial = segment / PARTIAL_DIRECTORY
    previous = [root / name for name in (SUMMARY_FILE, *root_files_to_set_aside) if (root / name).is_file()]
    set_aside_root = set_aside(previous, root, partial)

    started = perf_counter()
    segment_metadata = {
        "schema_version": RESUME_SCHEMA,
        "segment": number,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "adapter": adapter_name,
        "environment": environment_record(),
        "code_sha256": source_hashes(),
        "compatibility": compatibility,
        "source_kind": "local" if args.pdf_directory is not None else "http",
        "pdf_directory": str(Path(args.pdf_directory).resolve()) if args.pdf_directory is not None else None,
        "base_url": args.base_url,
        "set_aside": set_aside_root,
        "original_started_at_utc": metadata.get("started_at_utc"),
    }
    save_json(segment / SEGMENT_START_FILE, clean(segment_metadata, secrets))
    recorder = start_recorder(root, segment_metadata, segment) if start_recorder else None

    entries, reports = [], []
    fatal = interrupted = None
    model_load_seconds = None
    init_started = perf_counter()
    try:
        with measure(recorder, "model_load"):
            build_kwargs = {"overrides": pipeline_overrides} if pipeline_overrides is not None else {}
            if args.parser == "vl":
                parser = adapter.build_parser(args.device, args.cpu_threads, **build_kwargs)
            else:
                parser = adapter.build_parser(args.device, args.formula_model, args.cpu_threads, **build_kwargs)
        model_load_seconds = perf_counter() - init_started
        if recorder:
            recorder.capture_parser(parser)
        for item in selection["documents"]:
            with measure(recorder, "document_processing", document_name=item["document_name"]) as scope:
                entry, report = resume_document(item, args, root, adapter, parser, recorder,
                                                predict_overrides, partial, secrets)
                entries.append(entry)
                reports.append(report)
                if recorder:
                    scope["status"] = entry["status"]
    except BaseException as exc:
        if not isinstance(exc, Exception):
            interrupted = exc
        fatal = {"type": type(exc).__name__, "message": clean(str(exc), secrets)}
        if model_load_seconds is None:
            model_load_seconds = perf_counter() - init_started

    counts = Counter(p["status"] for d in entries for p in d["pages"])
    success = (fatal is None and len(entries) == len(selection["documents"])
               and all(d["status"] == "ok" for d in entries))
    resume_info = {
        "segment": number,
        "segment_record": segment.relative_to(root).as_posix(),
        "reused_documents": sum(r["mode"] == "reused_document" for r in reports),
        "reused_pages": sum(len(r["reused_pages"]) for r in reports),
        "processed_pages": sum(len(r["processed_pages"]) for r in reports),
        "invalid_pages": sum(len(r["invalid_pages"]) for r in reports),
        "set_aside_files": len(set_aside_root) + sum(len(r["set_aside"]) for r in reports),
        "review_mismatches": sum(len(r["review_mismatches"]) for r in reports),
    }
    summary = {
        "status": "completed" if success else "completed_with_errors",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        # 경과 시간은 이번 세그먼트만이다. 중단 전 구간은 측정 기록이 없어 더하지 않는다.
        "elapsed_seconds": perf_counter() - started,
        "elapsed_scope": "current_resume_segment",
        "model_load_seconds": model_load_seconds,
        "parser": args.parser,
        "document_count": len(entries),
        "page_status_counts": dict(counts),
        "documents": entries,
        "fatal_error": fatal,
        "quality_evaluation": "not_run",
        "resume": resume_info,
    }
    save_json(root / SUMMARY_FILE, clean(summary, secrets))
    save_json(segment / SEGMENT_FINISH_FILE, clean({"schema_version": RESUME_SCHEMA, "segment": number,
        "status": summary["status"], "finished_at_utc": summary["finished_at_utc"],
        "elapsed_seconds": summary["elapsed_seconds"], "model_load_seconds": model_load_seconds,
        "fatal_error": fatal, "documents": reports}, secrets))
    return {"root": root, "summary": summary, "success": success, "interrupted": interrupted,
            "segment": number, "segment_dir": segment}
