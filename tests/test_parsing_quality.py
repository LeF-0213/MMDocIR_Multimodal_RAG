"""파싱 품질 검사의 의미 있는 경계와 CLI 저장 부작용을 검증한다.

모델 없이 병합셀·좌표 변환·반복 신호·미해결 판정을 검사한다.
파일 검사는 임시 폴더의 작은 입력으로 수행하고, 원본 미변경과 배타적 보고서 저장을 확인한다.
실제 PDF 문자 인식 정확도를 측정하는 테스트는 아니다.
"""

# 표준 라이브러리: copy는 원본 미변경 비교, hashlib는 작은 입력의 해시,
# json은 임시 입력·보고서, subprocess·sys는 실제 Python CLI 실행을 담당한다.
# tempfile은 검사 뒤 정리되는 임시 폴더, unittest는 테스트와 단언,
# Path는 저장소 위치·임시 파일 경로를 처리한다.
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

# 내부 모듈: 순수 페이지 검사와 실행 폴더 검사를 서로 구분해 검증한다.
from parsing.quality_checks import LIMITS, TableGrid, inspect_page
from parsing.validate_run import inspect_run


ROOT = Path(__file__).resolve().parents[1]


def page_record(content="원본 문장", label="text", number=1):
    """내용·좌표를 독립적으로 바꿀 수 있는 작은 페이지 기록이다."""
    doc_id = hashlib.sha256(b"fixture").hexdigest()
    page_id = f"{doc_id}:p{number}"
    return {
        "document_name": "fixture", "document_id": doc_id,
        "page_id": page_id, "page_number": number, "page_index": number - 1,
        "page_numbering": "pdf_viewer_1_based", "status": "ok",
        "source_sha256": hashlib.sha256(b"source fixture").hexdigest(),
        "page_image": {
            "path": f"documents/{doc_id}/page_{number:05d}.png",
            "sha256": hashlib.sha256(b"image fixture").hexdigest(),
        },
        "render": {"width": 100, "height": 100,
                   "pixel_to_pdf_matrix": [0.5, 0, 0, 0.5, 0, 0]},
        "parsing": {
            "status": "ok",
            "blocks": [{"element_id": f"{page_id}:vl:0", "source_list_index": 0,
                        "label": label, "content": content,
                        "bbox_image_pixels": [1, 2, 5, 6],
                        "bbox_pdf_points": [0.5, 1, 2.5, 3]}],
            "raw": {"res": {"parsing_res_list": [{
                "block_label": label, "block_content": content,
                "block_bbox": [1, 2, 5, 6], "block_id": 0,
            }]}},
        },
    }


class GridTests(unittest.TestCase):
    def test_merged_header_is_not_a_missing_cell(self):
        html = ('<table><tr><th rowspan="2">name</th><th colspan="2">year</th></tr>'
                '<tr><th>2020</th><th>2021</th></tr></table>')
        self.assertEqual(TableGrid().measure(html),
                         {"rows": 2, "columns": 3, "unfilled_slots": 0})

    def test_row_completely_covered_by_rowspan(self):
        html = '<table><tr><td rowspan="2">value</td></tr><tr></tr></table>'
        self.assertEqual(TableGrid().measure(html)["unfilled_slots"], 0)

    def test_crossing_spans_are_rejected(self):
        html = ('<table><tr><td>a</td><td rowspan="2">b</td></tr>'
                '<tr><td colspan="2">c</td></tr></table>')
        with self.assertRaises(ValueError):
            TableGrid().measure(html)

    def test_unclosed_table_is_not_accepted(self):
        with self.assertRaises(ValueError):
            TableGrid().measure('<table><tr><td>value</td></tr>')

    def test_large_span_is_bounded_before_allocation(self):
        with self.assertRaises(ValueError):
            TableGrid().measure('<table><tr><td colspan="1000000000">x</td></tr></table>')


