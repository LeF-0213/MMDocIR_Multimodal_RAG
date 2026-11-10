"""구조 기반 청킹의 경계 조건을 검증한다.

토크나이저 다운로드 없이 단어 수 기반 모의 측정기를 쓴다. 실제 토큰 수가 아니라 분할 규칙을 검사한다.
페이지 JSON·파싱 실행 폴더는 테스트 안에서 만들고, 실제 결과 경로·개수를 고정하지 않는다.
"""

# 표준 라이브러리: copy는 설정 변형, Path·tempfile은 임시 실행 폴더, unittest·mock은 테스트와 측정기 대체다.
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

# 내부 모듈: common은 JSON·해시, chunking은 표 변환·페이지 청킹·실행 진입점이다.
from common.files import digest, file_hash, read_json, write_json
from chunking.structure import build_page_chunks
from chunking.tables import html_table_rows, pipe_table_rows

# 모의 측정기의 끝 토큰 수다. 실제 Qwen 토크나이저도 문서 입력 끝에 토큰 하나를 붙인다.
FAKE_SPECIAL_TOKENS = 1


class WordCounter:
    """공백 단위 단어 수 + 끝 토큰을 토큰 수로 본다. 분할 규칙만 확인하려는 모의 객체다."""

    model_position_limit = None
    record = {"repo_id": "synthetic", "revision": "synthetic", "weights_downloaded": False}

    def count(self, text):
        return len(text.split()) + FAKE_SPECIAL_TOKENS

    def split(self, text, limit):
        words = text.split()
        size = max(1, limit - FAKE_SPECIAL_TOKENS)
        return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def settings(max_tokens=50, overlap_tokens=0):
    return {"max_tokens": max_tokens, "overlap_tokens": overlap_tokens, "caption_max_gap_points": 20,
            "table_header_rows_without_th": 1,
            "kind_markers": {"table": "[Table]", "chart": "[Chart]", "visual": "[Figure]", "caption": "Caption:"},
            "labels": {"title_levels": [["doc_title"], ["paragraph_title"]], "text": ["text"],
                       "table": ["table"], "chart": ["chart"], "visual": ["image"], "caption": ["figure_title"],
                       "excluded": ["header", "number"]}}


def block(index, label, content, top=0, bottom=10):
    return {"element_id": f"doc:p1:vl:{index}", "label": label, "content": content,
            "bbox_pdf_points": [0, top, 100, bottom], "bbox_image_pixels": [0, top * 2, 200, bottom * 2]}


def page(blocks, status="ok"):
    return {"page_id": "doc:p1", "document_id": "doc", "document_name": "synthetic", "page_number": 1,
            "status": status, "parsing": {"status": status, "blocks": blocks}}


class TableTests(unittest.TestCase):
    def test_merged_cells_and_two_header_rows_keep_value_meaning(self):
        html = ("<table><tr><th rowspan='2'>Item</th><th colspan='2'>Year ended</th></tr>"
                "<tr><th>2018</th><th>2017</th></tr>"
                "<tr><td>Sales</td><td>32</td><td></td></tr><tr><td><img src='a.jpg'></td><td>1</td><td>2</td></tr></table>")
        rows = html_table_rows(html, header_rows_without_th=1)
        self.assertTrue(rows.ok)
        self.assertEqual(rows.headers, ["Item", "Year ended / 2018", "Year ended / 2017"])
        # 빈 칸은 0으로 바꾸지 않고 쌍을 만들지 않는다. 그림 칸은 존재만 표시한다.
        self.assertEqual(rows.row_texts()[0], "Item: Sales; Year ended / 2018: 32")
        self.assertEqual((rows.empty_cells, rows.image_cells), (1, 1))

    def test_table_without_th_uses_configured_header_rows(self):
        rows = html_table_rows("<table><tr><td>Name</td><td>Age</td></tr><tr><td>Kim</td><td>40</td></tr>",
                               header_rows_without_th=1)
        self.assertEqual(rows.row_texts(), ["Name: Kim; Age: 40"])

    def test_pipe_table_and_non_table_text(self):
        rows = pipe_table_rows("Year | Value\n---|---\n2018 | 5")
        self.assertEqual(rows.row_texts(), ["Year: 2018; Value: 5"])
        self.assertFalse(pipe_table_rows("plain chart description").ok)


