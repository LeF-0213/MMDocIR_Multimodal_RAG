"""임베딩 정규화·포인트 ID·질의 파일·문서 필터·페이지 집계·RRF·검색 지표·실패 단계의 경계 조건을 검증한다.

모델·네트워크·Qdrant 없이 순수 함수만 검사한다. 입력은 테스트 안에서 만든 작은 예시이고 실제 결과와 무관하다.
"""

# 표준 라이브러리: json·tempfile·Path는 임시 질의 파일, math는 nDCG 기대값 계산, unittest는 테스트 실행이다.
import json
import math
from pathlib import Path
import tempfile
import unittest

# NumPy: 정규화 입력 배열.
import numpy as np

# 내부 모듈: 임베딩 정규화, 인덱싱 ID 규칙, 검색 집계·결합, 평가 지표.
from embedding.encoders import normalize_rows
from evaluation.retrieval import failure_stage, mrr_at, ndcg_at, recall_at
from indexing.store import point_id, record_id
from retrieval.fusion import pages_by_max, reciprocal_rank_fusion
from retrieval.run import DOCUMENT_FIELD, document_filter, load_queries


class EmbeddingAndIndexTest(unittest.TestCase):
    def test_normalize_rows_unit_length_and_rejects_zero(self):
        vectors, norms = normalize_rows([[3.0, 4.0], [0.0, 2.0]])
        self.assertTrue(np.allclose(np.linalg.norm(vectors, axis=1), 1.0))
        self.assertEqual(norms.tolist(), [5.0, 2.0])
        with self.assertRaises(ValueError):
            normalize_rows([[0.0, 0.0]])

    def test_point_id_is_deterministic_and_distinct(self):
        self.assertEqual(point_id("doc:p1:vl:chunk:0"), point_id("doc:p1:vl:chunk:0"))
        self.assertNotEqual(point_id("doc:p1:vl:chunk:0"), point_id("doc:p1:vl:chunk:1"))

    def test_record_id_prefers_chunk_then_page(self):
        self.assertEqual(record_id({"chunk_id": "c", "page_id": "p"}), "c")
        self.assertEqual(record_id({"page_id": "p"}), "p")


class QueryScopeTest(unittest.TestCase):
    def write_queries(self, rows):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "queries.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def test_load_queries_requires_known_document_and_no_label_fields(self):
        query = {"query_id": "q1", "document_name": "doc-a", "question": "?"}
        self.assertEqual(load_queries(self.write_queries([query]), {"doc-a"}), [query])
        with self.assertRaises(ValueError):
            load_queries(self.write_queries([query]), {"doc-b"})
        with self.assertRaises(ValueError):
            load_queries(self.write_queries([query | {"page_id": "p"}]), {"doc-a"})

    def test_document_filter_matches_exact_document(self):
        condition = document_filter("doc-a").must[0]
        self.assertEqual((condition.key, condition.match.value), (DOCUMENT_FIELD, "doc-a"))


class FusionTest(unittest.TestCase):
    def test_pages_by_max_keeps_best_chunk_per_page(self):
        pages = pages_by_max([("p1", 0.2, "a"), ("p2", 0.5, "b"), ("p1", 0.7, "c")])
        self.assertEqual([(p["page_id"], p["best_record_id"]) for p in pages], [("p1", "c"), ("p2", "b")])

    def test_rrf_rewards_agreement_and_counts_single_channel(self):
        fused = dict(reciprocal_rank_fusion([["a", "b"], ["b", "c"]], k=60))
        self.assertAlmostEqual(fused["b"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(fused["c"], 1 / 62)
        self.assertGreater(fused["b"], fused["a"])


class MetricTest(unittest.TestCase):
    def test_metrics_single_relevant(self):
        ranking = ["x", "r", "y"]
        self.assertEqual(recall_at(ranking, {"r"}, 1), 0.0)
        self.assertEqual(recall_at(ranking, {"r"}, 2), 1.0)
        self.assertEqual(mrr_at(ranking, {"r"}, 3), 0.5)
        self.assertAlmostEqual(ndcg_at(ranking, {"r"}, 3), 1 / math.log2(3))

    def test_ndcg_ideal_is_capped_by_k(self):
        # 관련 3개 중 k=2 안에 2개가 모두 들어오면 1이다(이상 순위도 k개까지만 센다).
        self.assertAlmostEqual(ndcg_at(["a", "b", "c"], {"a", "b", "c"}, 2), 1.0)

    def test_failure_stage(self):
        row = {"fused": [{"page_id": p} for p in ["a", "b", "c", "d"]],
               "reranked": [{"page_id": p} for p in ["b", "a", "c", "d"]]}
        self.assertEqual(failure_stage(row, {"z"}, 2, 1)[0], "not_in_candidates")
        self.assertEqual(failure_stage(row, {"b"}, 2, 1)[0], "hit")
        self.assertEqual(failure_stage(row, {"a"}, 2, 1)[0], "ranked_below_final")
        self.assertEqual(failure_stage(row, {"d"}, 2, 1)[0], "outside_rerank")


if __name__ == "__main__":
    unittest.main()
