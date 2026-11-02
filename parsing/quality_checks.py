"""
파싱 결과의 한 페이지를 검사하는 순수 검사 모듈이다.
입력은 저장된 페이지 JSON이며, 출력은 측정값과 검토 항목이다.
원본을 수정하거나 모델을 호출하지 않는다. 파일 조회·결과 저장은 validate_run.py가 맡는다.
검사 통과는 원문 내용의 정확성 보장이 아니며, 반복 임계값은 개발용 초기값이다.
"""

# 표준 라이브러리: math는 좌표의 유한성을, re는 출력의 단어를 검사한다.
# Counter는 반복 횟수를 집계하고 HTMLParser는 표 태그와 병합 범위를 읽는다.
import math
import re
from collections import Counter
from html.parser import HTMLParser

RULE_VERSION = "0.1.0"
# 반복 임계값은 개발용 초기값이며 환각 확정 기준이 아니다.
# 좌표 허용 오차는 부동소수점 계산 비교용이고 문장 위치의 허용 오차가 아니다.
LIMITS = {
    "minimum_tokens": 40,
    "dominant_token_ratio": 0.5,
    "maximum_characters": 10000,
    "repeated_trigram_ratio": 0.7,
    "long_sequence_length": 50,
    "maximum_grid_slots": 20000,
    "coordinate_tolerance": 0.00001,
}


