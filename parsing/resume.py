"""중단된 파싱 실행을 같은 폴더에서 이어서 처리할 때 필요한 확인·재사용 규칙을 모은 모듈이다.

전체 위치: PDF → 파싱〔중단 실행 재개〕 → 원본 대조·품질 검사 → 청킹
입력: 중단된 실행 폴더(run.json·selection.json·documents/…)와 현재 코드·환경.
출력: 복원한 실행 인자, 호환성 확인 결과, 재사용 예정(plan_resume), 재개 세션(ResumeSession).

역할 분담
- 실제 페이지 처리는 run.py의 run_parsing()·process_document()가 맡는다. 재개 모드에서는 이 모듈의
  ResumeSession을 받아 끝난 문서·페이지를 재사용하고 남은 페이지만 같은 함수로 처리한다.
  처리 경로가 하나라서 처음 실행한 페이지와 재개한 페이지가 같은 규칙으로 만들어진다.
- 설정은 현재 설정 파일이 아니라 중단된 실행의 run.json에서 복원한다. 파서·predict 옵션,
  패키지 버전, Python 빌드, 코드 해시가 하나라도 다르면 재개를 거부한다. 원본 PDF 위치만 바꿀 수 있고
  내용은 해시로 대조한다. 코드를 고친 뒤에는 새 실행 폴더로 처음부터 실행해야 한다.
- 이미 저장된 failed 페이지·문서도 실행 결과로 보고 다시 처리하지 않는다.
- 끝나지 않은 페이지의 잔여 파일(PNG·깨진 JSON·그림 폴더)은 지우지 않고
  resume/segment_<번호>/partial/ 아래 같은 상대 경로로 옮긴다.

실행 부작용: restore_arguments()·check_compatibility()·plan_resume()은 읽기만 한다.
ResumeSession.start()부터 세그먼트 폴더 생성과 파일 이동이 일어난다.
"""

# 표준 라이브러리: argparse.Namespace는 run.py 함수가 받는 실행 인자 형태를 복원한다.
# datetime·timezone은 UTC 기록, Path는 경로 처리다.
import argparse
from datetime import datetime, timezone
from pathlib import Path

# 내부 공통 모듈: file_hash는 원본·결과 파일 동일성, inside는 기록된 상대 경로가
# 실행 폴더 밖으로 나가지 않는지 확인, read_json은 엄격한 JSON 읽기를 맡는다.
# clean은 저장 전 민감정보 제거다.
from common.files import file_hash, inside, read_json
from common.redaction import clean

# 내부 파싱 모듈: pdf_source는 선정 목록 검증, result_store는 배타적 저장과 실행 폴더의
# 문서 ID·폴더·파일 이름 규칙을 맡는다. 재개는 run.py가 만든 폴더를 같은 규칙으로 다시 읽어야 한다.
# run.py는 이 모듈을 import하므로, run.py의 함수는 필요한 함수 안에서만 늦게 가져와 순환 import를 피한다.
from .pdf_source import load_selection
from .result_store import (SELECTION_FILE, SOURCE_PDF_FILE, document_id, document_path, page_identifier,
                           page_stem, save_json)

# 아래 이름은 실행 폴더 저장 형식의 정의다. run.py가 같은 이름으로 파일을 만든다.
# 문서 폴더·페이지 파일 이름처럼 검사·비교·청킹도 읽는 규칙은 result_store에 있다.
RUN_SCHEMA = "project.parsing.v1"
RUN_FILE = "run.json"
SUMMARY_FILE = "summary.json"
DOCUMENT_FILE = "document.json"
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


