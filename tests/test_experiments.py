"""실험 기록·전후 평가의 의미 있는 경계와 파싱 연결을 검증한다.

모든 PDF·모델·결과·정답은 임시 폴더 안의 모의 데이터로 만든다.
실제 결과 경로·해시·개수를 고정하지 않으며 모델 다운로드와 실제 추론은 하지 않는다.
복구·퇴행·보류, 소스 무결성·배타적 저장·기록 설정의 실제 전달을 확인한다.
"""

# 표준 라이브러리: copy는 독립 전후 입력, hashlib는 모의 원본 해시,
# json은 설정, pathlib·tempfile은 임시 자료, types는 모델 없는 객체 그래프다.
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
# unittest·mock은 경계 조건과 모델·OS 조회의 의도한 실패를 재현한다.
import unittest
from unittest.mock import patch

# 내부 공통·기록·자원·평가·비교 모듈의 공개 연결과 순수 함수를 각각 검사한다.
from experiments.common import (FINISHED_FILE, GOLD_SCHEMA, MANIFEST_FILE, RECORD_SCHEMA,
    clean, digest, file_hash, inside, read_json, source_is_sensitive, write_json)
from experiments.record import ExperimentRecorder, HF_METADATA_DIRECTORY, load_record_config, model_inventory
from experiments.resources import ResourceMonitor
from experiments.evaluate import (EVALUATION_SCHEMA, PDF_COORDINATES, LogicalTable,
    evaluate_pair, region_iou, sentence_result, validate_gold, validate_settings)
from experiments.compare import compare_runs, differences, load_run, main as compare_main, markdown

# 측정 스레드가 테스트의 실행 순서에 개입하지 않도록 긴 간격을 명시한다.
# 구간 시작·종료에서 동기 샘플을 수집하므로 실제 sleep은 필요 없다.
TEST_INTERVAL_SECONDS = 60
TEST_GIT_TIMEOUT_SECONDS = 1


def settings():
    """작은 입력에서 부분 일치·자원 제한을 독립적으로 바꿀 수 있는 평가 설정이다."""
    return {"schema_version": EVALUATION_SCHEMA,
        "normalization": {"unicode_form": "NFC", "collapse_whitespace": True, "casefold": False},
        "sentence": {"minimum_similarity": 0.6, "maximum_join_blocks": 4, "maximum_candidates": 100,
                     "maximum_page_characters": 10000, "maximum_alignment_characters": 1000},
        "position": {"iou_threshold": 0.5, "table_iou_threshold": 0.5},
        "table": {"maximum_grid_slots": 100, "maximum_tables": 10}}


def page(texts, *, table=False, precise=False):
    """같은 모의 원본에서 블록 수·내용만 바꾼 페이지를 만든다."""
    doc = digest(b"synthetic document name")
    return {"document_id": doc, "page_number": 1, "page_numbering": "pdf_viewer_1_based",
        "source_sha256": digest(b"synthetic PDF bytes"), "status": "ok",
        "parsing": {"status": "ok", "raw": {"res": {"model_settings": {"merge_layout_blocks": False}}},
                    "blocks": [{"element_id": f"{doc}:{i}", "label": "table" if table else "text",
                                "content": text, "bbox_pdf_points": [0, 0, 10, 10],
                                **({"position_granularity": "sentence"} if precise else {})}
                               for i, text in enumerate(texts)]}}


def gold_for(record, sentences=None, tables=None):
    """원문 확인을 선언한 모의 정답이다. 실제 문서 정답으로 사용하지 않는다."""
    return {"schema_version": GOLD_SCHEMA, "version": "synthetic.v1", "split": "development",
        "pages": [{"document_id": record["document_id"], "page_number": record["page_number"],
                   "source_sha256": record["source_sha256"], "coordinate_system": PDF_COORDINATES,
                   "review_status": "confirmed", "sentences": sentences or [], "tables": tables or []}]}


def sentence(text="alpha beta", identity="sentence"):
    return {"id": identity, "text": text, "bboxes": [[0, 0, 10, 10]]}


def evaluate(before, after, gold):
    def keyed(p):
        return {} if p is None else {(p["document_id"], p["page_number"]): p}
    return evaluate_pair(keyed(before), keyed(after), gold, settings())


