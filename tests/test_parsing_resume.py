"""중단된 파싱 실행의 재개 경계를 검증한다.

전원 차단처럼 종료 기록 없이 끊긴 실행을 테스트 안에서 만든다. 정상 실행 뒤 종료 파일과
끝나지 않은 페이지의 JSON을 지우고 PNG만 남겨, 페이지 추론 도중 끊긴 상태를 재현한다.
모델은 호출 횟수를 셀 수 있는 모의 객체로 바꾸며 실제 다운로드·추론은 하지 않는다.
실제 결과 경로·해시·개수를 고정하지 않고, 모든 PDF·설정은 임시 폴더에서 만든다.
"""

# 표준 라이브러리: Path·tempfile은 임시 실행 폴더, SimpleNamespace·mock은 모델 없는 객체와
# 호출 횟수 확인, unittest는 테스트 구성을 맡는다.
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

# 내부 모듈: common은 해시·JSON, experiments는 기록·재개·비교 진입점, parsing.resume은 재개 규칙이다.
from common.files import digest, file_hash, read_json, write_json
from experiments.compare import _cost_difference, load_run
from experiments.record import ExperimentRecorder
from experiments.schema import FINISHED_FILE, MANIFEST_FILE, MODELS_FILE, RECORD_SCHEMA
from parsing.resume import RESUME_DIRECTORY

# 측정 스레드가 테스트 순서에 끼어들지 않도록 긴 간격을 쓴다. 구간 시작·끝에서 동기 샘플을 남긴다.
TEST_INTERVAL_SECONDS = 60
TEST_GIT_TIMEOUT_SECONDS = 1
# 테스트용 문서 구성이다. 첫 문서는 중간에 끊기고 두 번째 문서는 끝까지 처리된 상태를 만든다.
INTERRUPTED_DOCUMENT = "interrupted-document"
COMPLETED_DOCUMENT = "completed-document"


def record_config(root):
    return {"schema_version": RECORD_SCHEMA, "project_root": str(root),
            "source_paths": ["source.py"], "source_suffixes": [".py"],
            "git_timeout_seconds": TEST_GIT_TIMEOUT_SECONDS,
            "memory": {"interval_seconds": TEST_INTERVAL_SECONDS, "include_children": False, "gpu_device": None},
            "experiment": {"purpose": "재개 경계 검증"}, "model_bindings": [],
            "secret_environment_names": [], "environment_packages": [], "pipeline_options": {}, "predict_options": {}}


def fake_parser():
    """페이지마다 블록 하나를 돌려주는 모의 모델이다. predict 호출 횟수로 재처리 여부를 센다."""
    raw = {"res": {"parsing_res_list": [{"block_label": "text", "block_content": "synthetic evidence",
                                        "block_bbox": [0, 0, 10, 10]}]}}
    return SimpleNamespace(_merged_paddlex_config={},
                           predict=MagicMock(side_effect=lambda **kwargs: iter([SimpleNamespace(json=raw)])))


def write_pdf(path, pages, text):
    import pymupdf
    with pymupdf.open() as document:
        for number in range(pages):
            document.new_page().insert_text((20, 30), f"{text} {number + 1}")
        path.write_bytes(document.tobytes())


