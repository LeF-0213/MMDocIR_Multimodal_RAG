"""생성 한도 블록 재인식(parsing/rerecognize.py)의 대상 선택·채택·거부 경계를 검증한다.

모델은 호출마다 정해 둔 글자를 돌려주는 모의 객체로 바꾸고, 토큰 수는 테스트 안에서 만든
단어 단위 tokenizer.json으로 센다. 실제 PaddleOCR-VL 가중치·출력은 쓰지 않는다.
실행 폴더·원본 PDF·설정은 임시 폴더에 만들며 실제 결과 경로·해시·개수를 고정하지 않는다.
"""

# 표준 라이브러리: json은 결과 JSONL 읽기, Path·tempfile은 임시 실행 폴더, SimpleNamespace는 모의 결과,
# patch는 모델 로딩 교체, unittest는 테스트 구성을 맡는다.
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

# 외부 라이브러리: pymupdf는 원본 PDF 사본, tokenizers는 단어 단위 모의 tokenizer.json을 만든다.
import pymupdf
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace

# 내부 모듈: common은 해시·JSON, rerecognize는 검증 대상, quality_checks는 반복 신호의 최소 단어 수다.
from common.files import digest, file_hash, read_json, write_json
from parsing import rerecognize
from parsing.quality_checks import LIMITS

# 테스트용 생성 한도다. 실제 모델 한도와 무관하게 작게 잡되, 반복 신호를 계산하는 최소 단어 수
# (quality_checks.LIMITS["minimum_tokens"])보다 크게 둬서 한도 전에 끝난 반복 출력도 만들 수 있게 한다.
LIMIT = LIMITS["minimum_tokens"] * 2
DOCUMENT = "fixture-document"
WORDS = ("loop", "real", "text", "table", "cell", "short", "answer", "body")