def record_config(root):
    return {"schema_version": RECORD_SCHEMA, "project_root": str(root),
        "source_paths": ["source.py"], "source_suffixes": [".py"],
        "git_timeout_seconds": TEST_GIT_TIMEOUT_SECONDS,
        "memory": {"interval_seconds": TEST_INTERVAL_SECONDS, "include_children": False, "gpu_device": None},
        "experiment": {"purpose": "모의 경계 검증"}, "model_bindings": [],
        "secret_environment_names": [], "environment_packages": [], "pipeline_options": {}, "predict_options": {}}


def legacy_run(root, record):
    """실제 v1의 저장 형식만 따르는 작은 파일을 만든다."""
    root.mkdir()
    write_json(root / "run.json", {"schema_version": "project.parsing.v1", "parser": "synthetic",
                                  "parser_options": {}, "predict_options": {}, "code_sha256": {}})
    write_json(root / "summary.json", {"status": "completed"})
    folder = root / "documents" / record["document_id"]
    folder.mkdir(parents=True)
    write_json(folder / f"page_{record['page_number']:05d}.json", record)
    return root


class CommonTests(unittest.TestCase):
    def test_missing_null_and_bool_int_are_different(self):
        result = differences({"v": None, "typed": False}, {"typed": 0, "added": None})
        self.assertEqual({tuple(d["path"]): d["kind"] for d in result},
                         {("v",): "removed", ("typed",): "type_changed", ("added",): "added"})

    def test_list_order_matters(self):
        self.assertTrue(differences(["first", "second"], ["second", "first"]))

    def test_secret_fields_and_url_credentials_are_removed(self):
        value = clean({"api_key": "synthetic-private", "max_new_tokens": 100,
                       "url": "https://name:password@example.invalid/doc?token=hidden"})
        self.assertNotIn("synthetic-private", json.dumps(value))
        self.assertNotIn("name:password", value["url"])
        self.assertNotIn("hidden", value["url"])
        self.assertEqual(value["max_new_tokens"], 100)

    def test_source_with_literal_secret_is_withheld(self):
        name = "API" + "_KEY"
        data = f'{name} = "synthetic-private"\n'.encode()
        self.assertTrue(source_is_sensitive(data, ".py"))

    def test_strict_json_and_path_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path = root / "input.json"
            for text in ('{"k":1,"k":2}', '{"v":NaN}'):
                path.write_text(text)
                with self.assertRaises(ValueError): read_json(path)
            with self.assertRaises(ValueError): inside(root, "../outside")
            write_json(root / "new.json", {"v": 1})
            with self.assertRaises(FileExistsError): write_json(root / "new.json", {})


