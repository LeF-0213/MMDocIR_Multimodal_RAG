"""
PDF → 페이지 준비 → 선택한 파서 → 좌표 연결 → 저장을 실행한다.
pdf_source.py에서 입력을 받고 pdf_parser.py로 원본 정보와 이미지를 준비한다.
vl_parser.py 또는 structure_parser.py를 호출한 뒤 review_pdf.py로 검토본을 만든다.
실행 시 로컬 PDF 읽기 또는 HTTP 다운로드·모델 캐시·새 결과 폴더 생성이 발생한다.
기존 파일은 덮어쓰지 않으며 --help는 모델 로딩·다운로드·결과 저장을 하지 않는다.

모델은 실행당 한 번 로딩하고 페이지마다 결과와 실패 위치를 기록한다.
실험 기록·전후 비교는 모르며, experiments.parsing_stage가 기록기를 인자로 넘겨 연결한다.
python -m parsing.run은 파싱 결과만 남긴다. 기록·비교까지 하려면 루트 main.py를 실행한다.
"""

# 표준 라이브러리: argparse는 CLI 검증에 사용한다. contextlib는 기록을 끈 호출의 빈 구간,
# json은 실행 요약을 터미널에 출력하고 math는 설정의 NaN·무한대를 거부한다.
import argparse
from contextlib import nullcontext
import json
import math

# platform·sys는 실행 환경, Counter는 상태별 개수를 기록한다.
import platform
import sys
from collections import Counter

# datetime·timezone은 UTC 실행 식별, metadata는 설치 버전 조회에 사용한다.
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version

# Path는 경로, perf_counter는 모델 로딩·처리 경과 시간 측정에 사용한다.
from pathlib import Path
from time import perf_counter

# 내부 공통 모듈: 입력·코드 내용 해시와 저장 직전의 민감정보 제거를 맡는다.
# 공통 패키지 전체를 코드 해시에 넣으려고 위치 확인용으로 패키지도 가져온다.
import common
from common.files import digest
from common.redaction import clean, diagnostic_clean

# 아래 내부 모듈은 네트워크 입력과 파일 저장의 책임을 분리한다.
# PDF 처리·모델·검토 모듈은 CLI 검증 뒤 가져와 --help의 의존성을 줄인다.
from .pdf_source import build_pdf_url, load_selection, local_pdf_path, read_pdf_bytes
from .result_store import create_output_directory, save_bytes, save_json

# 실행 폴더 이름의 UTC 직렬화 형식이다. 실제 상위 폴더·파서는 설정이나 CLI에서 받는다.
RUN_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"


def new_output_directory(parent: Path, parser: str) -> Path:
    """반복 실행을 보존할 새 경로만 계산한다. 폴더 생성·덮어쓰기는 하지 않는다."""
    stamp = datetime.now(timezone.utc).strftime(RUN_TIMESTAMP_FORMAT)
    return parent / f"parsing_{parser}_{stamp}"