class ResumeFixture:
    """정상 실행 → 전원 차단 상태 재현까지 준비한다."""

    def __init__(self, root):
        from experiments.parsing_stage import main as parsing_main
        self.root = root
        (root / "source.py").write_text("value = 1\n")
        self.inputs = root / "inputs"; self.inputs.mkdir()
        write_pdf(self.inputs / f"{INTERRUPTED_DOCUMENT}.pdf", 2, "first")
        write_pdf(self.inputs / f"{COMPLETED_DOCUMENT}.pdf", 1, "second")
        self.selection = root / "selection.json"
        write_json(self.selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
            {"document_name": INTERRUPTED_DOCUMENT, "page_numbers": [1, 2]},
            {"document_name": COMPLETED_DOCUMENT, "page_numbers": [1]}]})
        self.config = root / "record.json"; write_json(self.config, record_config(root))
        self.run = root / "runs" / "run"
        with patch("parsing.vl_parser.build_parser", return_value=fake_parser()):
            result = parsing_main(["--pdf-directory", str(self.inputs), "--selection", str(self.selection),
                                   "--output-dir", str(self.run), "--experiment-config", str(self.config),
                                   "--no-auto-compare"])
        assert result == 0
        self.interrupted = self.run / "documents" / digest(INTERRUPTED_DOCUMENT.encode("utf-8"))
        self.completed = self.run / "documents" / digest(COMPLETED_DOCUMENT.encode("utf-8"))
        # 전원 차단: 종료 기록이 없고, 첫 문서는 2페이지 PNG만 저장된 채 끊겼다.
        for name in ("summary.json", FINISHED_FILE, "auto_comparison.json"):
            (self.run / name).unlink()
        for name in ("document.json", "review_vl.pdf", "page_00002.json"):
            (self.interrupted / name).unlink()

    def resume(self, *extra):
        from experiments.resume_stage import main as resume_main
        parser = fake_parser()
        with patch("parsing.vl_parser.build_parser", return_value=parser):
            result = resume_main(["--run-dir", str(self.run), "--no-auto-compare", *extra])
        return result, parser


