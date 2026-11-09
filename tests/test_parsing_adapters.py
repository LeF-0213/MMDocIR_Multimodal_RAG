"""파서 어댑터·결과 변환·그림 저장이 모델 결과를 잃지 않는지 검증한다.

모델을 로딩하지 않고, 결과 객체와 PDF는 테스트 안에서 만든다.
검증하는 경계는 병합 설정의 기록·덮어쓰기, 병합 그룹 번호 보존,
그림 참조 경로의 폴더 이탈 거부, 그림 수집 실패가 문자 결과를 지우지 않는지,
run.py가 저장한 그림을 validate_run.py가 참조와 해시로 다시 찾는지다.
인식 품질이나 실제 PaddleX 결과의 정확성을 확인하는 테스트는 아니다.
"""

# 표준 라이브러리: tempfile은 검사 뒤 지워지는 임시 폴더, unittest는 테스트와 단언,
# mock.patch는 모델 로딩·HTTP를 대체하고, SimpleNamespace는 결과 객체의 속성만 흉내 낸다.
# Path는 임시 파일 경로를 다룬다.
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# NumPy(별칭 np)는 모델 입력 배열을, Pillow의 Image는 PaddleX가 반환하는 그림 객체를 만든다.
# PyMuPDF는 테스트용 한 페이지 PDF를 메모리에서 만든다.
import numpy as np
import pymupdf
from PIL import Image

# 내부 모듈: 검증 대상인 어댑터 설정·결과 변환·저장·실행 진입점·검사기를 가져온다.
from common.files import file_hash, read_json, write_json
from parsing import vl_parser
from parsing.model_result import normalize_result_blocks, predict_page, result_images
from parsing.result_store import save_bytes_under
from parsing.run import main as parsing_main
from parsing.validate_run import inspect_run


def raw_result(content="문장", group_id=0):
    """parsing_res_list 한 블록짜리 모델 JSON이다."""
    block = {"block_label": "text", "block_content": content, "block_bbox": [0, 0, 10, 10],
             "block_id": 0, "block_order": 1}
    if group_id is not None:
        block["group_id"] = group_id
    return {"res": {"parsing_res_list": [block]}}


def fake_result(raw, images=None, markdown_error=None):
    """json·markdown 속성만 가진 결과 객체다. markdown_error가 있으면 속성 접근이 실패한다."""
    class Result:
        json = raw

        @property
        def markdown(self):
            if markdown_error:
                raise markdown_error
            return {"markdown_texts": "", "markdown_images": images or {}}
    return Result()


def fake_parser(result):
    return SimpleNamespace(predict=MagicMock(side_effect=lambda **kwargs: iter([result])))


IMAGE = np.zeros((4, 4, 3), dtype=np.uint8)


class OptionTests(unittest.TestCase):
    def test_merge_is_disabled_by_default_and_can_be_overridden(self):
        self.assertIs(vl_parser.parser_options("cpu")["merge_layout_blocks"], False)
        overridden = vl_parser.parser_options("cpu", overrides={"merge_layout_blocks": True})
        self.assertIs(overridden["merge_layout_blocks"], True)


class ResultTests(unittest.TestCase):
    def test_group_id_is_kept_and_missing_group_is_not_guessed(self):
        self.assertEqual(normalize_result_blocks(raw_result(group_id=3), "p", "s", "vl")[0]["model_group_id"], 3)
        self.assertIsNone(normalize_result_blocks(raw_result(group_id=None), "p", "s", "vl")[0]["model_group_id"])

    def test_images_are_encoded_as_jpeg(self):
        encoded = result_images(fake_result(raw_result(), {"imgs/a.jpg": Image.new("RGBA", (3, 2))}))
        self.assertEqual(list(encoded), ["imgs/a.jpg"])
        self.assertTrue(encoded["imgs/a.jpg"].startswith(b"\xff\xd8"))

    def test_escaping_or_absolute_references_are_rejected(self):
        for reference in ("../a.jpg", "/tmp/a.jpg", "imgs//a.jpg", "imgs\\a.jpg", ""):
            with self.subTest(reference=reference), self.assertRaises(ValueError):
                result_images(fake_result(raw_result(), {reference: Image.new("RGB", (1, 1))}))

    def test_array_image_is_not_converted_by_guessing_color_order(self):
        with self.assertRaises(TypeError):
            result_images(fake_result(raw_result(), {"imgs/a.jpg": np.zeros((1, 1, 3), np.uint8)}))

    def test_image_failure_keeps_text_result(self):
        output = predict_page(fake_parser(fake_result(raw_result(), markdown_error=RuntimeError("x"))),
                              IMAGE, "p", "s", "vl")
        self.assertEqual(output["status"], "ok")
        self.assertEqual(output["blocks"][0]["content"], "문장")
        self.assertEqual(output["result_images"]["status"], "failed")
        self.assertNotIn("result_image_bytes", output)

    def test_save_under_rejects_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "page"
            with self.assertRaises(ValueError):
                save_bytes_under(base, "../outside.jpg", b"x")
            self.assertFalse((Path(directory) / "outside.jpg").exists())


class RunImageTests(unittest.TestCase):
    def test_saved_images_resolve_table_references(self):
        content = '<table><tr><td><img src="imgs/icon.jpg" alt="Image"" /></td></tr></table>'
        raw = {"res": {"parsing_res_list": [{"block_label": "table", "block_content": content,
                                             "block_bbox": [0, 0, 10, 10], "block_id": 0}]}}
        parser = fake_parser(fake_result(raw, {"imgs/icon.jpg": Image.new("RGB", (2, 2))}))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with pymupdf.open() as document:
                document.new_page()
                pdf = document.tobytes()
            selection = root / "selection.json"
            write_json(selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
                {"document_name": "synthetic", "page_numbers": [1]}]})
            output = root / "run"
            with patch("parsing.vl_parser.build_parser", return_value=parser), \
                 patch("parsing.run.read_pdf_bytes", return_value=pdf):
                self.assertEqual(parsing_main(["--base-url", "https://example.invalid/", "--selection",
                                               str(selection), "--output-dir", str(output)]), 0)

            page = next(output.glob("documents/*/page_00001.json"))
            record = read_json(page)
            item = record["parsing"]["result_images"]["items"][0]
            self.assertEqual(item["reference"], "imgs/icon.jpg")
            self.assertEqual(file_hash(output / item["path"]), item["sha256"])
            self.assertNotIn("result_image_bytes", record["parsing"])

            # PDF 폴더 없이 실행 폴더의 원본 사본과 저장 그림으로 검사가 끝나야 한다.
            report = inspect_run(output, selection)
            self.assertEqual(report["source_reference"], "run_copy")
            self.assertEqual(report["pages"][0]["findings"], [])

            (output / item["path"]).write_bytes(b"changed")
            findings = inspect_run(output, selection)["pages"][0]["findings"]
            self.assertEqual([(f["rule"], f["evidence"]["reason"]) for f in findings],
                             [("html_image_reference", "그림 파일 해시 불일치")])


if __name__ == "__main__":
    unittest.main()