class SentenceTests(unittest.TestCase):
    def test_split_blocks_and_empty_blocks_preserve_sentence(self):
        before = page(["alpha", "", "beta"])
        result = sentence_result(before, sentence(), settings())
        self.assertEqual(result["status"], "correct")
        self.assertEqual(result["block_indices"], [0, 2])

    def test_repeated_sentence_is_unresolved(self):
        record = page(["alpha beta", "alpha beta"])
        report = evaluate(record, record, gold_for(record, [sentence()]))
        self.assertEqual(report["sentences"][0]["transition"], "unresolved")
        self.assertEqual(report["summary"]["sentences"]["before"]["confirmed_correct_rate"], 0)

    def test_recovery_and_new_missing_are_separate(self):
        before = page(["stable information"]); after = page(["alpha beta"])
        gold = gold_for(before, [sentence(), sentence("stable information", "stable")])
        report = evaluate(before, after, gold)
        self.assertEqual(report["summary"]["sentences"]["transition_counts"],
                         {"recovered": 1, "new_missing": 1})

    def test_typo_is_not_correct(self):
        record = page(["alpha betx"])
        result = sentence_result(record, sentence(), settings())
        self.assertEqual(result["status"], "incorrect")

    def test_same_text_is_not_counted_for_overlapping_gold(self):
        record = page(["alpha beta"])
        report = evaluate(record, record, gold_for(record, [sentence(), sentence("alpha beta", "duplicate-content")]))
        self.assertEqual(report["summary"]["sentences"]["before"]["counts"], {"unresolved": 2})

    def test_source_change_does_not_become_new_missing(self):
        before = page(["alpha beta"]); after = copy.deepcopy(before)
        after["source_sha256"] = digest(b"different PDF")
        report = evaluate(before, after, gold_for(before, [sentence()]))
        self.assertEqual(report["sentences"][0]["after"]["status"], "source_mismatch")
        self.assertEqual(report["sentences"][0]["transition"], "unresolved")

    def test_missing_page_stays_in_denominator(self):
        before = page(["alpha beta"])
        report = evaluate(before, None, gold_for(before, [sentence()]))
        self.assertEqual(report["summary"]["sentences"]["denominator"], 1)
        self.assertEqual(report["sentences"][0]["transition"], "new_missing")

    def test_proxy_location_is_not_precise_location(self):
        record = page(["alpha beta"])
        result = sentence_result(record, sentence(), settings())
        self.assertEqual(result["position"]["status"], "proxy_pass")
        record["parsing"]["blocks"][0]["bbox_pdf_points"] = [0, 0, 100, 100]
        self.assertEqual(sentence_result(record, sentence(), settings())["position"]["status"], "proxy_fail")

    def test_precise_position_and_union_are_measured(self):
        record = page(["alpha beta"], precise=True)
        self.assertEqual(sentence_result(record, sentence(), settings())["position"]["status"], "correct")
        self.assertEqual(region_iou([[0, 0, 5, 10], [5, 0, 10, 10]], [[0, 0, 10, 10]]), 1)

    def test_unconfirmed_gold_and_resource_limits_remain_unscored(self):
        record = page(["alpha beta"]); gold = gold_for(record, [sentence()])
        gold["pages"][0]["review_status"] = "unresolved"
        report = evaluate(record, record, gold)
        self.assertIsNone(report["summary"]["sentences"]["before"]["confirmed_correct_rate"])
        limited = settings(); limited["sentence"]["maximum_page_characters"] = 1
        self.assertEqual(sentence_result(record, sentence(), limited)["status"], "unresolved")

    def test_gold_duplicate_ids_and_invalid_settings_are_rejected(self):
        record = page(["alpha beta"])
        with self.assertRaises(ValueError): validate_gold(gold_for(record, [sentence(), sentence()]))
        limited = settings(); limited["position"]["iou_threshold"] = float("nan")
        with self.assertRaises(ValueError): validate_settings(limited)

    def test_exact_text_beyond_join_limit_is_not_reported_as_typo(self):
        record = page(["alpha", "beta"])
        config = settings(); config["sentence"]["maximum_join_blocks"] = 1
        self.assertEqual(sentence_result(record, sentence(), config)["status"], "unresolved")