class PageTests(unittest.TestCase):
    def test_repetition_is_suspected_and_input_is_unchanged(self):
        record = page_record("China, " * 100)
        original = copy.deepcopy(record)
        report = inspect_page(record)
        repeated = next(f for f in report["findings"] if f["rule"] == "dominant_token")
        self.assertEqual(repeated["status"], "suspected")
        self.assertEqual(repeated["evidence"]["count"], 100)
        self.assertEqual(record, original)

    def test_normal_text_is_not_declared_correct(self):
        report = inspect_page(page_record())
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["review_status"], "not_reviewed")

    def test_nan_coordinate_is_explicit_error(self):
        record = page_record()
        record["parsing"]["blocks"][0]["bbox_pdf_points"][0] = float("nan")
        self.assertIn("coordinate_structure", {f["rule"] for f in inspect_page(record)["findings"]})

    def test_affine_rotation_uses_all_four_corners(self):
        record = page_record()
        record["render"]["pixel_to_pdf_matrix"] = [0, 1, -1, 0, 10, 0]
        record["parsing"]["blocks"][0]["bbox_pdf_points"] = [4, 1, 8, 5]
        self.assertEqual(inspect_page(record)["findings"], [])

    def test_wrong_pdf_scale_is_explicit_error(self):
        record = page_record()
        record["parsing"]["blocks"][0]["bbox_pdf_points"] = [1, 2, 5, 6]
        self.assertIn("coordinate_mapping", {f["rule"] for f in inspect_page(record)["findings"]})

    def test_grouped_empty_block_is_not_declared_ocr_loss(self):
        record = page_record()
        parsed = record["parsing"]
        second = copy.deepcopy(parsed["blocks"][0])
        second.update(element_id="second", source_list_index=1, content="")
        parsed["blocks"].append(second)
        raw = parsed["raw"]["res"]["parsing_res_list"]
        raw[0]["group_id"] = 1
        raw.append({"block_content": "", "block_bbox": [1, 2, 5, 6], "group_id": 1})
        findings = inspect_page(record)["findings"]
        self.assertEqual([f["rule"] for f in findings], ["merged_content_scope"])
        self.assertEqual(findings[0]["status"], "suspected")

    def test_invalid_grid_is_unresolved(self):
        record = page_record('<table><tr><td rowspan="9">x</td></tr></table>', "table")
        findings = inspect_page(record)["findings"]
        self.assertEqual(findings[0]["rule"], "table_grid")
        self.assertEqual(findings[0]["status"], "unresolved")

    def test_grid_does_not_claim_to_check_numeric_meaning(self):
        report = inspect_page(page_record('<table><tr><td>0.85</td></tr></table>', "table"))
        self.assertEqual(report["review_status"], "not_reviewed")
        self.assertEqual(report["tables"][0]["columns"], 1)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.run_dir = self.base / "run"
        self.pdf_dir = self.base / "pdfs"
        self.pdf_dir.mkdir()
        (self.pdf_dir / "fixture.pdf").write_bytes(b"source fixture")
        self.record = page_record()
        self.page = self.run_dir / self.record["page_image"]["path"]
        self.page.parent.mkdir(parents=True)
        # 이미지 디코딩이 아닌 bytes 해시 검사이므로 임의의 작은 bytes를 쓴다.
        self.page.write_bytes(b"image fixture")
        self.page = self.page.with_suffix(".json")
        self.write_record()
        self.selection = self.base / "selection.json"
        self.selection.write_text(json.dumps({
            "page_numbering": "pdf_viewer_1_based",
            "documents": [{"document_name": "fixture", "page_numbers": [1]}],
        }), encoding="utf-8")

    def write_record(self):
        self.page.write_text(json.dumps(self.record), encoding="utf-8")

    def inspect(self):
        return inspect_run(self.run_dir, self.selection, self.pdf_dir)

    def command(self, *extra):
        return subprocess.run(
            [sys.executable, "-B", "-m", "parsing.validate_run",
             "--run-dir", str(self.run_dir), "--selection", str(self.selection),
             "--pdf-dir", str(self.pdf_dir), *map(str, extra)],
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )

    def test_changed_image_is_detected(self):
        self.page.with_suffix(".png").write_bytes(b"changed image")
        findings = self.inspect()["pages"][0]["findings"]
        self.assertIn("image_hash", {f["rule"] for f in findings})

    def test_missing_pdf_remains_unresolved(self):
        (self.pdf_dir / "fixture.pdf").unlink()
        findings = self.inspect()["pages"][0]["findings"]
        missing = next(f for f in findings if f["rule"] == "source_hash")
        self.assertEqual(missing["status"], "unresolved")

    def test_wrong_page_identity_is_detected(self):
        self.record["page_index"] = 5
        self.write_record()
        self.assertIn("page_identity", {f["rule"] for f in self.inspect()["pages"][0]["findings"]})

    def test_wrong_image_reference_is_detected(self):
        self.record["page_image"]["path"] = "another.png"
        self.write_record()
        self.assertIn("image_reference", {f["rule"] for f in self.inspect()["pages"][0]["findings"]})

    def test_unreadable_page_does_not_hide_other_results(self):
        self.page.write_text("{invalid", encoding="utf-8")
        report = self.inspect()
        self.assertEqual(report["summary"]["page_count"], 1)
        self.assertEqual(report["pages"][0]["findings"][0]["rule"], "page_inspection")

    def test_export_is_outside_input_and_exclusive(self):
        before = {p: p.read_bytes() for p in self.run_dir.rglob("*") if p.is_file()}
        output = self.base / "report.json"
        first = self.command("--output", output)
        self.assertEqual(first.returncode, 0, first.stderr)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(report["limits"], LIMITS)
        self.assertEqual(report["summary"]["page_count"], 1)
        self.assertIsNone(report["summary"]["quality_accuracy"])
        exported = output.read_bytes()
        second = self.command("--output", output)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(output.read_bytes(), exported)
        self.assertEqual(before, {p: p.read_bytes() for p in self.run_dir.rglob("*") if p.is_file()})

    def test_output_inside_input_is_rejected(self):
        output = self.run_dir / "report.json"
        result = self.command("--output", output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