def write_tokenizer(path: Path):
    """공백으로 단어를 나누고 단어 하나를 토큰 하나로 세는 tokenizer.json이다."""
    vocab = {"[UNK]": 0, **{word: i + 1 for i, word in enumerate(WORDS)}}
    tokenizer = Tokenizer(WordLevel(vocab, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = Whitespace()
    tokenizer.save(str(path))


def repeated(word: str, count: int) -> str:
    return " ".join([word] * count)


class Fixture:
    """한 페이지 VL 실행: 한도에 닿은 image·table 블록과 정상 text 블록을 담는다."""

    def __init__(self, root: Path):
        self.root = root
        self.run = root / "runs" / "parsing_vl_fixture"
        doc_id = digest(DOCUMENT.encode("utf-8"))
        self.doc = self.run / "documents" / doc_id
        self.doc.mkdir(parents=True)
        with pymupdf.open() as document:
            document.new_page().insert_text((20, 30), "real text body")
            (self.doc / "source.pdf").write_bytes(document.tobytes())
        write_json(self.run / "run.json", {"parser": "vl"})
        write_json(self.run / "selection.json", {"page_numbering": "pdf_viewer_1_based",
                                                 "documents": [{"document_name": DOCUMENT, "page_numbers": [1]}]})
        page_id = f"{doc_id}:p1"

        def block(index, label, content):
            return {"element_id": f"{page_id}:vl:{index}", "label": label, "content": content,
                    "bbox_pdf_points": [10, 10, 200, 60], "bbox_image_pixels": [20, 20, 400, 120]}

        self.page = self.doc / "page_00001.json"
        write_json(self.page, {
            "page_id": page_id, "document_id": doc_id, "document_name": DOCUMENT, "page_number": 1,
            "page_index": 0, "source_sha256": file_hash(self.doc / "source.pdf"),
            "parsing": {"blocks": [block(0, "image", repeated("loop", LIMIT)),
                                   block(1, "text", "short answer"),
                                   block(2, "table", repeated("cell", LIMIT))]}})
        self.model_dir = root / "model"
        self.model_dir.mkdir()
        write_tokenizer(self.model_dir / "tokenizer.json")
        self.output = root / "outputs"
        self.config = root / "config.json"
        write_json(self.config, {
            "schema_version": rerecognize.CONFIG_SCHEMA, "input_run": str(self.run), "output_root": str(self.output),
            "model": {"model_name": "fixture-model", "model_dir": str(self.model_dir), "device": "cpu"},
            "generation_limit_tokens": LIMIT, "limit_margin_tokens": 0, "max_new_tokens": LIMIT,
            "render_dpis": [72, 144], "min_pixels": 1, "max_pixels": 10_000_000, "channel_order": "bgr",
            "prompts": {"default": "OCR:", "by_label": {"chart": "Chart Recognition:"}},
            "unsupported_labels": ["table"],
            "rejection_rules": ["dominant_token", "repeated_trigrams", "long_number_sequence", "output_length"]})

    def run_with(self, outputs: list[str]):
        """모델이 호출 순서대로 outputs를 돌려준다. 받은 입력 배열도 기록한다."""
        calls = []

        def predict(inputs, **kwargs):
            calls.append({"image": inputs[0]["image"], "query": inputs[0]["query"], **kwargs})
            return iter([{"result": outputs[len(calls) - 1]}])

        with patch.object(rerecognize, "load_model", return_value=SimpleNamespace(predict=predict)):
            code = rerecognize.main(["--config", str(self.config)])
        folder = next(self.output.glob("rerecognition_vl_*"))
        rows = [json.loads(line) for line in (folder / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        return code, calls, rows, folder


class RerecognizeTests(unittest.TestCase):
    def test_second_resolution_is_accepted_and_input_run_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            before = {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()}
            code, calls, rows, folder = fixture.run_with([repeated("loop", LIMIT), "real text body"])
            self.assertEqual(code, 0)
            # 정상 text 블록은 대상이 아니고, 표는 전처리를 재현하지 않으므로 모델에 넣지 않는다.
            by_label = {row["label"]: row for row in rows}
            self.assertEqual(set(by_label), {"image", "table"})
            self.assertEqual(by_label["table"]["status"], rerecognize.SKIPPED)
            image = by_label["image"]
            self.assertEqual(image["status"], rerecognize.ACCEPTED)
            self.assertEqual(image["content"], "real text body")
            # 첫 DPI는 한도에 닿아 거부, 두 번째 DPI를 채택한다. 두 번째 입력이 더 크다.
            self.assertEqual([a["accepted"] for a in image["attempts"]], [False, True])
            self.assertFalse(image["attempts"][0]["ended_before_limit"])
            self.assertGreater(calls[1]["image"].shape[1], calls[0]["image"].shape[1])
            self.assertEqual(calls[0]["query"], "OCR:")
            self.assertEqual(calls[0]["max_new_tokens"], LIMIT)
            self.assertTrue((folder / image["attempts"][1]["crop"]).is_file())
            self.assertEqual(read_json(folder / "summary.json")["accepted"], 1)
            self.assertEqual(before, {p: file_hash(p) for p in fixture.run.rglob("*") if p.is_file()})

    def test_bgr_channel_order_reverses_rendered_rgb(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            _, calls, rows, folder = fixture.run_with(["real text body"])
            crop = rows[0]["attempts"][0]["crop"]
            from PIL import Image
            import numpy as np
            rgb = np.array(Image.open(folder / crop))
            self.assertTrue((calls[0]["image"] == rgb[..., ::-1]).all())

    def test_repetition_without_reaching_limit_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            # 한도보다 짧게 끝났지만 한 단어가 대부분이면 반복 신호로 거부한다.
            short_loop = repeated("loop", LIMIT // 2)
            code, _, rows, _ = fixture.run_with([short_loop, short_loop])
            image = next(row for row in rows if row["label"] == "image")
            self.assertEqual(code, 1)
            self.assertEqual(image["status"], rerecognize.UNRESOLVED)
            self.assertTrue(all(a["ended_before_limit"] for a in image["attempts"]))
            self.assertIn("dominant_token", image["attempts"][0]["findings"])
            self.assertIsNone(image["content"])

    def test_changed_source_pdf_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            with pymupdf.open() as document:
                document.new_page().insert_text((20, 30), "changed")
                (fixture.doc / "source.pdf").write_bytes(document.tobytes())
            with patch.object(rerecognize, "load_model", return_value=SimpleNamespace(predict=None)):
                with self.assertRaises(SystemExit) as raised:
                    rerecognize.main(["--config", str(fixture.config)])
            self.assertEqual(raised.exception.code, 2)

    def test_check_lists_targets_without_loading_model(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Fixture(Path(directory))
            with patch.object(rerecognize, "load_model") as load:
                self.assertEqual(rerecognize.main(["--config", str(fixture.config), "--check"]), 0)
            load.assert_not_called()
            self.assertFalse(fixture.output.exists())


if __name__ == "__main__":
    unittest.main()
