"""확인한 정답을 기준으로 파싱 전후의 문장·위치·표 셀 변화를 계산한다.

compare가 읽은 페이지 사전·정답·평가 설정을 입력받는 순수 모듈이다.
모델·네트워크·파일 저장을 사용하지 않는다. 문자열 매칭 규칙의 미검출과
모호한 대응을 구분하며 블록 bbox 점수를 정밀 문장 위치로 승격하지 않는다.
"""

# 표준 라이브러리: Counter는 판정 집계, difflib는 부분 문자열 유사도,
# HTMLParser는 표 구조·텍스트, math는 유한성, re·unicodedata는 보수적 정규화를 맡는다.
from collections import Counter
from difflib import SequenceMatcher
from html.parser import HTMLParser
import math
import re
import unicodedata

# 내부 공통 모듈: 정답의 스키마와 JSON 해시가 보고서 정의와 일치하게 한다.
from .common import GOLD_SCHEMA

# 실제 실험의 임계값이 아닌 평가 파일 형식·좌표 기준의 정의다.
EVALUATION_SCHEMA = "project.parsing_evaluation.v1"
PDF_COORDINATES = "pymupdf_unrotated_page_points"
UNRESOLVED = {"unresolved", "unscored", "source_mismatch"}
SUCCESS = {"correct"}


def validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("평가 설정은 JSON 객체여야 합니다.")
    if settings.get("schema_version") != EVALUATION_SCHEMA:
        raise ValueError("지원하지 않는 평가 설정 스키마입니다.")
    normalization = settings["normalization"]
    if normalization["unicode_form"] not in ("NFC", "NFD"):
        raise ValueError("의미 변환을 피하려고 NFC 또는 NFD만 허용합니다.")
    if any(type(normalization[k]) is not bool for k in ("collapse_whitespace", "casefold")):
        raise ValueError("정규화 옵션은 bool이어야 합니다.")
    for key in ("minimum_similarity",):
        value = settings["sentence"][key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{key}는 (0, 1] 범위여야 합니다.")
    for key in ("iou_threshold", "table_iou_threshold"):
        value = settings["position"][key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= 1:
            raise ValueError(f"{key}는 (0, 1] 범위여야 합니다.")
    for section, keys in (("sentence", ("maximum_join_blocks", "maximum_candidates",
                                       "maximum_page_characters", "maximum_alignment_characters")),
                          ("table", ("maximum_grid_slots", "maximum_tables"))):
        if any(type(settings[section][key]) is not int or settings[section][key] < 1 for key in keys):
            raise ValueError(f"{section}의 자원 한도는 양의 정수여야 합니다.")
    return settings


def normalize(text, settings):
    text = unicodedata.normalize(settings["unicode_form"], text)
    if settings["casefold"]:
        text = text.casefold()
    if settings["collapse_whitespace"]:
        text = re.sub(r"\s+", " ", text).strip()
    return text


def rectangle(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)):
        raise ValueError("bbox에는 유한한 숫자 네 개가 필요합니다.")
    if value[2] <= value[0] or value[3] <= value[1]:
        raise ValueError("bbox의 넓이는 양수여야 합니다.")
    return tuple(value)


def union_area(boxes):
    """여러 사각형의 실제 합집합 면적을 계산한다. 큰 외접 bbox로 대체하지 않는다."""
    boxes = [rectangle(b) for b in boxes]
    xs = sorted({x for b in boxes for x in (b[0], b[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        intervals = sorted((b[1], b[3]) for b in boxes if b[0] < right and b[2] > left)
        height = 0.0
        end = None
        for start, stop in intervals:
            height += stop - start if end is None else max(0, stop - max(start, end))
            end = stop if end is None else max(end, stop)
        area += (right - left) * height
    return area


def region_iou(predicted, expected):
    predicted = [rectangle(b) for b in predicted]
    expected = [rectangle(b) for b in expected]
    intersections = []
    for a in predicted:
        for b in expected:
            box = [max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])]
            if box[0] < box[2] and box[1] < box[3]:
                intersections.append(box)
    shared = union_area(intersections)
    total = union_area(predicted) + union_area(expected) - shared
    return shared / total if total else None


def _position(blocks, truth, settings):
    if not truth.get("bboxes"):
        return {"status": "unscored", "reason": "정답 위치 없음"}
    try:
        score = region_iou([b["bbox_pdf_points"] for b in blocks], truth["bboxes"])
    except (KeyError, ValueError):
        return {"status": "unresolved", "reason": "출력 PDF 좌표 없음 또는 불량 좌표"}
    precise = all(b.get("position_granularity") == "sentence" for b in blocks)
    passed = score is not None and score >= settings["position"]["iou_threshold"]
    return {"status": ("correct" if passed else "incorrect") if precise
            else ("proxy_pass" if passed else "proxy_fail"),
            "iou": score, "granularity": "sentence" if precise else "block_proxy"}


def sentence_result(record, truth, settings):
    """내용 매칭 후 위치를 평가한다. 정답 bbox로 내용 검색 범위를 제한하지 않는다."""
    blocks = (record or {}).get("parsing", {}).get("blocks", [])
    if record is None or (record or {}).get("parsing", {}).get("status") in ("failed", "skipped"):
        return {"status": "missing", "reason": "페이지 또는 파싱 결과 없음", "spans": []}
    norm = settings["normalization"]
    indexed = [(i, normalize(b["content"], norm)) for i, b in enumerate(blocks)]
    indexed = [(i, text) for i, text in indexed if text]
    texts = [text for _, text in indexed]
    page = " ".join(texts)
    limits = settings["sentence"]
    if len(page) > limits["maximum_page_characters"]:
        return {"status": "unresolved", "reason": "페이지 문자 자원 한도", "spans": []}
    offsets = []; cursor = 0
    for text in texts:
        offsets.append((cursor, cursor + len(text)))
        cursor += len(text) + 1
    alternatives = {normalize(v, norm) for v in [truth["text"], *truth.get("alternatives", [])]}
    if "" in alternatives:
        raise ValueError("정답 문장은 정규화 뒤에도 비어 있지 않아야 합니다.")
    matches = {}
    exceeded_join = False
    for target in alternatives:
        for match in re.finditer(re.escape(target), page):
            a, z = match.span()
            if (a and page[a - 1].isalnum() and target[0].isalnum()
                    or z < len(page) and page[z].isalnum() and target[-1].isalnum()):
                continue
            members = [i for i, (start, end) in enumerate(offsets) if end > a and start < z]
            if members and len(members) <= limits["maximum_join_blocks"]:
                matches[(a, z)] = members
            elif members:
                exceeded_join = True
            if len(matches) > limits["maximum_candidates"]:
                return {"status": "unresolved", "reason": "문장 후보 자원 한도", "spans": []}
    if exceeded_join:
        return {"status": "unresolved", "reason": "정확 문장의 블록 결합 자원 한도", "spans": []}
    if len(matches) > 1:
        return {"status": "unresolved", "reason": "반복 문장의 다중 대응", "spans": list(matches)}
    if matches:
        span, members = next(iter(matches.items()))
        members = [indexed[i][0] for i in members]
        return {"status": "correct", "method": "normalized_exact_substring",
                "spans": [list(span)], "block_indices": members,
                "element_ids": [blocks[i].get("element_id") for i in members],
                "position": _position([blocks[i] for i in members], truth, settings)}
    # 부분 대응은 최대 블록 결합·후보·문자 한도로 비용을 제한한다.
    candidates = []
    generated = 0
    for start in range(len(texts)):
        for end in range(start + 1, min(len(texts), start + limits["maximum_join_blocks"]) + 1):
            generated += 1
            if generated > limits["maximum_candidates"]:
                return {"status": "unresolved", "reason": "부분 대응 후보 자원 한도", "spans": []}
            text = " ".join(texts[start:end])
            possible = [t for t in alternatives if 2 * min(len(t), len(text)) / (len(t) + len(text))
                        >= limits["minimum_similarity"]]
            if not possible:
                continue
            if max([len(text), *(len(t) for t in possible)]) > limits["maximum_alignment_characters"]:
                return {"status": "unresolved", "reason": "편집 정렬 문자 자원 한도", "spans": []}
            ratio = max(SequenceMatcher(None, t, text, autojunk=False).ratio() for t in possible)
            if ratio >= limits["minimum_similarity"]:
                candidates.append((ratio, start, end))
    if candidates:
        best = max(c[0] for c in candidates)
        winners = [c for c in candidates if c[0] == best]
        if len(winners) != 1:
            return {"status": "unresolved", "reason": "부분 대응 동점", "spans": []}
        _, start, end = winners[0]
        return {"status": "incorrect", "method": "block_similarity", "similarity": best,
                "block_indices": [indexed[i][0] for i in range(start, end)], "spans": []}
    return {"status": "missing", "reason": "설정한 매칭 규칙에서 미검출", "spans": []}


class LogicalTable(HTMLParser):
    """HTML 셀 내용·병합 범위를 읽는다. 구조 상한을 넘기거나 중첩 표이면 보류한다."""

    def __init__(self, maximum_slots):
        super().__init__(convert_charrefs=True)
        self.maximum_slots = maximum_slots
        self.rows = []
        self.row = None
        self.cell = None
        self.open_table = False
        self.table_count = 0
        self.requested_slots = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.open_table or self.table_count:
                raise ValueError("한 블록의 중첩·여러 표는 지원하지 않습니다.")
            self.open_table = True; self.table_count += 1
        elif tag == "tr":
            if not self.open_table or self.row is not None:
                raise ValueError("표 행 구조 오류")
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None or self.cell is not None:
                raise ValueError("표 셀 구조 오류")
            attrs = dict(attrs)
            rs, cs = int(attrs.get("rowspan", 1)), int(attrs.get("colspan", 1))
            if min(rs, cs) < 1 or rs * cs > self.maximum_slots:
                raise ValueError("병합 범위 자원 한도 또는 불량 범위")
            self.requested_slots += rs * cs
            if self.requested_slots > self.maximum_slots:
                raise ValueError("표 셀 자원 한도")
            self.cell = {"rowspan": rs, "colspan": cs, "tag": tag, "parts": []}
        elif tag == "br" and self.cell is not None:
            self.cell["parts"].append(" ")
        elif tag == "img" and self.cell is not None:
            self.cell["contains_image"] = True

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["parts"].append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.cell is None or self.cell["tag"] != tag:
                raise ValueError("표 셀 종료 오류")
            self.cell["text"] = "".join(self.cell.pop("parts"))
            self.row.append(self.cell); self.cell = None
        elif tag == "tr":
            if self.row is None or self.cell is not None:
                raise ValueError("표 행 종료 오류")
            self.rows.append(self.row); self.row = None
        elif tag == "table":
            if not self.open_table or self.row is not None:
                raise ValueError("표 종료 오류")
            self.open_table = False

    def grid(self, html):
        self.feed(html); self.close()
        if not self.table_count or self.open_table or self.cell is not None or self.row is not None:
            raise ValueError("완료되지 않은 표")
        occupied = {}; cells = []
        for row_index, row in enumerate(self.rows):
            col = 0
            for cell in row:
                while (row_index, col) in occupied:
                    col += 1
                if row_index + cell["rowspan"] > len(self.rows):
                    raise ValueError("실제 행 범위를 넘는 병합셀")
                if len(occupied) + cell["rowspan"] * cell["colspan"] > self.maximum_slots:
                    raise ValueError("표 격자 자원 한도")
                cell = {**cell, "row": row_index, "column": col}
                for r in range(row_index, row_index + cell["rowspan"]):
                    for c in range(col, col + cell["colspan"]):
                        if (r, c) in occupied:
                            raise ValueError("겹치는 병합셀")
                        occupied[r, c] = cell
                cells.append(cell); col += cell["colspan"]
        return cells


def table_result(record, truth, settings):
    if record is None or record.get("parsing", {}).get("status") in ("failed", "skipped"):
        return {"status": "missing", "reason": "페이지 또는 파싱 결과 없음", "cells": []}
    tables = [b for b in (record or {}).get("parsing", {}).get("blocks", []) if b.get("label") == "table"]
    if len(tables) > settings["table"]["maximum_tables"]:
        return {"status": "unresolved", "reason": "표 후보 자원 한도", "cells": []}
    candidates = []
    unknown = False
    for index, table in enumerate(tables):
        try:
            score = region_iou([table["bbox_pdf_points"]], truth["bboxes"])
        except (KeyError, ValueError):
            unknown = True
            continue
        if score >= settings["position"]["table_iou_threshold"]:
            candidates.append((score, index))
    if not candidates:
        return {"status": "unresolved" if unknown else "missing", "cells": [],
                "reason": "표 좌표 미확인" if unknown else "매칭 기준의 표 없음"}
    top = max(score for score, _ in candidates)
    winners = [i for score, i in candidates if score == top]
    if len(winners) != 1:
        return {"status": "unresolved", "reason": "표 대응 동점", "cells": []}
    index = winners[0]
    try:
        cells = LogicalTable(settings["table"]["maximum_grid_slots"]).grid(tables[index]["content"])
    except (ValueError, TypeError) as exc:
        return {"status": "unresolved", "reason": str(exc), "block_index": index, "cells": []}
    norm = settings["normalization"]
    result = []
    for gold in truth["cells"]:
        anchored = [c for c in cells if (c["row"], c["column"]) == (gold["row"], gold["column"])]
        target = normalize(gold["text"], norm)
        elsewhere = [c for c in cells if normalize(c["text"], norm) == target]
        entry = {"id": gold["id"], "row": gold["row"], "column": gold["column"]}
        if gold.get("kind", "text") != "text" or any(c.get("contains_image") for c in anchored):
            result.append({**entry, "status": "unresolved", "reason": "이미지 셀은 텍스트만으로 평가할 수 없음",
                           "correspondence_correct": False})
            continue
        if not anchored:
            entry["status"] = "moved" if len(elsewhere) == 1 else "missing"
        else:
            cell = anchored[0]
            entry.update(predicted_text=cell["text"],
                         span_correct=all(cell[k] == gold[k] for k in ("rowspan", "colspan")))
            if normalize(cell["text"], norm) == target:
                entry["status"] = "correct"
            else:
                entry["status"] = "moved" if len(elsewhere) == 1 else "incorrect"
            if "headers" in gold:
                # th로 명시된 위쪽 열 헤더와 왼쪽 행 헤더만 근거로 삼는다.
                headers = [c["text"] for c in cells if c["tag"] == "th" and (
                    c["row"] < cell["row"] and c["column"] <= cell["column"] < c["column"] + c["colspan"]
                    or c["row"] <= cell["row"] < c["row"] + c["rowspan"] and c["column"] < cell["column"])]
                entry["header_status"] = ("correct" if [normalize(v, norm) for v in headers]
                                          == [normalize(v, norm) for v in gold["headers"]] else "incorrect")
                entry["predicted_headers"] = headers
        if elsewhere:
            entry["matching_value_positions"] = [[c["row"], c["column"]] for c in elsewhere]
        entry["correspondence_correct"] = (entry["status"] == "correct"
            and entry.get("span_correct", False)
            and entry.get("header_status", "correct") == "correct")
        result.append(entry)
    return {"status": "matched", "block_index": index, "iou": top, "cells": result,
            "predicted_cell_count": len(cells), "header_method": "explicit_html_th_only"}


def transition(before, after):
    if before in UNRESOLVED or after in UNRESOLVED:
        return "unresolved"
    if before != "correct" and after == "correct":
        return "recovered"
    if before == "correct" and after == "missing":
        return "new_missing"
    if before == "correct" and after != "correct":
        return "new_moved" if after == "moved" else "new_incorrect"
    return "preserved" if before == after == "correct" else "still_failed"


def validate_gold(gold):
    if not isinstance(gold, dict):
        raise ValueError("정답은 JSON 객체여야 합니다.")
    if gold.get("schema_version") != GOLD_SCHEMA or not isinstance(gold.get("pages"), list):
        raise ValueError("정답 스키마와 pages 목록을 확인하세요.")
    if not gold.get("version") or gold.get("split") not in ("development", "test"):
        raise ValueError("정답 버전과 development/test 분할을 명시해야 합니다.")
    seen_pages = set(); seen_ids = set()
    for page in gold["pages"]:
        key = (page["document_id"], page["page_number"])
        if not isinstance(key[0], str) or not key[0] or type(key[1]) is not int or key[1] < 1 or key in seen_pages:
            raise ValueError("정답 페이지 ID·순번·중복을 확인하세요.")
        seen_pages.add(key)
        if page["coordinate_system"] != PDF_COORDINATES or not re.fullmatch(r"[0-9a-f]{64}", page["source_sha256"]):
            raise ValueError("정답의 PDF 해시와 좌표계가 필요합니다.")
        if page.get("review_status") not in ("confirmed", "unresolved"):
            raise ValueError("원문 대조 상태가 필요합니다.")
        for category in ("sentences", "tables"):
            for item in page.get(category, []):
                if not isinstance(item["id"], str) or not item["id"] or item["id"] in seen_ids:
                    raise ValueError("정답 항목 ID는 전체 정답에서 유일해야 합니다.")
                seen_ids.add(item["id"])
                for box in item.get("bboxes", []): rectangle(box)
                if category == "sentences":
                    if not isinstance(item["text"], str) or not item["text"].strip():
                        raise ValueError("정답 문장이 필요합니다.")
                    if (not isinstance(item.get("alternatives", []), list)
                            or any(not isinstance(t, str) or not t.strip() for t in item.get("alternatives", []))):
                        raise ValueError("허용 표기 목록은 비어 있지 않은 문자열이어야 합니다.")
                else:
                    if not item.get("bboxes") or not isinstance(item.get("cells"), list):
                        raise ValueError("정답 표의 위치와 셀 목록이 필요합니다.")
                    positions = set()
                    for cell in item["cells"]:
                        if cell["id"] in seen_ids: raise ValueError("중복 정답 셀 ID")
                        seen_ids.add(cell["id"])
                        position = (cell["row"], cell["column"])
                        if (any(type(v) is not int or v < 0 for v in position) or position in positions
                                or any(type(cell[k]) is not int or cell[k] < 1 for k in ("rowspan", "colspan"))
                                or not isinstance(cell["text"], str)):
                            raise ValueError("정답 셀 위치·병합 범위·문자열 오류")
                        positions.add(position)
    return gold


def evaluate_pair(before_pages, after_pages, gold, settings):
    validate_settings(settings); validate_gold(gold)
    sentence_items = []; table_items = []; withheld_pages = 0
    for page in gold["pages"]:
        key = (page["document_id"], page["page_number"])
        if page["review_status"] != "confirmed":
            withheld_pages += 1
            continue
        records = [before_pages.get(key), after_pages.get(key)]
        results = []
        for record in records:
            if record and (record.get("_load_error") or record.get("_integrity_error")):
                results.append({"sentences": [{"status": "unresolved", "spans": [],
                    "reason": record.get("_load_error") or record.get("_integrity_error")}
                    for _ in page.get("sentences", [])],
                    "tables": [{"status": "unresolved", "cells": []} for _ in page.get("tables", [])]})
                continue
            if record is not None and record.get("source_sha256") != page["source_sha256"]:
                results.append({"sentences": [{"status": "source_mismatch", "spans": []}
                                             for _ in page.get("sentences", [])],
                                "tables": [{"status": "source_mismatch", "cells": []}
                                           for _ in page.get("tables", [])]})
                continue
            sentences = [sentence_result(record, item, settings) for item in page.get("sentences", [])]
            # 서로 다른 정답이 같은 출력 문자 구간을 차지하면 두 판정 모두 보류한다.
            for i, a in enumerate(sentences):
                for b in sentences[i + 1:]:
                    if any(max(x[0], y[0]) < min(x[1], y[1]) for x in a.get("spans", []) for y in b.get("spans", [])):
                        a["status"] = b["status"] = "unresolved"
                        a["reason"] = b["reason"] = "정답 간 출력 구간 중복"
            tables = [table_result(record, item, settings) for item in page.get("tables", [])]
            used = Counter(t.get("block_index") for t in tables if "block_index" in t)
            for table in tables:
                if "block_index" in table and used[table["block_index"]] > 1:
                    table.update(status="unresolved", cells=[], reason="정답 표 간 출력 중복")
            results.append({"sentences": sentences, "tables": tables})
        for category, destination in (("sentences", sentence_items), ("tables", table_items)):
            for item, a, b in zip(page.get(category, []), results[0][category], results[1][category]):
                old_status = "correct" if category == "tables" and a["status"] == "matched" else a["status"]
                new_status = "correct" if category == "tables" and b["status"] == "matched" else b["status"]
                entry = {"id": item["id"], "document_id": key[0], "page_number": key[1],
                         "before": a, "after": b, "transition": transition(old_status, new_status)}
                if category == "tables":
                    ac = {c["id"]: c for c in a["cells"]}; bc = {c["id"]: c for c in b["cells"]}
                    entry["cell_transitions"] = []
                    for cell in item["cells"]:
                        old = ac.get(cell["id"], {"status": "missing" if a["status"] == "missing" else "unresolved"})
                        new = bc.get(cell["id"], {"status": "missing" if b["status"] == "missing" else "unresolved"})
                        entry["cell_transitions"].append({"id": cell["id"], "before": old, "after": new,
                            "transition": transition(old["status"], new["status"]),
                            "correspondence_transition": transition(
                                old["status"] if old["status"] in UNRESOLVED else
                                    ("correct" if old.get("correspondence_correct") else "incorrect"),
                                new["status"] if new["status"] in UNRESOLVED else
                                    ("correct" if new.get("correspondence_correct") else "incorrect"))})
                else:
                    old_position = a.get("position", {}).get("status", "unscored")
                    new_position = b.get("position", {}).get("status", "unscored")
                    if old_position.startswith("proxy_") and new_position.startswith("proxy_"):
                        entry["position_transition"] = "proxy_" + transition(
                            "correct" if old_position == "proxy_pass" else "incorrect",
                            "correct" if new_position == "proxy_pass" else "incorrect")
                    else:
                        entry["position_transition"] = transition(old_position, new_position)
                destination.append(entry)
    cells = [c for t in table_items for c in t["cell_transitions"]]
    summaries = {}
    for name, items in (("sentences", sentence_items), ("table_cells", cells)):
        total = len(items)
        counts = {side: Counter(i[side]["status"] for i in items) for side in ("before", "after")}
        summaries[name] = {"denominator": total, "transition_counts": dict(Counter(i["transition"] for i in items)),
            **{side: {"counts": dict(counts[side]), "confirmed_correct_rate": counts[side]["correct"] / total if total else None}
               for side in counts}}
    summaries["position"] = {side: dict(Counter(i[side].get("position", {}).get("status", "unscored")
                                                for i in sentence_items)) for side in ("before", "after")}
    summaries["position"]["denominator"] = len(sentence_items)
    summaries["position"]["transitions"] = dict(Counter(i["position_transition"] for i in sentence_items))
    summaries["table_correspondence"] = {side: {
        "confirmed_correct_count": sum(c[side].get("correspondence_correct", False) for c in cells),
        "confirmed_correct_rate": sum(c[side].get("correspondence_correct", False) for c in cells) / len(cells) if cells else None,
        "header_counts": dict(Counter(c[side].get("header_status", "unscored") for c in cells)),
        "span_counts": dict(Counter(str(c[side].get("span_correct", "unscored")) for c in cells))}
        for side in ("before", "after")}
    summaries["table_correspondence"]["denominator"] = len(cells)
    summaries["table_correspondence"]["transition_counts"] = dict(Counter(c["correspondence_transition"] for c in cells))
    summaries["tables"] = {"denominator": len(table_items),
        "transition_counts": dict(Counter(t["transition"] for t in table_items)),
        **{side: dict(Counter(t[side]["status"] for t in table_items)) for side in ("before", "after")}}
    return {"status": "measured" if sentence_items or table_items else "unmeasured",
            "gold_version": gold["version"], "split": gold["split"], "withheld_page_count": withheld_pages,
            "matching_is_policy_based": True, "summary": summaries, "sentences": sentence_items, "tables": table_items}