class ResumeError(ValueError):
    """재개 조건을 만족하지 않는다. 결과를 섞지 않도록 처리를 시작하거나 계속하지 않는다."""


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
    from .run import environment_record, source_hashes

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

    # 코드 해시는 파일별로 비교하고 예외를 두지 않는다. 해시를 새로 덮어쓰거나 옛 값을 복사하지 않는다.
    recorded = metadata.get("code_sha256") or {}
    current = source_hashes()
    changed = sorted(name for name in recorded if current.get(name) != recorded[name])
    added = sorted(set(current) - set(recorded))
    if not recorded:
        problems.append("중단 전 코드 해시 기록이 없습니다.")
    if changed or added:
        problems.append(f"중단 전과 코드가 다릅니다(변경 {changed}, 추가 {added}). 새 실행 폴더로 처음부터 실행하세요.")

    environment = environment_record()
    before = metadata.get("environment") or {}
    package_changes = {name: {"before": (before.get("packages") or {}).get(name), "after": value}
                       for name, value in environment["packages"].items()
                       if (before.get("packages") or {}).get(name) != value}
    if package_changes:
        problems.append(f"패키지 버전이 다릅니다: {package_changes}")
    if before.get("python") != environment["python"]:
        problems.append("Python 버전·빌드가 다릅니다.")
    if problems:
        raise ResumeError("재개할 수 없습니다. " + " ".join(problems))
    return {"code": {"recorded_files": len(recorded), "changed": [], "added": []},
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
    if record.get("page_id") != page_identifier(doc_id, number) or record.get("page_number") != number:
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


def page_summary(doc_id: str, number: int, record: dict) -> dict:
    """run.save_page()가 반환하는 문서 요약 항목과 같은 형태를 저장 없이 만든다."""
    return {
        "page_number": number,
        "page_id": record["page_id"],
        "status": record["status"],
        "json_path": document_path(doc_id, f"{page_stem(number)}.json"),
        "native_status": (record.get("native") or {}).get("status"),
        "parser_status": (record.get("parsing") or {}).get("status"),
    }


def plan_resume(args: argparse.Namespace, selection: dict, root: Path) -> list[dict]:
    """파일을 바꾸지 않고 문서·페이지별로 재사용·재처리 예정을 보여 준다(--check 용)."""
    root = Path(root).resolve()
    plan = []
    for item in selection["documents"]:
        doc_id = document_id(item["document_name"])
        folder = root / document_path(doc_id)
        entry = {"document_name": item["document_name"], "document_id": doc_id}
        if (folder / DOCUMENT_FILE).is_file():
            entry["action"] = "reuse_document"
            plan.append(entry)
            continue
        source = folder / SOURCE_PDF_FILE
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
                          "reason": reason,
                          "leftovers": [] if record else [p.name for p in page_leftovers(folder, number)]})
        entry["pages"] = pages
        plan.append(entry)
    return plan