class TableGrid(HTMLParser):
    """단일 표의 행·셀·병합 범위를 읽는다. 일반 HTML의 완전한 유효성 검사기는 아니다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.table_count = 0
        self.table_open = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.table_count += 1
            if self.table_count > 1:
                raise ValueError("한 블록의 중첩 표 또는 여러 표는 별도 처리가 필요합니다.")
            self.table_open = True
        elif tag == "tr":
            if not self.table_open or self.row is not None:
                raise ValueError("표 바깥의 행이거나 이전 행이 닫히지 않았습니다.")
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None or self.cell is not None:
                raise ValueError("셀의 시작 위치가 올바르지 않습니다.")
            rowspan = int(attrs.get("rowspan", "1"))
            colspan = int(attrs.get("colspan", "1"))
            if min(rowspan, colspan) < 1:
                raise ValueError("현재 검사기는 양수 병합 범위만 지원합니다.")
            self.cell = (rowspan, colspan)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.cell is None or self.row is None:
                raise ValueError("열린 셀이 없습니다.")
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr":
            if self.row is None or self.cell is not None:
                raise ValueError("행을 닫기 전에 셀을 확인해야 합니다.")
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            if not self.table_open or self.row is not None or self.cell is not None:
                raise ValueError("열린 행·셀 또는 표 종료 위치를 확인해야 합니다.")
            self.table_open = False

    def measure(self, html):
        """명시적인 행·셀 종료와 양수 병합 범위를 가진 단일 표를 검사한다."""
        self.feed(html)
        self.close()
        if (self.table_count != 1 or self.table_open
                or self.row is not None or self.cell is not None):
            raise ValueError("표·행·셀 구성을 확인할 수 없습니다.")
        if not self.rows:
            raise ValueError("표에 행이 없습니다.")

        # occupied는 rowspan으로 앞 행에서 차지한 자리도 포함한다.
        # 단순 td 개수 비교로 정상적인 병합 표를 오류 처리하는 일을 피한다.
        occupied = set()
        for r, row in enumerate(self.rows):
            c = 0
            for rowspan, colspan in row:
                while (r, c) in occupied:
                    c += 1
                if r + rowspan > len(self.rows):
                    raise ValueError("rowspan이 실제 행 수를 넘습니다.")
                if len(occupied) + rowspan * colspan > LIMITS["maximum_grid_slots"]:
                    raise ValueError("검사 자원 한도를 넘는 표입니다.")
                slots = {
                    (rr, cc)
                    for rr in range(r, r + rowspan)
                    for cc in range(c, c + colspan)
                }
                if occupied & slots:
                    raise ValueError("병합 셀이 서로 겹칩니다.")
                occupied.update(slots)
                c += colspan
        if not occupied:
            raise ValueError("표에 셀이 없습니다.")
        width = 1 + max(c for _, c in occupied)
        holes = len(self.rows) * width - len(occupied)
        return {"rows": len(self.rows), "columns": width, "unfilled_slots": holes}


def box(value):
    """bool·NaN·역전·면적 0을 허용하지 않는 사각형 검사다."""
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError("bbox에는 숫자 네 개가 필요합니다.")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in value
    ):
        raise ValueError("bbox에는 유한한 숫자가 필요합니다.")
    if value[2] <= value[0] or value[3] <= value[1]:
        raise ValueError("bbox가 역전됐거나 면적이 0입니다.")
    return value


def inspect_page(record):
    """확정 가능한 형식·계산 오류와 원본 검토가 필요한 신호를 구분한다."""
    findings = []
    tables = []

    def add(rule, status, element_id, evidence):
        findings.append({
            "rule": rule,
            "status": status,
            "element_id": element_id,
            "evidence": evidence,
        })

    if not isinstance(record, dict):
        raise ValueError("페이지 JSON은 객체여야 합니다.")
    parsing = record["parsing"]
    if not isinstance(parsing, dict):
        raise ValueError("parsing은 객체여야 합니다.")
    blocks = parsing["blocks"]
    if not isinstance(blocks, list):
        raise ValueError("parsing.blocks는 목록이어야 합니다.")
    if parsing.get("status") in ("failed", "skipped"):
        add("processing_status", "unresolved", None,
            {"page_status": record.get("status"),
             "parser_status": parsing.get("status")})
    render = record["render"]
    matrix = render["pixel_to_pdf_matrix"]
    if (
        not isinstance(matrix, list) or len(matrix) != 6
        or any(isinstance(v, bool) or not isinstance(v, (int, float))
               or not math.isfinite(v) for v in matrix)
    ):
        raise ValueError("유한한 숫자 여섯 개의 좌표 행렬이 필요합니다.")
    a, b, c, d, e, f = matrix
    width, height = render["width"], render["height"]
    if (type(width) is not int or type(height) is not int
            or width <= 0 or height <= 0):
        raise ValueError("양수 이미지 크기가 필요합니다.")

    # raw의 순번을 공통 블록의 source_list_index와 연결한다.
    # 원본 라벨·내용·bbox를 바꾸지 않고 병합 정보만 검사 근거로 읽는다.
    raw = parsing.get("raw") or {}
    if not isinstance(raw, dict):
        raise ValueError("raw는 객체 또는 null이어야 합니다.")
    raw = raw.get("res", raw)
    if not isinstance(raw, dict):
        raise ValueError("raw.res는 객체여야 합니다.")
    raw_blocks = raw.get("parsing_res_list", [])
    if not isinstance(raw_blocks, list):
        raise ValueError("raw parsing_res_list는 목록이어야 합니다.")
    groups = {}
    seen = set()

    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("블록은 객체여야 합니다.")
        eid = block["element_id"]
        if not isinstance(eid, str) or not eid:
            raise ValueError("비어 있지 않은 요소 ID가 필요합니다.")
        if eid in seen:
            add("duplicate_id", "confirmed_error", eid, {"value": eid})
        seen.add(eid)
        content = block["content"]
        if not isinstance(content, str):
            raise ValueError("블록 내용은 문자열이어야 합니다.")
        index = block["source_list_index"]
        if type(index) is not int or not 0 <= index < len(raw_blocks):
            add("raw_mapping", "unresolved", eid, {"index": index})
        else:
            original = raw_blocks[index]
            if (content != original.get("block_content")
                    or block.get("bbox_image_pixels") != original.get("block_bbox")):
                add("raw_mapping", "confirmed_error", eid, {"index": index})
            gid = original.get("group_id")
            if gid is not None:
                groups.setdefault(str(gid), []).append((eid, bool(content.strip())))

        try:
            x0, y0, x1, y1 = box(block["bbox_image_pixels"])
            # x'=a*x+c*y+e, y'=b*x+d*y+f인 아핀 변환이다.
            # 회전된 페이지도 포함하므로 네 꼭짓점으로 PDF 사각형을 다시 구한다.
            points = [
                (a*x + c*y + e, b*x + d*y + f)
                for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
            ]
            expected = [
                min(x for x, _ in points), min(y for _, y in points),
                max(x for x, _ in points), max(y for _, y in points),
            ]
            actual = box(block["bbox_pdf_points"])
            error = max(abs(x-y) for x, y in zip(expected, actual))
            if error > LIMITS["coordinate_tolerance"]:
                add("coordinate_mapping", "confirmed_error", eid, {"max_error": error})
            if min(x0, y0) < 0 or x1 > width or y1 > height:
                add("image_bounds", "confirmed_error", eid, {"bbox": [x0,y0,x1,y1]})
        except (KeyError, TypeError, ValueError) as exc:
            add("coordinate_structure", "confirmed_error", eid, {"reason": str(exc)})

        # 표 태그 이름의 반복이 문자 반복 신호로 섞이지 않도록 태그를 제외한다.
        # 이 문자열은 검사에만 쓰며 원본 content에는 반영하지 않는다.
        text = re.sub(r"<[^>]*>", " ", content) if block["label"] == "table" else content
        tokens = re.findall(r"\w+", text.casefold())
        if len(tokens) >= LIMITS["minimum_tokens"]:
            token, count = Counter(tokens).most_common(1)[0]
            ratio = count / len(tokens)
            if ratio >= LIMITS["dominant_token_ratio"]:
                add("dominant_token", "suspected", eid,
                    {"token": token, "count": count, "ratio": ratio})
            trigrams = list(zip(tokens, tokens[1:], tokens[2:]))
            # 연속한 세 단어 묶음의 중복 비율이다. 긴 문장이 반복되면 한 단어의
            # 점유율이 낮아도 신호를 얻는다. 정상 반복도 표시될 수 있다.
            duplicate_ratio = 1 - len(set(trigrams)) / len(trigrams)
            if duplicate_ratio >= LIMITS["repeated_trigram_ratio"]:
                add("repeated_trigrams", "suspected", eid,
                    {"ratio": duplicate_ratio})

        # 번호 목록·차트의 연속 행 번호는 원문의 정상 목록일 수도 있다.
        # 자동 삭제하거나 환각으로 확정하지 않고 긴 연속 구간만 표시한다.
        sequences = [
            re.findall(r"(?m)^\s*(\d+)\s*\|", content),
            re.findall(r"\b(\d+)\.", content),
        ]
        for values in sequences:
            numbers = [int(v) for v in values if len(v) < 10]
            longest = streak = 1 if numbers else 0
            for previous, current in zip(numbers, numbers[1:]):
                streak = streak + 1 if current == previous + 1 else 1
                longest = max(longest, streak)
            if longest >= LIMITS["long_sequence_length"]:
                add("long_number_sequence", "suspected", eid, {"length": longest})
                break
        if len(content) > LIMITS["maximum_characters"]:
            add("output_length", "suspected", eid, {"characters": len(content)})

        if block["label"] == "table":
            try:
                measured = TableGrid().measure(content)
                tables.append({"element_id": eid, **measured})
                if measured["unfilled_slots"]:
                    add("table_grid", "suspected", eid, measured)
            except (ValueError, TypeError) as exc:
                add("table_grid", "unresolved", eid, {"reason": str(exc)})

    for gid, members in groups.items():
        if len(members) > 1 and any(not filled for _, filled in members):
            add("merged_content_scope", "suspected", None,
                {"group_id": gid, "members": [eid for eid, _ in members]})
    return {
        "page_id": record["page_id"],
        "processing_status": record["status"],
        "review_status": "needs_review" if findings else "not_reviewed",
        "block_count": len(blocks),
        "tables": tables,
        "findings": findings,
    }
