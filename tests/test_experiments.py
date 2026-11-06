"""실험 기록·전후 평가의 의미 있는 경계와 파싱 연결을 검증한다.

모든 PDF·모델·결과·정답은 임시 폴더 안의 모의 데이터로 만든다.
실제 결과 경로·해시·개수를 고정하지 않으며 모델 다운로드와 실제 추론은 하지 않는다.
복구·퇴행·보류, 소스 무결성·배타적 저장·기록 설정의 실제 전달을 확인한다.
"""

# 표준 라이브러리: copy는 독립 전후 입력, hashlib는 모의 원본 해시,
# datetime·timedelta·timezone은 현재 시각 기준 모의 실행 순서를 만든다.
# json은 설정, pathlib·tempfile은 임시 자료, types는 모델 없는 객체 그래프다.
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
# subprocess·sys는 현재 Python으로 다른 작업 폴더에서 main.py의 읽기 전용 확인을 실행한다.
import subprocess
import sys
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
from experiments.automatic import (AUTO_COMPARISON_FILE, DEFAULT_RECORDING_CONFIG,
                                  finish_comparison, prepare_comparison)

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


class AutomaticComparisonTests(unittest.TestCase):
    def fixture(self, root):
        """시각·문서·출력은 이번 테스트 안에서만 생성한 값이다."""
        selection = {"page_numbering": "pdf_viewer_1_based", "documents": [
            {"document_name": "synthetic-document", "page_numbers": [2, 1]}]}
        args = SimpleNamespace(output_dir=root / "next", compare_baseline=None, no_auto_compare=False,
            comparison_gold=None, comparison_evaluation_config=None, comparison_output_dir=None,
            parser="vl", all_pages=False)
        return args, selection

    def baseline(self, root, selection, *, parser="vl", age_seconds=0, completed=True):
        root.mkdir()
        write_json(root / "run.json", {"schema_version": "project.parsing.v1", "parser": parser,
            "started_at_utc": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()})
        write_json(root / "selection.json", selection)
        if completed:
            write_json(root / "summary.json", {"status": "completed"})
        return root

    def test_default_recording_and_explicit_opt_out(self):
        from parsing.run import arguments
        args = arguments(["--base-url", "https://example.invalid/"])
        self.assertEqual(args.experiment_config, DEFAULT_RECORDING_CONFIG)
        self.assertFalse(args.no_auto_compare)
        disabled = arguments(["--base-url", "https://example.invalid/", "--no-experiment-record", "--no-auto-compare"])
        self.assertIsNone(disabled.experiment_config)
        self.assertTrue(disabled.no_auto_compare)
        with self.assertRaises(SystemExit):
            arguments(["--base-url", "https://example.invalid/", "--no-auto-compare", "--compare-baseline", "."])

    def test_recent_compatible_finished_run_and_selection_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            older = self.baseline(root / "older", selected, age_seconds=30)
            recent_selection = copy.deepcopy(selected)
            recent_selection["documents"][0]["page_numbers"].reverse()
            recent = self.baseline(root / "recent", recent_selection, age_seconds=20)
            self.baseline(root / "unfinished", selected, age_seconds=10, completed=False)
            incomplete = self.baseline(root / "incomplete_manifest", selected)
            write_json(incomplete / MANIFEST_FILE, {"run_id": "synthetic-incomplete"})
            self.baseline(root / "other_parser", selected, parser="structure")
            other = copy.deepcopy(selected); other["documents"][0]["page_numbers"].append(3)
            self.baseline(root / "other_pages", other)
            plan = prepare_comparison(args, selected)
            self.assertEqual(Path(plan["baseline"]), recent.resolve())
            self.assertNotEqual(Path(plan["baseline"]), older.resolve())
            self.assertTrue(plan["ignored"])
            self.assertFalse(args.output_dir.exists())

    def test_tied_recent_runs_are_not_arbitrarily_chosen(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            first = self.baseline(root / "first", selected)
            second = self.baseline(root / "second", selected)
            (second / "run.json").write_bytes((first / "run.json").read_bytes())
            self.assertEqual(prepare_comparison(args, selected)["status"], "skipped")

    def test_explicit_other_parser_and_output_safety(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            args.compare_baseline = self.baseline(root / "structure", selected, parser="structure")
            plan = prepare_comparison(args, selected)
            self.assertEqual(plan["mode"], "explicit")
            args.comparison_output_dir = args.output_dir / "report"
            with self.assertRaises(ValueError):
                prepare_comparison(args, selected)
            args.comparison_output_dir = root / "exists"; args.comparison_output_dir.mkdir()
            with self.assertRaises(FileExistsError):
                prepare_comparison(args, selected)

    def test_changed_baseline_is_recorded_as_failed_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            previous = self.baseline(root / "previous", selected)
            plan = prepare_comparison(args, selected)
            (previous / "summary.json").write_text('{}')
            args.output_dir.mkdir()
            result = finish_comparison(plan, args.output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertIn("메타데이터", result["error"]["message"])
            self.assertFalse(Path(plan["output_dir"]).exists())
            self.assertEqual(read_json(args.output_dir / AUTO_COMPARISON_FILE)["status"], "failed")

    def test_interrupted_run_skips_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            self.baseline(root / "previous", selected)
            plan = prepare_comparison(args, selected)
            args.output_dir.mkdir()
            with patch("experiments.automatic.run_comparison") as compare:
                result = finish_comparison(plan, args.output_dir, interrupted=True)
            compare.assert_not_called()
            self.assertEqual(result["status"], "skipped")

    def test_all_pages_does_not_limit_report_to_selected_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            args.all_pages = True
            args.compare_baseline = self.baseline(root / "previous", selected)
            plan = prepare_comparison(args, selected)
            args.output_dir.mkdir()
            # 비교기를 대체하되 생성 파일 해시까지 실제로 연결되는지 확인한다.
            def compare(*unused, **kwargs):
                self.assertIsNone(kwargs["selection"])
                target = Path(kwargs["output_dir"]); target.mkdir()
                write_json(target / "comparison.json", {"summary": {}})
                (target / "comparison.md").write_text("모의 보고서")
                return {"summary": {}}
            with patch("experiments.automatic.run_comparison", side_effect=compare):
                self.assertEqual(finish_comparison(plan, args.output_dir)["status"], "completed")

    def test_invalid_gold_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            args.comparison_gold = root / "gold.json"; write_json(args.comparison_gold, {})
            with self.assertRaises(ValueError):
                prepare_comparison(args, selected)
            self.assertFalse(args.output_dir.exists())

    def test_report_failure_preserves_candidate_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            self.baseline(root / "previous", selected)
            plan = prepare_comparison(args, selected)
            args.output_dir.mkdir()
            write_json(args.output_dir / "summary.json", {"status": "completed"})
            original = (args.output_dir / "summary.json").read_bytes()
            with patch("experiments.automatic.run_comparison", side_effect=OSError("synthetic report failure")):
                result = finish_comparison(plan, args.output_dir)
            self.assertEqual(result["status"], "failed")
            self.assertEqual((args.output_dir / "summary.json").read_bytes(), original)

    def test_gold_and_settings_are_forwarded_to_shared_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); args, selected = self.fixture(root)
            self.baseline(root / "previous", selected)
            args.comparison_gold = root / "gold.json"
            write_json(args.comparison_gold, gold_for(page(["alpha beta"]), [sentence()]))
            args.comparison_evaluation_config = root / "evaluation.json"
            write_json(args.comparison_evaluation_config, settings())
            plan = prepare_comparison(args, selected)
            args.output_dir.mkdir()
            # 인자 전달만 검사한다. 실제 보고서 생성은 연속 파싱 통합 테스트에서 검증한다.
            with patch("experiments.automatic.run_comparison", side_effect=OSError("synthetic stop")) as compare:
                finish_comparison(plan, args.output_dir)
            self.assertEqual(Path(compare.call_args.kwargs["gold"]), args.comparison_gold.resolve())
            self.assertEqual(Path(compare.call_args.kwargs["evaluation_config"]), args.comparison_evaluation_config.resolve())


class MainEntryTests(unittest.TestCase):
    def configuration(self, root):
        """독립된 설정 폴더·입력·선정 목록을 만들고 상대 경로 기준을 검증한다."""
        configs = root / "configs"; configs.mkdir()
        (root / "inputs").mkdir()
        write_json(configs / "selected.json", {"page_numbering": "pdf_viewer_1_based", "documents": [
            {"document_name": "synthetic-document", "page_numbers": [1]}]})
        write_json(configs / "record.json", record_config(root))
        (root / "source.py").write_text("value = 1\n")
        config = {"schema_version": "project.parsing_run.v1", "selection": "selected.json",
            "input": {"kind": "local", "pdf_directory": "../inputs", "base_url": None},
            "output_root": "../runs", "parser": "vl", "device": "cpu", "cpu_threads": 1,
            "dpi": 72, "http_timeout_seconds": 1, "formula_model": None, "all_pages": False,
            "recording": {"enabled": True, "config": "record.json"},
            "comparison": {"enabled": True, "baseline": None, "output_directory": None,
                           "gold": None, "evaluation_config": None}}
        path = configs / "run.json"; write_json(path, config)
        return path, config

    def test_default_main_runs_local_parsing_and_automatic_comparison(self):
        import main as entry
        import pymupdf
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); config_path, config = self.configuration(root)
            original = root / "inputs" / "synthetic-document.pdf"
            with pymupdf.open() as document:
                document.new_page().insert_text((20, 30), "synthetic evidence")
                original.write_bytes(document.tobytes())
            original_hash = file_hash(original)
            raw = {"res": {"parsing_res_list": [{"block_label": "text", "block_content": "synthetic evidence",
                                                "block_bbox": [0, 0, 10, 10]}]}}
            parser = SimpleNamespace(_merged_paddlex_config={},
                predict=MagicMock(side_effect=lambda **kwargs: iter([SimpleNamespace(json=raw)])))
            with patch("main.DEFAULT_RUN_CONFIG", config_path), \
                 patch("parsing.vl_parser.build_parser", return_value=parser), \
                 patch("parsing.run.read_pdf_bytes") as remote:
                self.assertEqual(entry.main([]), 0)
                self.assertEqual(entry.main([]), 0)
            remote.assert_not_called()
            runs = sorted((root / "runs").glob("*/run.json"))
            self.assertEqual(len(runs), parser.predict.call_count)
            first, second = (read_json(file) for file in runs)
            self.assertEqual(first["source_kind"], "local")
            self.assertEqual(first["launch_config"]["config"], config)
            self.assertEqual(first["launch_config"]["sha256"], file_hash(config_path))
            self.assertEqual(second["pdf_directory"], str(original.parent.resolve()))
            auto = read_json(runs[-1].parent / AUTO_COMPARISON_FILE)
            self.assertEqual(auto["status"], "completed")
            report = read_json(Path(auto["report_files"]["comparison.json"]["path"]))
            self.assertTrue(all(item["same_pdf"] for item in report["input_comparison"]))
            self.assertEqual(file_hash(original), original_hash)
            self.assertEqual(Path(second["automatic_comparison"]["baseline"]), runs[0].parent.resolve())

    def test_check_creates_no_outputs_and_does_not_start_parser(self):
        import main as entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, _ = self.configuration(root)
            # --check는 PDF 내용 검증이 아니라 경로·설정 검증이다.
            (root / "inputs" / "synthetic-document.pdf").write_bytes(b"synthetic placeholder")
            before = {file.relative_to(root): file_hash(file) for file in root.rglob("*") if file.is_file()}
            with patch("main.run_parsing") as run, patch("main.DEFAULT_RUN_CONFIG", path):
                self.assertEqual(entry.main(["--check"]), 0)
            run.assert_not_called()
            after = {file.relative_to(root): file_hash(file) for file in root.rglob("*") if file.is_file()}
            self.assertEqual(before, after)
            self.assertFalse((root / "runs").exists())

    def test_entrypoint_resolves_paths_from_config_in_another_working_directory(self):
        import main as entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, _ = self.configuration(root)
            (root / "inputs" / "synthetic-document.pdf").write_bytes(b"synthetic placeholder")
            result = subprocess.run([sys.executable, "-B", str(Path(entry.__file__).resolve()),
                "--config", str(path), "--check"], cwd=root, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads(result.stdout)
            self.assertEqual(report["status"], "configuration_valid")
            self.assertEqual(Path(report["output_directory"]).parent, (root / "runs").resolve())
            self.assertFalse((root / "runs").exists())

    def test_missing_local_pdf_fails_before_model_and_output(self):
        import main as entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, _ = self.configuration(root)
            with patch("parsing.vl_parser.build_parser") as build:
                with self.assertRaises(SystemExit) as exc:
                    entry.main(["--config", str(path)])
            self.assertEqual(exc.exception.code, 2)
            build.assert_not_called()
            self.assertFalse((root / "runs").exists())

    def test_invalid_types_and_unknown_fields_are_rejected(self):
        from main import configured_arguments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, original = self.configuration(root)
            for key, value in (("cpu_threads", True), ("all_pages", "false"), ("unexpected", None)):
                with self.subTest(key=key):
                    config = {**original, key: value}
                    path.write_text(json.dumps(config))
                    with self.assertRaises(ValueError):
                        configured_arguments(path)

    def test_http_and_structure_settings_are_forwarded(self):
        from main import configured_arguments
        from parsing.run import arguments
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); path, config = self.configuration(root)
            config["input"] = {"kind": "http", "pdf_directory": None, "base_url": "https://example.invalid/pdfs"}
            config["parser"] = "structure"; config["formula_model"] = "synthetic-formula-model"
            path.write_text(json.dumps(config))
            invocation, context = configured_arguments(path)
            args = arguments(invocation)
            self.assertEqual(args.base_url, config["input"]["base_url"])
            self.assertIsNone(args.pdf_directory)
            self.assertEqual(args.formula_model, config["formula_model"])
            self.assertEqual(context["sha256"], file_hash(path))
            self.assertEqual(args.selection, (path.parent / config["selection"]).resolve())

    def test_local_path_rejects_escape_and_external_symlink(self):
        from parsing.pdf_source import local_pdf_path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "inputs"; source.mkdir()
            outside = root / "outside.pdf"; outside.write_bytes(b"synthetic PDF")
            with self.assertRaises(ValueError):
                local_pdf_path(source, "../outside")
            (source / outside.name).symlink_to(outside)
            with self.assertRaises(ValueError):
                local_pdf_path(source, outside.stem)


class ParserIntegrationTests(unittest.TestCase):
    def test_recording_and_comparison_can_both_be_disabled(self):
        from parsing.run import main as parsing_main
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selection = root / "selection.json"
            write_json(selection, {"page_numbering": "pdf_viewer_1_based", "documents": [
                {"document_name": "synthetic-document", "page_numbers": [1]}]})
            output = root / "run"
            with patch("parsing.vl_parser.build_parser", side_effect=RuntimeError("synthetic failure")), \
                 patch("experiments.record.ExperimentRecorder") as recorder:
                result = parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                    "--output-dir", str(output), "--no-experiment-record", "--no-auto-compare"])
            recorder.assert_not_called()
            self.assertEqual(result, 1)
            self.assertFalse((output / MANIFEST_FILE).exists())
            self.assertEqual(read_json(output / AUTO_COMPARISON_FILE)["status"], "disabled")

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
                                     predict=MagicMock(side_effect=lambda **kwargs: iter([SimpleNamespace(json=raw)])))
            output = root / "run"
            with patch("parsing.vl_parser.build_parser", return_value=parser) as build, \
                 patch("parsing.run.read_pdf_bytes", return_value=pdf), \
                 patch("experiments.automatic.DEFAULT_RECORDING_CONFIG", config_file):
                # 기록 설정 플래그 없이 기본 설정을 읽는지 확인한다.
                result = parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                    "--output-dir", str(output)])
                second_output = root / "next_run"
                second_result = parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                    "--output-dir", str(second_output)])
            self.assertEqual(result, 0)
            self.assertEqual(second_result, 0)
            self.assertEqual(read_json(output / AUTO_COMPARISON_FILE)["status"], "skipped")
            auto = read_json(second_output / AUTO_COMPARISON_FILE)
            self.assertEqual(auto["status"], "completed")
            self.assertEqual(Path(auto["baseline"]), output.resolve())
            report = read_json(Path(auto["report_files"]["comparison.json"]["path"]))
            self.assertEqual(report["summary"]["quality_status"], "unmeasured")
            self.assertEqual(report["source_comparison"]["status"], "compared")
            self.assertEqual(read_json(second_output / FINISHED_FILE)["status"], "completed")
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
            with patch("parsing.vl_parser.build_parser", side_effect=RuntimeError("synthetic load failure")):
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
            with patch("parsing.vl_parser.build_parser", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    parsing_main(["--base-url", "https://example.invalid/", "--selection", str(selection),
                        "--output-dir", str(output), "--experiment-config", str(config_file)])
            self.assertEqual(read_json(output / FINISHED_FILE)["status"], "interrupted")


if __name__ == "__main__":
    unittest.main()