class StructureTests(unittest.TestCase):
    def test_headings_captions_exclusions_and_block_coverage(self):
        blocks = [block(0, "header", "Running header"), block(1, "doc_title", "Annual Report"),
                  block(2, "paragraph_title", "Risk"), block(3, "text", "Risk text."),
                  block(4, "text", ""), block(5, "figure_title", "Figure 1", top=100, bottom=105),
                  block(6, "image", "chart words", top=106, bottom=200),
                  block(7, "paragraph_title", "Outlook"), block(8, "custom_label", "Outlook text."),
                  block(9, "figure_title", "Far caption", top=900, bottom=905)]
        chunks, excluded, stats = build_page_chunks(page(blocks), settings(), WordCounter(), {}, "vl")
        reasons = {e["element_id"]: e["reason"] for e in excluded}
        self.assertEqual(reasons, {"doc:p1:vl:0": "excluded_label", "doc:p1:vl:4": "empty_content"})
        texts = {c["kind"]: c for c in chunks}
        self.assertTrue(chunks[0]["text"].startswith("Annual Report > Risk\n\nRisk text."))
        self.assertEqual(texts["visual"]["caption_element_ids"], ["doc:p1:vl:5"])
        self.assertIn("Caption: Figure 1", texts["visual"]["text"])
        # 대상과 멀리 떨어진 캡션은 본문으로 남고, 상위 제목은 유지된 채 하위 제목만 바뀐다.
        self.assertEqual(stats["captions_as_text"], 1)
        self.assertEqual(chunks[-1]["heading"], "Annual Report > Outlook")
        self.assertEqual(stats["unmapped_labels"], {"custom_label": 1})
        covered = ({e for c in chunks for e in c["element_ids"] + c["heading_element_ids"]}
                   | set(reasons))
        self.assertEqual(covered, {b["element_id"] for b in blocks})
        self.assertEqual(len({c["chunk_id"] for c in chunks}), len(chunks))

    def test_long_text_splits_by_sentence_then_token_boundary_without_exceeding_limit(self):
        sentence = "word " * 8
        long_sentence = "x " * 40
        blocks = [block(0, "text", f"{sentence.strip()}. {sentence.strip()}. {long_sentence.strip()}")]
        chunks, _, stats = build_page_chunks(page(blocks), settings(max_tokens=20), WordCounter(), {}, "vl")
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(c["token_count"] <= 20 for c in chunks))
        self.assertGreater(stats["hard_splits"], 0)
        self.assertTrue(any("hard_split" in c["flags"] for c in chunks))
        self.assertEqual([c["split"]["index"] for c in chunks], list(range(len(chunks))))

    def test_table_rows_split_with_repeated_header_and_newlines(self):
        rows = "".join(f"<tr><td>r{i}</td><td>{i}</td></tr>" for i in range(12))
        blocks = [block(0, "paragraph_title", "Data"), block(1, "table", f"<table><tr><td>Name</td><td>Value</td></tr>{rows}</table>")]
        chunks, _, _ = build_page_chunks(page(blocks), settings(max_tokens=20), WordCounter(), {}, "vl")
        tables = [c for c in chunks if c["kind"] == "table"]
        self.assertGreater(len(tables), 1)
        for chunk in tables:
            self.assertTrue(chunk["text"].startswith("Data\n[Table]\n\n"))
            # 행 하나가 한 줄이고, 행마다 열 제목이 붙어 조각만 봐도 값의 의미가 남는다.
            lines = chunk["text"].split("\n\n", 1)[1].split("\n")
            self.assertEqual(len(lines), chunk["table"]["piece_rows"])
            self.assertTrue(all(line.startswith("Name: r") and "; Value: " in line for line in lines))
            self.assertEqual(chunk["table"]["source_content"].count("<tr>"), 13)

    def test_overlap_repeats_tail_but_not_whole_chunk(self):
        text = " ".join(f"Sentence number {i} ends here." for i in range(10))
        chunks, _, _ = build_page_chunks(page([block(0, "text", text)]), settings(max_tokens=16, overlap_tokens=6),
                                         WordCounter(), {}, "vl")
        self.assertGreater(len(chunks), 1)
        for before, after in zip(chunks, chunks[1:]):
            last = before["text"].split(". ")[-1]
            self.assertIn(last.rstrip("."), after["text"])
            self.assertNotEqual(before["text"], after["text"])

    def test_flags_follow_blocks_and_failed_page_is_excluded_as_a_whole(self):
        blocks = [block(0, "text", "repeated repeated repeated")]
        chunks, _, _ = build_page_chunks(page(blocks), settings(), WordCounter(),
                                         {"doc:p1:vl:0": {"dominant_token"}}, "vl")
        self.assertEqual(chunks[0]["flags"], ["dominant_token"])
        chunks, excluded, _ = build_page_chunks(page(blocks, status="failed"), settings(), WordCounter(), {}, "vl")
        self.assertEqual(chunks, [])
        self.assertTrue(excluded[0]["reason"].startswith("page_not_parsed"))

    def test_title_without_following_content_is_kept_and_flagged(self):
        blocks = [block(0, "paragraph_title", "Mission"), block(1, "paragraph_title", "Vision"),
                  block(2, "text", "Vision text.")]
        chunks, _, stats = build_page_chunks(page(blocks), settings(), WordCounter(), {}, "vl")
        self.assertEqual(stats["title_only_chunks"], 1)
        self.assertEqual([c["text"] for c in chunks if "title_only" in c["flags"]], ["Mission"])