class ResumeSession:
    """재개 실행 한 번(세그먼트)의 폴더·이동 기록·재사용 통계를 관리한다.

    run.py는 이 객체를 받으면 출력 폴더를 새로 만들지 않고, 문서·페이지마다 아래 메서드로
    재사용 여부를 묻는다. 객체가 없으면 기존과 똑같이 새 실행으로 동작한다.
    """

    def __init__(self, root: Path, metadata: dict, compatibility: dict):
        self.root = Path(root).resolve()
        self.metadata = metadata
        self.compatibility = compatibility
        self.number = None
        self.directory = None
        self.partial = None
        self.set_aside_root = []
        self.reports = []
        self.started_at_utc = None
        self.segment = None

    def start(self, root_files_to_set_aside: tuple = (), secrets: tuple = ()) -> dict:
        """세그먼트 폴더를 만들고 이전 종료 파일을 옮긴 뒤 세그먼트 시작 기록을 반환·저장한다."""
        from .run import environment_record, source_hashes

        base = self.root / RESUME_DIRECTORY
        numbers = []
        if base.is_dir():
            for path in base.iterdir():
                suffix = path.name.removeprefix(SEGMENT_PREFIX)
                if path.is_dir() and path.name.startswith(SEGMENT_PREFIX) and suffix.isdigit():
                    numbers.append(int(suffix))
        self.number = max(numbers, default=FIRST_SEGMENT) + 1
        self.directory = base / f"{SEGMENT_PREFIX}{self.number}"
        # exist_ok=False로 동시에 두 재개가 같은 세그먼트를 쓰지 못하게 한다.
        self.directory.mkdir(parents=True, exist_ok=False)
        self.partial = self.directory / PARTIAL_DIRECTORY
        previous = [self.root / name for name in (SUMMARY_FILE, *root_files_to_set_aside)
                    if (self.root / name).is_file()]
        self.set_aside_root = self.set_aside(previous)
        self.started_at_utc = datetime.now(timezone.utc).isoformat()
        segment = {"schema_version": RESUME_SCHEMA, "segment": self.number,
                   "started_at_utc": self.started_at_utc,
                   "original_started_at_utc": self.metadata.get("started_at_utc"),
                   "environment": environment_record(), "code_sha256": source_hashes(),
                   "compatibility": self.compatibility, "set_aside": self.set_aside_root}
        save_json(self.directory / SEGMENT_START_FILE, clean(segment, secrets))
        self.segment = segment
        return segment

    def set_aside(self, paths: list[Path]) -> list[dict]:
        """잔여 파일을 지우지 않고 세그먼트의 partial/ 아래 같은 상대 경로로 옮긴다."""
        moves = []
        for path in paths:
            relative = path.resolve().relative_to(self.root)
            target = self.partial / relative
            if target.exists():
                raise ResumeError(f"옮길 위치에 파일이 이미 있습니다: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            # rename은 같은 볼륨 안에서 내용을 복사하지 않고 이름만 바꾼다. 원본 bytes는 그대로다.
            path.rename(target)
            moves.append({"from": relative.as_posix(), "to": target.relative_to(self.root).as_posix()})
        return moves

    def new_report(self, doc_id: str) -> dict:
        report = {"document_id": doc_id, "mode": "resumed_document", "reused_pages": [],
                  "processed_pages": [], "invalid_pages": [], "set_aside": [], "review_mismatches": []}
        self.reports.append(report)
        return report

    def completed_document(self, folder: Path, doc_id: str, recorder) -> dict | None:
        """document.json까지 저장된 문서를 검증해 재사용한다. 없으면 None, 손상되면 거부한다."""
        if not (folder / DOCUMENT_FILE).is_file():
            return None
        entry = read_json(folder / DOCUMENT_FILE)
        if not isinstance(entry, dict) or entry.get("document_id") != doc_id:
            raise ResumeError(f"완료 문서 기록의 ID가 다릅니다: {doc_id}")
        source_sha256 = entry.get("source_sha256")
        source = folder / SOURCE_PDF_FILE
        if source_sha256 and (not source.is_file() or file_hash(source) != source_sha256):
            raise ResumeError(f"완료 문서의 원본 사본이 없거나 해시가 다릅니다: {doc_id}")
        review = entry.get("review_pdf")
        if review and not inside(self.root, review).is_file():
            raise ResumeError(f"완료 문서의 검토 PDF가 없습니다: {review}")
        records = []
        for page in entry.get("pages", []):
            number = page.get("page_number")
            record, reason = inspect_saved_page(self.root, folder, doc_id, number, source_sha256)
            if record is None:
                raise ResumeError(f"완료 문서의 페이지 결과가 손상됐습니다: {doc_id} p{number} ({reason})")
            records.append((number, record))
        if recorder:
            source_artifact = (recorder.artifact(source, "source_pdf", source_sha256=source_sha256,
                                                 resume_reused=True) if source_sha256 else None)
            for number, record in records:
                self.register_page(recorder, folder, number, record, source_artifact)
            if review:
                recorder.artifact(inside(self.root, review), "review_pdf",
                                  parents=[p for p in (source_artifact,) if p], document_id=doc_id,
                                  resume_reused=True)
        report = self.new_report(doc_id)
        report.update(mode="reused_document", reused_pages=[number for number, _ in records])
        return entry

    def check_source(self, folder: Path, source_sha256: str, name: str) -> bool:
        """미완료 문서의 원본 사본이 있으면 해시를 대조한다. 있으면 True(새로 저장하지 않음)다."""
        saved = folder / SOURCE_PDF_FILE
        if not saved.is_file():
            return False
        # 중단 전과 다른 원본으로 남은 페이지를 처리하면 한 문서에 두 원본이 섞인다.
        if file_hash(saved) != source_sha256:
            raise ResumeError(f"원본 PDF가 중단 전과 다릅니다: {name}")
        return True

    def saved_page(self, report: dict, folder: Path, doc_id: str, number: int, source_sha256: str):
        """재사용할 페이지 기록을 반환한다. 없거나 손상됐으면 잔여 파일을 옮기고 None을 반환한다."""
        record, reason = inspect_saved_page(self.root, folder, doc_id, number, source_sha256)
        if record is not None:
            return record
        if reason != "missing":
            report["invalid_pages"].append({"page_number": number, "reason": reason})
        report["set_aside"] += self.set_aside(page_leftovers(folder, number))
        return None

    def reuse_page(self, context, number: int, document, review, record: dict, report: dict) -> dict:
        """저장된 블록으로 검토 PDF 페이지를 다시 만들고 결과물로 등록한 뒤 문서 요약 항목을 반환한다."""
        from .review_pdf import add_review_page

        if number <= len(document):
            rebuilt = add_review_page(review, document, number,
                                      {"status": record["status"],
                                       "blocks": (record.get("parsing") or {}).get("blocks", [])},
                                      document_path(context.doc_id, f"{page_stem(number)}.json"))
            saved = record.get("review") or {}
            keys = ("review_page_number", "status", "drawn_count", "warnings")
            if any(rebuilt.get(key) != saved.get(key) for key in keys):
                # 페이지 JSON은 고치지 않는다. 검토 PDF와 기록의 차이를 재개 기록에 남긴다.
                report["review_mismatches"].append({"page_number": number,
                                                    "saved": {k: saved.get(k) for k in keys},
                                                    "rebuilt": {k: rebuilt.get(k) for k in keys}})
        self.register_page(context.recorder, context.folder, number, record, context.source_artifact)
        report["reused_pages"].append(number)
        return page_summary(context.doc_id, number, record)

    def register_page(self, recorder, folder: Path, number: int, record: dict, source_artifact) -> None:
        """재사용한 페이지 파일을 새 세그먼트의 결과물 목록에 해시와 함께 등록한다."""
        if not recorder:
            return
        page_id = record["page_id"]
        image_artifact = None
        if (record.get("page_image") or {}).get("path"):
            image_artifact = recorder.artifact(inside(self.root, record["page_image"]["path"]), "page_image",
                                               parents=[p for p in (source_artifact,) if p], page_id=page_id,
                                               resume_reused=True)
        for item in ((record.get("parsing") or {}).get("result_images") or {}).get("items") or []:
            recorder.artifact(inside(self.root, item["path"]), "parsing_result_image",
                              parents=[p for p in (image_artifact,) if p], page_id=page_id, resume_reused=True)
        recorder.artifact(folder / f"{page_stem(number)}.json", "parsing_page",
                          parents=[p for p in (source_artifact, image_artifact) if p],
                          page_id=page_id, resume_reused=True)

    def set_aside_review(self, path: Path, report: dict) -> None:
        """검토 PDF 저장 뒤 document.json 전에 끊긴 경우의 이전 검토 PDF를 옮겨 보존한다."""
        if path.exists():
            report["set_aside"] += self.set_aside([path])

    def summary(self) -> dict:
        """summary.json의 resume 항목이다. 이번 세그먼트에서 재사용·처리한 양을 센다."""
        return {
            "segment": self.number,
            "segment_record": self.directory.relative_to(self.root).as_posix(),
            "reused_documents": sum(r["mode"] == "reused_document" for r in self.reports),
            "reused_pages": sum(len(r["reused_pages"]) for r in self.reports),
            "processed_pages": sum(len(r["processed_pages"]) for r in self.reports),
            "invalid_pages": sum(len(r["invalid_pages"]) for r in self.reports),
            "set_aside_files": len(self.set_aside_root) + sum(len(r["set_aside"]) for r in self.reports),
            "review_mismatches": sum(len(r["review_mismatches"]) for r in self.reports),
        }

    def finish(self, summary: dict, secrets: tuple = ()) -> None:
        save_json(self.directory / SEGMENT_FINISH_FILE, clean({
            "schema_version": RESUME_SCHEMA, "segment": self.number, "status": summary["status"],
            "finished_at_utc": summary["finished_at_utc"], "elapsed_seconds": summary["elapsed_seconds"],
            "model_load_seconds": summary["model_load_seconds"], "fatal_error": summary["fatal_error"],
            "documents": self.reports}, secrets))