def positive_int(value: str) -> int:
    """잘못된 해상도·스레드 설정을 실행 전에 거부한다."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("1 이상의 정수가 필요합니다.")
    return parsed


def add_parsing_arguments(cli: argparse.ArgumentParser) -> None:
    """파싱에 필요한 옵션을 등록한다. experiments.parsing_stage도 같은 옵션을 재사용한다."""
    cli.add_argument(
        "--selection",
        type=Path,
        default=Path("configs/selection.json"),
        help="PDF 뷰어 기준 1부터 시작하는 페이지 선택 목록",
    )
    source = cli.add_mutually_exclusive_group(required=True)
    source.add_argument("--base-url", help="원본 PDF가 있는 HTTP(S) 폴더 URL")
    source.add_argument("--pdf-directory", type=Path, help="선정 문서의 원본 PDF가 있는 로컬 폴더")
    cli.add_argument("--output-dir", type=Path, help="새 결과 폴더. 기존 폴더는 거부")
    # 두 모델의 메모리와 결과를 구분하려고 한 실행에는 파서 하나만 사용한다.
    cli.add_argument(
        "--parser",
        choices=("vl", "structure"),
        default="vl",
        help="vl: PaddleOCR-VL, structure: PP-StructureV3",
    )
    cli.add_argument("--device", default="cpu", help="예: cpu 또는 gpu:0")
    cli.add_argument("--cpu-threads", type=positive_int, default=4)

    # 144 DPI는 PDF 1포인트를 2픽셀로 렌더링하는 시작값이다.
    # 최적 품질로 실측한 값이 아니며 작은 수식은 높은 DPI와 비용을 비교해야 한다.
    cli.add_argument("--dpi", type=positive_int, default=144)
    cli.add_argument("--formula-model", help="structure 경로에서 사용할 수식 모델 ID")
    cli.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="HTTP 연결·읽기 대기 초. 총 다운로드 시간 제한은 아님",
    )
    cli.add_argument(
        "--all-pages",
        action="store_true",
        help="선정 문서의 모든 페이지 처리. 문서 목록 자체는 변경하지 않음",
    )


def validate_parsing_arguments(cli: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """옵션 사이의 조건을 검사하고 출력 폴더 기본값을 채운다."""
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        cli.error("--timeout은 유한한 양수여야 합니다.")
    if args.parser == "structure" and not args.formula_model:
        cli.error("structure 경로에는 --formula-model을 지정해야 합니다.")
    if args.parser == "vl" and args.formula_model:
        cli.error("--formula-model은 structure 경로에서만 사용합니다.")
    if not args.device.strip():
        cli.error("--device에는 비어 있지 않은 장치를 지정해야 합니다.")
    if args.output_dir is None:
        # 마이크로초를 포함하되 충돌하면 덮어쓰지 않고 실패한다.
        args.output_dir = new_output_directory(Path("outputs"), args.parser)


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """파싱만 실행할 때의 옵션을 검증한다. 기본값도 실행 기록에 남긴다."""
    cli = argparse.ArgumentParser(description="PDF 파싱과 원본 bbox 검토본 생성")
    add_parsing_arguments(cli)
    args = cli.parse_args(argv)
    validate_parsing_arguments(cli, args)
    return args


def validate_source(args: argparse.Namespace, selection: dict) -> None:
    """출력 생성·모델 로딩 전에 입력을 검사한다. 로컬 문서는 전부 존재 여부를 확인한다."""
    if args.pdf_directory is not None:
        for item in selection["documents"]:
            local_pdf_path(args.pdf_directory, item["document_name"])
    else:
        build_pdf_url(args.base_url, selection["documents"][0]["document_name"])


def measure(recorder, name: str, **details):
    """recorder는 measure()를 제공하는 객체다. 기록을 끄면 빈 구간으로 같은 호출부를 유지한다."""
    return recorder.measure(name, **details) if recorder else nullcontext()


def source_hashes() -> dict:
    """파싱 결과에 영향을 주는 코드의 해시다. common 파일은 이름이 겹치지 않게 경로를 붙인다."""
    hashes = {p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))}
    hashes.update({f"common/{p.name}": digest(p.read_bytes())
                   for p in sorted(Path(common.__file__).parent.glob("*.py"))})
    return hashes


def environment_record() -> dict:
    """모델을 import하지 않고 설치 버전과 실제 실행 환경을 조회한다."""
    packages = {}
    for name in (
        "paddleocr",
        "paddlex",
        "paddlepaddle",
        "paddlepaddle-gpu",
        "PyMuPDF",
        "numpy",
        "psutil",
    ):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "python": sys.version,
        "executable": sys.executable,
        "platform": platform.platform(),
        "packages": packages,
    }


def process_document(
    item: dict,
    args: argparse.Namespace,
    root: Path,
    adapter,
    parser,
    recorder=None,
    predict_overrides: dict | None = None,
) -> dict:
    """문서별로 입력을 읽고 각 페이지 결과를 즉시 저장해 중단 시 손실을 줄인다."""
    # 내부 모듈: PDF 읽기·좌표는 기존 pdf_parser.py에 그대로 위임한다.
    # NumPy 반환값 등은 model_result.py에서 표준 JSON 타입으로 바꾼다.
    from .pdf_parser import add_pdf_coordinates, error_info, open_pdf, prepare_page
    from .model_result import json_builtin
    from .review_pdf import add_review_page

    # PyMuPDF는 검토용 빈 문서를 만들고 페이지 복사본을 관리한다.
    import pymupdf

    name = item["document_name"]
    # 경로명은 문서명 해시 전체를 쓴다. 파일명에 OS 예약문자·긴 이름이 있어도
    # 안전하게 저장하며 이름의 해시와 원본 bytes의 해시는 별개 필드로 기록한다.
    doc_id = digest(name.encode("utf-8"))
    folder = root / "documents" / doc_id
    folder.mkdir(parents=True, exist_ok=False)
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
        if getattr(args, "pdf_directory", None) is not None:
            path = local_pdf_path(args.pdf_directory, name)
            entry.update(source_kind="local", source_path=str(path))
            with measure(recorder, "pdf_read", document_id=doc_id):
                data = path.read_bytes()
        else:
            url = build_pdf_url(args.base_url, name)
            entry.update(source_kind="http", source_url=clean(url, recorder.secrets if recorder else ()))
            with measure(recorder, "pdf_download", document_id=doc_id):
                data = read_pdf_bytes(url, timeout=args.timeout)
        entry["source_sha256"] = digest(data)
        entry["source_pdf"] = f"documents/{doc_id}/source.pdf"
        save_bytes(folder / "source.pdf", data)
        source_artifact = (recorder.artifact(folder / "source.pdf", "source_pdf", source_sha256=entry["source_sha256"])
                           if recorder else None)

        stage = "open_pdf"
        with open_pdf(data) as document, pymupdf.open() as review:
            entry["source_page_count"] = len(document)

            # --all-pages는 샘플 설정을 덮어쓰지 않고 실제 PDF 페이지 수로 확장한다.
            page_numbers = (
                list(range(1, len(document) + 1))
                if args.all_pages
                else sorted(item["page_numbers"])
            )
            entry["requested_page_numbers"] = page_numbers
            entry["review_pdf"] = f"documents/{doc_id}/review_{args.parser}.pdf"

            for number in page_numbers:
                page_started = perf_counter()
                page_id = f"{doc_id}:p{number}"
                stem = f"page_{number:05d}"
                page_path = f"documents/{doc_id}/{stem}.json"
                record = {
                    "page_id": page_id,
                    "id_origin": "project_generated",
                    "document_id": doc_id,
                    "document_name": name,
                    "page_number": number,
                    "page_numbering": "pdf_viewer_1_based",
                    "page_index": number - 1,
                    "source_sha256": entry["source_sha256"],
                    "status": "failed",
                }

                image = png = None
                image_artifact = None
                page_stage = "page_validation"
                try:
                    if number > len(document):
                        raise ValueError(f"원본 {len(document)}페이지에 선택한 {number}페이지가 없습니다.")

                    page_stage = "prepare_page"
                    with measure(recorder, "page_prepare", page_id=page_id):
                        prepared, image, png = prepare_page(document[number - 1], args.dpi)
                    record.update(prepared)

                    if png is not None:
                        save_bytes(folder / f"{stem}.png", png)
                        record["page_image"] = {
                            "path": f"documents/{doc_id}/{stem}.png",
                            "sha256": digest(png),
                        }
                        if recorder:
                            image_artifact = recorder.artifact(folder / f"{stem}.png", "page_image",
                                                               parents=[source_artifact], page_id=page_id)

                    page_stage = "model_prediction"
                    if image is None:
                        record["parsing"] = {
                            "status": "skipped",
                            "blocks": [],
                            "raw": None,
                            "reason": "페이지 렌더링 실패",
                        }
                    else:
                        # 텍스트층이 있어도 전체 페이지 이미지에 모델을 적용한다.
                        # 일부 누락·표·수식·그림 속 글자는 빈 텍스트층 검사로 찾을 수 없다.
                        with measure(recorder, "model_prediction", page_id=page_id, kind="model_call") as scope:
                            if predict_overrides is not None:
                                record["parsing"] = adapter.parse_image(parser, image, page_id,
                                    predict_overrides=predict_overrides)
                            else:
                                record["parsing"] = adapter.parse_image(parser, image, page_id)
                            if recorder and record["parsing"]["status"] in ("failed", "skipped"):
                                scope["status"] = "failed"

                    parsed = record["parsing"]
                    page_stage = "coordinate_mapping"
                    if parsed["status"] in ("ok", "empty"):
                        parsed["blocks"] = add_pdf_coordinates(
                            parsed["blocks"],
                            record["render"]["pixel_to_pdf_matrix"],
                        )
                        parsed["coordinate_status"] = "ok"

                    # 원본 추출·이미지 조회·모델 중 어느 부분이 실패해도 전체 성공으로
                    # 숨기지 않는다. ok는 처리 완료일 뿐 품질 검증 완료가 아니다.
                    failed = any(
                        record[k]["status"] == "failed"
                        for k in ("native", "images", "render")
                    )
                    record["status"] = "partial" if failed else parsed["status"]
                    if parsed["status"] in ("failed", "skipped"):
                        record["status"] = "failed"
                except Exception as exc:
                    record["error"] = {"stage": page_stage, **error_info(exc)}
                    record["status"] = "failed"
                    if page_stage == "coordinate_mapping":
                        record["parsing"]["coordinate_status"] = "failed"
                finally:
                    # 페이지 이미지 배열을 기록에 넣지 않고 다음 페이지 전에 참조를 해제한다.
                    image = png = None

                # 파싱 실패도 원본 페이지 복사본을 남겨 무엇이 빠졌는지 검토한다.
                # 존재하지 않는 페이지는 복사하지 않고 JSON에만 실패를 기록한다.
                if number <= len(document):
                    try:
                        record["review"] = add_review_page(
                            review,
                            document,
                            number,
                            {
                                "status": record["status"],
                                # 시각화 함수의 입력은 상태와 블록 목록이다.
                                # 페이지 기록에서는 블록이 parsing 아래에 있으므로
                                # 명시적으로 꺼내 전달한다. 페이지 준비가 실패한 경우
                                # 빈 목록을 넘겨 원본 복사본과 실패 상태만 남긴다.
                                "blocks": record.get("parsing", {}).get("blocks", []),
                            },
                            page_path,
                        )
                        record["review"]["path"] = entry["review_pdf"]
                        if (
                            record["review"]["status"] != "ok"
                            and record["status"] in ("ok", "empty")
                        ):
                            record["status"] = "partial"
                    except Exception as exc:
                        record["review"] = {"status": "failed", "error": error_info(exc)}
                        record["status"] = "failed"

                record["elapsed_seconds"] = perf_counter() - page_started

                # strict JSON 실패나 디스크 오류는 문서 수준 오류로도 기록한다.
                # 실패를 무시하고 다음 파일을 만들어 성공처럼 보이게 하지 않는다.
                stage = "page_result_save"
                if recorder:
                    record["experiment"] = {"run_id": recorder.run_id, "stage_run_id": recorder.stage_id}
                save_json(folder / f"{stem}.json", diagnostic_clean(json_builtin(record), recorder.secrets if recorder else ()))
                if recorder:
                    recorder.artifact(folder / f"{stem}.json", "parsing_page",
                                      parents=[p for p in (source_artifact, image_artifact) if p], page_id=page_id)
                entry["pages"].append(
                    {
                        "page_number": number,
                        "page_id": page_id,
                        "status": record["status"],
                        "json_path": page_path,
                        "native_status": record.get("native", {}).get("status"),
                        "parser_status": record.get("parsing", {}).get("status"),
                    }
                )
                print(f"[{args.parser}] {name} p{number}: {record['status']}", flush=True)

            stage = "review_save"
            if len(review):
                # 검토본만 새 파일로 저장한다. 원본 PDF의 내용과 해시는 변하지 않는다.
                save_bytes(
                    folder / f"review_{args.parser}.pdf",
                    review.tobytes(garbage=3, deflate=True),
                )
            else:
                entry["review_pdf"] = None
            entry["status"] = (
                "ok"
                if entry["pages"]
                and all(p["status"] in ("ok", "empty") for p in entry["pages"])
                else "partial"
            )
    except Exception as exc:
        entry["errors"].append({"stage": stage, **error_info(exc)})
        entry["status"] = "failed"

    entry = diagnostic_clean(entry, recorder.secrets if recorder else ())
    save_json(folder / "document.json", entry)
    return entry


def run_parsing(
    args: argparse.Namespace,
    selection: dict,
    *,
    pipeline_overrides: dict | None = None,
    predict_overrides: dict | None = None,
    extra_metadata: dict | None = None,
    start_recorder=None,
    secrets: tuple = (),
) -> dict:
    """모델을 로딩해 선정 문서를 처리하고 run.json·summary.json을 저장한 뒤 결과를 반환한다.

    start_recorder(root, metadata)는 run.json 저장 뒤 호출되어 기록기를 반환한다.
    기록기는 measure·artifact·capture_parser·secrets·run_id·stage_id를 제공해야 한다.
    종료 처리는 호출부 몫이다. 중단(KeyboardInterrupt 등)도 결과의 interrupted로 돌려주므로
    호출부는 자신의 마무리를 끝낸 뒤 다시 raise한다.
    """
    # CLI 검증 뒤 선택한 어댑터만 가져온다. --help는 모델을 로딩하지 않는다.
    if args.parser == "vl":
        from . import vl_parser as adapter

        options = adapter.parser_options(args.device, args.cpu_threads)
    else:
        from . import structure_parser as adapter

        options = adapter.parser_options(args.device, args.formula_model, args.cpu_threads)

    # 설정은 실제 build_parser()에 전달할 override와 동일하게 조합한다.
    options.update(pipeline_overrides or {})
    actual_predict_options = adapter.predict_options()
    actual_predict_options.update(predict_overrides or {})

    root = args.output_dir
    create_output_directory(root)
    save_json(root / "selection.json", selection)
    started = perf_counter()
    metadata = {
        "schema_version": "project.parsing.v1",
        "status": "started",
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": environment_record(),
        "parser": args.parser,
        "parser_options": options,
        "dpi": args.dpi,
        "http_timeout_seconds": args.timeout,
        "all_pages": args.all_pages,
        "base_url": args.base_url,
        "source_kind": "local" if args.pdf_directory is not None else "http",
        "pdf_directory": str(args.pdf_directory.resolve()) if args.pdf_directory is not None else None,
        "selection_sha256": digest(args.selection.read_bytes()),
        # 미확인 모델 가중치 revision·하드웨어·정밀도는 추정하지 않는다.
        "model_weight_revision": None,
        "quality_evaluation": "not_run",
        "code_sha256": source_hashes(),
        # 어댑터의 parse_image()가 실제 추론에 넘기는 값과 같은 함수에서 가져온다.
        # 값을 여기에 따로 적으면 어댑터만 고쳤을 때 기록이 실제 설정과 어긋난다.
        "predict_options": actual_predict_options,
        **(extra_metadata or {}),
    }

    # 중단되어도 실행 조건은 남는다. 최종 결과는 별도 summary.json으로 저장한다.
    save_json(root / "run.json", clean(metadata, secrets))
    recorder = start_recorder(root, metadata) if start_recorder else None
    entries = []
    fatal = None
    interrupted = None
    model_load_seconds = None

    try:
        init_started = perf_counter()
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
                entry = process_document(item, args, root, adapter, parser, recorder, predict_overrides)
                entries.append(entry)
                if recorder:
                    scope["status"] = entry["status"]
    except BaseException as exc:
        if not isinstance(exc, Exception):
            interrupted = exc
        fatal = {"type": type(exc).__name__, "message": clean(str(exc), secrets)}
        if model_load_seconds is None:
            model_load_seconds = perf_counter() - init_started

    counts = Counter(p["status"] for d in entries for p in d["pages"])
    success = (
        fatal is None
        and len(entries) == len(selection["documents"])
        and all(d["status"] == "ok" for d in entries)
    )

    summary = {
        "status": "completed" if success else "completed_with_errors",
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": perf_counter() - started,
        "model_load_seconds": model_load_seconds,
        "parser": args.parser,
        "document_count": len(entries),
        "page_status_counts": dict(counts),
        "documents": entries,
        "fatal_error": fatal,
        "quality_evaluation": "not_run",
    }
    save_json(root / "summary.json", clean(summary, secrets))
    return {"root": root, "summary": summary, "success": success, "interrupted": interrupted}


def main(argv: list[str] | None = None) -> int:
    """기록 없이 파싱만 실행한다. 실패가 있으면 종료 코드 1을 반환한다."""
    args = arguments(argv)
    selection = load_selection(args.selection)
    validate_source(args, selection)
    outcome = run_parsing(args, selection)
    if outcome["interrupted"]:
        raise outcome["interrupted"]
    summary = outcome["summary"]
    print(
        json.dumps(
            {
                "output_dir": str(outcome["root"]),
                "status": summary["status"],
                "page_status_counts": summary["page_status_counts"],
                "fatal_error": summary["fatal_error"],
            },
            ensure_ascii=False,
        )
    )

    return 0 if outcome["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