class RunTests(unittest.TestCase):
    def make_inputs(self, root):
        """파싱 실행 폴더 하나와 청킹 설정을 임시 폴더에 만든다."""
        run = root / "parsing_vl_run"; doc_id = digest("synthetic".encode("utf-8"))
        (run / "documents" / doc_id).mkdir(parents=True)
        write_json(run / "run.json", {"schema_version": "project.parsing.v1", "parser": "vl"})
        record = page([block(0, "paragraph_title", "Title"), block(1, "text", "Body text.")])
        record.update(page_id=f"{doc_id}:p1", document_id=doc_id)
        for item in record["parsing"]["blocks"]:
            item["element_id"] = item["element_id"].replace("doc:", f"{doc_id}:")
            item["content"] = item["content"]
        record["parsing"]["raw"] = {"res": {"parsing_res_list": [
            {"block_content": b["content"], "block_bbox": b["bbox_image_pixels"]} for b in record["parsing"]["blocks"]]}}
        for index, item in enumerate(record["parsing"]["blocks"]):
            item["source_list_index"] = index
        record["render"] = {"pixel_to_pdf_matrix": [0.5, 0, 0, 0.5, 0, 0], "width": 1000, "height": 1000}
        write_json(run / "documents" / doc_id / "page_00001.json", record)
        write_json(root / "selection.json", {"page_numbering": "pdf_viewer_1_based",
                                             "documents": [{"document_name": "synthetic", "page_numbers": [1, 2]}]})
        config = {"schema_version": "project.chunking.v1", "input_runs": [str(run)], "selection": "selection.json",
                  "output_root": "out", "tokenizer": {"repo_id": "synthetic", "revision": "synthetic",
                  "tokenizer_file": "tokenizer.json", "model_config_file": "config.json", "add_special_tokens": True},
                  "flag_rules": ["dominant_token"], **copy.deepcopy(settings())}
        write_json(root / "chunking.json", config)
        return run

    def test_run_writes_new_folder_and_reports_missing_page_without_touching_input(self):
        from chunking.run import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); run = self.make_inputs(root)
            before = {p: file_hash(p) for p in run.rglob("*") if p.is_file()}
            with patch("chunking.tokens.TokenCounter", return_value=WordCounter()):
                self.assertEqual(main(["--config", str(root / "chunking.json")]), 1)
            self.assertEqual(before, {p: file_hash(p) for p in run.rglob("*") if p.is_file()})
            output = next((root / "out").glob("chunking_vl_*"))
            summary = read_json(output / "summary.json")
            # 선정 목록의 2페이지가 입력에 없으므로 숨기지 않고 문제로 보고한다.
            self.assertEqual(summary["status"], "completed_with_problems")
            self.assertEqual(summary["pages"]["missing"], [{"document_name": "synthetic", "page_number": 2}])
            self.assertEqual(summary["blocks"]["unassigned"], [])
            lines = (output / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), summary["chunks"]["total"])
            self.assertEqual(read_json(output / "run.json")["tokenizer"]["weights_downloaded"], False)

    def test_check_creates_nothing(self):
        from chunking.run import main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); self.make_inputs(root)
            with patch("chunking.tokens.TokenCounter") as counter:
                self.assertEqual(main(["--config", str(root / "chunking.json"), "--check"]), 0)
            counter.assert_not_called()
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