class TableTests(unittest.TestCase):
    def test_merged_cells_and_collision(self):
        html = '<table><tr><th colspan="2">Year</th></tr><tr><td>A</td><td>B</td></tr></table>'
        cells = LogicalTable(settings()["table"]["maximum_grid_slots"]).grid(html)
        self.assertEqual(cells[0]["colspan"], 2)
        broken = '<table><tr><td>A</td><td rowspan="2">B</td></tr><tr><td colspan="2">C</td></tr></table>'
        with self.assertRaises(ValueError): LogicalTable(100).grid(broken)
        with self.assertRaises(ValueError): LogicalTable(10).grid('<table><tr><td colspan="99999">A</td></tr></table>')

    def test_value_move_is_new_regression(self):
        before = page(['<table><tr><td>7</td><td>8</td></tr></table>'], table=True)
        after = page(['<table><tr><td>8</td><td>7</td></tr></table>'], table=True)
        table = {"id": "table", "bboxes": [[0, 0, 10, 10]], "cells": [
            {"id": "value", "row": 0, "column": 0, "rowspan": 1, "colspan": 1, "text": "7"}]}
        report = evaluate(before, after, gold_for(before, tables=[table]))
        self.assertEqual(report["tables"][0]["cell_transitions"][0]["transition"], "new_moved")
        self.assertEqual(report["summary"]["table_correspondence"]["after"]["confirmed_correct_rate"], 0)

    def test_header_damage_is_not_hidden_by_equal_number(self):
        html = '<table><tr><th>Item</th><th>Year</th></tr><tr><th>Row</th><td>7</td></tr></table>'
        before = page([html], table=True); after = page([html.replace("Year", "Month")], table=True)
        cell = {"id": "value", "row": 1, "column": 1, "rowspan": 1, "colspan": 1,
                "text": "7", "headers": ["Year", "Row"]}
        table = {"id": "table", "bboxes": [[0, 0, 10, 10]], "cells": [cell]}
        report = evaluate(before, after, gold_for(before, tables=[table]))
        self.assertEqual(report["summary"]["table_cells"]["after"]["confirmed_correct_rate"], 1)
        self.assertEqual(report["summary"]["table_correspondence"]["after"]["confirmed_correct_rate"], 0)

    def test_missing_table_is_counted_for_every_gold_cell(self):
        record = page([], table=True)
        cells = [{"id": f"cell-{i}", "row": 0, "column": i, "rowspan": 1, "colspan": 1, "text": str(i)}
                 for i in range(2)]
        gold = gold_for(record, tables=[{"id": "table", "bboxes": [[0, 0, 10, 10]], "cells": cells}])
        report = evaluate(record, record, gold)
        self.assertEqual(report["summary"]["table_cells"]["before"]["counts"], {"missing": len(cells)})

    def test_image_cell_is_not_scored_as_correct_blank(self):
        record = page(['<table><tr><td><img src="symbol.png"></td></tr></table>'], table=True)
        table = {"id": "table", "bboxes": [[0, 0, 10, 10]], "cells": [
            {"id": "icon", "row": 0, "column": 0, "rowspan": 1, "colspan": 1, "text": "", "kind": "image"}]}
        report = evaluate(record, record, gold_for(record, tables=[table]))
        self.assertEqual(report["tables"][0]["cell_transitions"][0]["transition"], "unresolved")