class ResumeTests(unittest.TestCase):
    def test_only_unfinished_pages_are_processed_and_leftovers_are_kept(self):
        import pymupdf
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            reused = file_hash(fixture.interrupted / "page_00001.json")
            result, parser = fixture.resume()
            self.assertEqual(result, 0)
            # 끝난 두 페이지는 다시 추론하지 않고, 끊긴 한 페이지만 처리한다.
            self.assertEqual(parser.predict.call_count, 1)
            self.assertEqual(file_hash(fixture.interrupted / "page_00001.json"), reused)
            summary = read_json(fixture.run / "summary.json")
            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["resume"]["processed_pages"], 1)
            self.assertEqual(summary["resume"]["reused_pages"], 2)
            self.assertEqual(summary["resume"]["reused_documents"], 1)
            # 잔여 PNG는 지우지 않고 세그먼트 폴더로 옮긴다.
            partial = fixture.run / RESUME_DIRECTORY / "segment_2" / "partial"
            self.assertTrue((partial / fixture.interrupted.relative_to(fixture.run) / "page_00002.png").is_file())
            # 검토 PDF는 재사용 페이지와 새 페이지를 모두 담는다.
            with pymupdf.open(fixture.interrupted / "review_vl.pdf") as review:
                self.assertEqual(len(review), 2)
            # 종료 기록은 중단 전 실행 ID를 잇고, 시작 기록은 세그먼트 폴더에 남는다.
            finished = read_json(fixture.run / FINISHED_FILE)
            self.assertEqual(finished["run_id"], read_json(fixture.run / MANIFEST_FILE)["run_id"])
            self.assertEqual(finished["record_scope"], "resume_segment")
            self.assertTrue((fixture.run / RESUME_DIRECTORY / "segment_2" / MANIFEST_FILE).is_file())
            loaded = load_run(fixture.run)
            self.assertFalse([p for p in loaded["problems"] if "artifact_id" in p])

    def test_changed_code_is_refused_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            before = {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()}
            recorded = read_json(fixture.run / "run.json")["code_sha256"]
            changed = {**recorded, next(iter(recorded)): "0" * 64}
            with patch("parsing.resume.source_hashes", return_value=changed):
                result, parser = fixture.resume()
            self.assertEqual(result, 2)
            parser.predict.assert_not_called()
            self.assertEqual(before, {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()})
            self.assertFalse((fixture.run / RESUME_DIRECTORY).exists())

    def test_changed_source_pdf_stops_and_can_resume_after_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            original = fixture.inputs / f"{INTERRUPTED_DOCUMENT}.pdf"
            kept = original.read_bytes()
            write_pdf(original, 2, "changed")
            result, parser = fixture.resume()
            self.assertEqual(result, 1)
            parser.predict.assert_not_called()
            self.assertEqual(read_json(fixture.run / "summary.json")["fatal_error"]["type"], "ResumeError")
            # 원본을 되돌리면 치명 오류 요약을 옮겨 두고 다음 세그먼트로 다시 재개한다.
            original.write_bytes(kept)
            result, parser = fixture.resume()
            self.assertEqual(result, 0)
            self.assertEqual(parser.predict.call_count, 1)
            self.assertTrue((fixture.run / RESUME_DIRECTORY / "segment_3" / "partial" / "summary.json").is_file())

    def test_broken_page_json_is_set_aside_and_processed_again(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            broken = fixture.interrupted / "page_00001.json"
            broken.write_text('{"page_id": ', encoding="utf-8")
            result, parser = fixture.resume()
            self.assertEqual(result, 0)
            self.assertEqual(parser.predict.call_count, 2)
            summary = read_json(fixture.run / "summary.json")
            self.assertEqual(summary["resume"]["invalid_pages"], 1)
            partial = fixture.run / RESUME_DIRECTORY / "segment_2" / "partial"
            self.assertTrue((partial / broken.relative_to(fixture.run)).is_file())

    def test_finished_run_is_not_resumed(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            self.assertEqual(fixture.resume()[0], 0)
            result, parser = fixture.resume()
            self.assertEqual(result, 2)
            parser.predict.assert_not_called()

    def test_check_reports_plan_without_changing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = ResumeFixture(Path(directory))
            before = {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()}
            with patch("parsing.vl_parser.build_parser") as build:
                from experiments.resume_stage import main as resume_main
                self.assertEqual(resume_main(["--run-dir", str(fixture.run), "--check", "--no-auto-compare"]), 0)
            build.assert_not_called()
            self.assertEqual(before, {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()})


class ResumeRecordTests(unittest.TestCase):
    def test_changed_model_files_stop_the_segment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "source.py").write_text("value = 1\n")
            weights = root / "weights"; weights.mkdir(); (weights / "model.bin").write_bytes(b"synthetic")
            config = record_config(root)
            config["model_bindings"] = [{"role": "layout", "object_path": ["layout"],
                                         "directory_attribute": "model_dir", "name_attribute": "model_name"}]
            run = root / "run"; segment = run / RESUME_DIRECTORY / "segment_2"; segment.mkdir(parents=True)
            parser = SimpleNamespace(layout=SimpleNamespace(model_dir=str(weights), model_name="synthetic"))
            recorder = ExperimentRecorder(run, config, {"parser": "vl"}, record_dir=segment,
                                          run_id="previous-run", expected_models={"layout": "0" * 64})
            try:
                with self.assertRaises(ValueError):
                    recorder.capture_parser(parser)
                self.assertEqual(recorder.models["continuity"]["status"], "changed")
                self.assertTrue((segment / MODELS_FILE).is_file())
            finally:
                recorder.finish("failed")
            self.assertEqual(read_json(run / FINISHED_FILE)["run_id"], "previous-run")

    def test_resumed_cost_is_not_compared_as_a_whole_run(self):
        plain = {"finished": None, "summary": {"elapsed_seconds": 10}, "pages": {}}
        resumed = {"finished": None, "summary": {"elapsed_seconds": 3, "resume": {"segment": 2}}, "pages": {}}
        result = _cost_difference(plain, resumed)
        self.assertEqual(result["elapsed_seconds"]["comparison_kind"], "not_comparable_resumed")
        self.assertIsNone(result["elapsed_seconds"]["delta"])


if __name__ == "__main__":
    unittest.main()