class ResourceTests(unittest.TestCase):
    def test_sampled_memory_scope_and_measurement_error(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = ResourceMonitor(Path(directory) / "samples.jsonl", TEST_INTERVAL_SECONDS, False, None)
            with patch("experiments.resources.psutil.Process", side_effect=RuntimeError("synthetic observer failure")):
                with monitor.measure("prediction"):
                    pass
            summary = monitor.close()
            self.assertGreaterEqual(summary["scopes"][0]["sample_count"], 2)
            self.assertGreaterEqual(summary["scopes"][0]["unavailable_samples"], 2)
            self.assertEqual(summary["scopes"][0]["status"], "completed")
            self.assertEqual(summary["peak_kind"], "sampled_maximum")

    def test_model_exception_still_closes_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = ResourceMonitor(Path(directory) / "samples.jsonl", TEST_INTERVAL_SECONDS, False, None)
            with self.assertRaises(RuntimeError):
                with monitor.measure("prediction"): raise RuntimeError("synthetic model failure")
            self.assertEqual(monitor.close()["scopes"][0]["status"], "failed")


class RecordingTests(unittest.TestCase):
    def test_source_diff_uses_snapshot_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source.py"
            source.write_text('value = "before"\n')
            old = root / "before"; new = root / "after"; old.mkdir(); new.mkdir()
            recorder = ExperimentRecorder(old, record_config(root), {"parser": "synthetic"})
            recorder.finish("completed")
            source.write_text('value = "after"\n')
            recorder = ExperimentRecorder(new, record_config(root), {"parser": "synthetic"})
            recorder.finish("completed")
            before, after = load_run(old), load_run(new)
            report = compare_runs(before, after)
            entry = report["source_comparison"]["files"][0]
            self.assertEqual(entry["kind"], "changed")
            self.assertIn('-value = "before"', entry["unified_diff"])
            self.assertIn('+value = "after"', entry["unified_diff"])
            saved = inside(old, before["manifest"]["sources"]["files"][source.name]["snapshot"])
            saved.write_text("tampered")
            self.assertEqual(compare_runs(before, after)["source_comparison"]["files"][0]["status"], "unavailable")

    def test_source_change_during_run_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source.py"; source.write_text("value = 1\n")
            output = root / "output"; output.mkdir()
            recorder = ExperimentRecorder(output, record_config(root), {})
            source.write_text("value = 2\n")
            final = recorder.finish("completed")
            self.assertEqual(final["source_changes_during_run"], [source.name])

    def test_model_hash_is_path_independent_and_revision_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dirs = [root / "one", root / "two"]
            binding = {"role": "encoder", "object_path": ["encoder"],
                       "directory_attribute": "model_dir", "name_attribute": "model_name", "revision": "requested"}
            manifests = []
            for folder in dirs:
                folder.mkdir(); (folder / "weights.bin").write_bytes(b"synthetic weights")
                parser = SimpleNamespace(encoder=SimpleNamespace(model_dir=folder, model_name="synthetic"))
                manifests.append(model_inventory(parser, [binding])["models"][0])
            self.assertEqual(manifests[0]["inventory_sha256"], manifests[1]["inventory_sha256"])
            self.assertIsNone(manifests[0]["verified_revision"])
            metadata = dirs[0] / HF_METADATA_DIRECTORY / "weights.bin.metadata"
            metadata.parent.mkdir(parents=True)
            commit = hashlib.sha1(b"synthetic commit").hexdigest()
            metadata.write_text(f"{commit}\n{digest(b'synthetic weights')}\n0\n")
            parser = SimpleNamespace(encoder=SimpleNamespace(model_dir=dirs[0], model_name="synthetic"))
            self.assertEqual(model_inventory(parser, [binding])["models"][0]["verified_revision"], commit)
            (dirs[0] / "weights.bin").write_bytes(b"changed weights")
            self.assertIsNone(model_inventory(parser, [binding])["models"][0]["verified_revision"])

    def test_config_path_is_relative_to_config_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config = record_config(root)
            config["project_root"] = "."
            path = root / "record.json"; write_json(path, config)
            self.assertEqual(load_record_config(path)["project_root"], str(root.resolve()))

    def test_downstream_stage_reuse_preserves_origin_and_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "source.py").write_text("value = 1\n")
            upstream = root / "input.txt"; upstream.write_text("synthetic input")
            output = root / "run"; output.mkdir()
            recorder = ExperimentRecorder(output, record_config(root), {}, stage="chunking")
            reference = recorder.reuse(upstream, origin_run_id="earlier-run", origin_stage_id="earlier-stage",
                origin_artifact_id="earlier-artifact", expected_sha256=file_hash(upstream))
            derived = output / "chunks.json"; write_json(derived, {"chunks": []})
            recorder.artifact(derived, "chunks", parents=[reference])
            final = recorder.finish("completed")
            self.assertEqual(final["input_artifacts"][0]["status"], "reused")
            self.assertEqual(final["artifacts"][0]["parents"], [reference])
            self.assertEqual(load_run(output)["manifest"]["stage"], "chunking")

    def test_start_manifest_failure_stops_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "source.py").write_text("value = 1\n")
            output = root / "run"; output.mkdir()
            monitors = []
            def make_monitor(*args, **kwargs):
                monitor = ResourceMonitor(*args, **kwargs); monitors.append(monitor)
                return monitor
            with patch("experiments.record.ResourceMonitor", side_effect=make_monitor), \
                 patch("experiments.record.write_json", side_effect=OSError("synthetic disk failure")):
                with self.assertRaises(OSError):
                    ExperimentRecorder(output, record_config(root), {})
            self.assertFalse(monitors[0].thread.is_alive())


class ComparisonTests(unittest.TestCase):
    def test_old_runs_are_unmeasured_and_not_modified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); record = page(["alpha beta"])
            old = legacy_run(root / "before", record); new = legacy_run(root / "after", record)
            original = {p: file_hash(p) for p in root.rglob("*.json")}
            output = root / "comparison"
            self.assertEqual(compare_main(["--baseline", str(old), "--candidate", str(new), "--output-dir", str(output)]), 0)
            report = read_json(output / "comparison.json")
            self.assertEqual(report["quality"]["status"], "unmeasured")
            self.assertEqual(report["source_comparison"]["status"], "unavailable")
            self.assertEqual({p: file_hash(p) for p in original}, original)
            self.assertEqual((output / "comparison.md").read_text(), markdown(report))
            with self.assertRaises(SystemExit):
                compare_main(["--baseline", str(old), "--candidate", str(new), "--output-dir", str(output)])

    def test_export_inside_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); record = page([])
            old = legacy_run(root / "before", record)
            with self.assertRaises(SystemExit):
                compare_main(["--baseline", str(old), "--candidate", str(old), "--output-dir", str(old / "report")])

    def test_unreadable_page_does_not_hide_missing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); record = page([])
            run = legacy_run(root / "run", record)
            target = next(run.rglob("page_*.json")); target.write_text("broken json")
            loaded = load_run(run)
            self.assertTrue(loaded["problems"])


class ParserIntegrationTests(unittest.TestCase):
    def test_recorded_options_match_predict_and_saved_artifacts(self):
        # PyMuPDF는 테스트 안에서 작은 PDF를 생성한다. 모델·HTTP는 대체한다.
        import pymupdf
        from parsing.run import main as parsing_main
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source.py"; source.write_text("value = 1\n")
            with pymupdf.open() as document:
                page_obj = document.new_page()
                page_obj.insert_text((20, 30), "synthetic evidence")
                pdf = document.tobytes()
            selection = root / "selection.json"
            write_json(selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
                {"document_name": "synthetic-document", "page_numbers": [1]}]})
            config = record_config(root)
            config["pipeline_options"] = {"use_chart_recognition": False}
            config["predict_options"] = {"merge_layout_blocks": False}
            config_file = root / "record.json"; write_json(config_file, config)
            raw = {"res": {"model_settings": {"merge_layout_blocks": False}, "parsing_res_list": [
                {"block_label": "text", "block_content": "synthetic evidence", "block_bbox": [0, 0, 10, 10]}]}}
            parser = SimpleNamespace(_merged_paddlex_config={"configured": True},
                                     predict=MagicMock(return_value=iter([SimpleNamespace(json=raw)])))
            output = root / "run"
            with patch("parsing.v1_parser.build_parser", return_value=parser) as build, \
                 patch("parsing.run.read_pdf_bytes", return_value=pdf):
                result = parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                    "--output-dir", str(output), "--experiment-config", str(config_file)])
            self.assertEqual(result, 0)
            self.assertEqual(build.call_args.kwargs["overrides"], config["pipeline_options"])
            self.assertFalse(parser.predict.call_args.kwargs["merge_layout_blocks"])
            metadata = read_json(output / "run.json")
            self.assertEqual(metadata["predict_options"]["merge_layout_blocks"], False)
            self.assertFalse(metadata["parser_options"]["use_chart_recognition"])
            final = read_json(output / FINISHED_FILE)
            self.assertEqual(final["status"], "completed")
            kinds = {item["kind"] for item in final["artifacts"]}
            self.assertEqual(kinds, {"source_pdf", "page_image", "parsing_page"})
            self.assertTrue(all(item["parents"] for item in final["artifacts"] if item["kind"] != "source_pdf"))
            scopes = {s["name"] for s in final["resources"]["scopes"]}
            self.assertTrue({"model_load", "model_prediction", "entire_run"} <= scopes)

    def test_model_load_failure_has_finished_record(self):
        from parsing.run import main as parsing_main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "source.py").write_text("value = 1\n")
            selection = root / "selection.json"
            write_json(selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
                {"document_name": "synthetic-document", "page_numbers": [1]}]})
            config_file = root / "record.json"; write_json(config_file, record_config(root))
            output = root / "run"
            with patch("parsing.v1_parser.build_parser", side_effect=RuntimeError("synthetic load failure")):
                result = parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                    "--output-dir", str(output), "--experiment-config", str(config_file)])
            self.assertEqual(result, 1)
            final = read_json(output / FINISHED_FILE)
            self.assertEqual(final["status"], "completed_with_errors")
            self.assertEqual(final["error"]["type"], "RuntimeError")

    def test_interruption_has_terminal_manifest_and_is_reraised(self):
        from parsing.run import main as parsing_main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / "source.py").write_text("value = 1\n")
            selection = root / "selection.json"
            write_json(selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
                {"document_name": "synthetic-document", "page_numbers": [1]}]})
            config_file = root / "record.json"; write_json(config_file, record_config(root))
            output = root / "run"
            with patch("parsing.v1_parser.build_parser", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                        "--output-dir", str(output), "--experiment-config", str(config_file)])
            self.assertEqual(read_json(output / FINISHED_FILE)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
