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

# 0.2.0: 생략 가능한 표 종료 태그 허용, 미해결만 있는 페이지의 검토 상태 분리
# 0.3.0: 반복·길이 검사를 content_findings()로 분리, Structure 표 구조 토큰 상한(table_structure_limit) 추가
# 0.3.1: 번호 자릿수 상한을 LIMITS의 maximum_sequence_digits로 옮김. 값(9자리)과 판정은 0.3.0과 같다
RULE_VERSION = "0.3.1"
# 아래 수치는 개발용 초기값이며 환각 확정 기준이 아니다. 신호는 원본과 대조할 후보를 고를 뿐이다.
# 반복·길이 수치를 처음 정한 이유는 커밋·문서 어디에도 기록되지 않았다. 그래서 값마다 단위·역할과
# 저장된 실행에서 잰 분포만 적는다. 측정은 tools/limits_distribution.py로 2026-11-12에 했고, 대상은
# 파싱 실행 4개(VL 2026-10-27·2026-11-10, Structure 2026-10-28·2026-11-11)의 저장된 선정 페이지 블록 1,651개다.
# 이미 본 개발 표본이므로 일반적인 기준의 근거가 아니며, 평가 성능으로 쓰지 않는다.
LIMITS = {
    # 단위: \w+ 토큰 수. 이보다 짧은 블록은 반복 비율을 보지 않는다. 짧으면 우연한 반복도 비율이 높다.
    # 측정: 3~39토큰 블록 880개 중 최다 토큰 비율 0.5 이상은 10개였고 모두 4~5토큰 블록이다.
    "minimum_tokens": 40,
    # 단위: 가장 많은 토큰 수 / 전체 토큰 수. 한 단어가 블록을 차지하는 반복을 찾는다.
    # 측정: 40토큰 이상 485개 중 최댓값 0.99(VL Toyota p2, 두 실행), 그다음 0.35. 표본에서는 0.35~0.99
    # 사이 어느 값이든 결과가 같아, 이 표본이 0.5라는 값 자체를 뒷받침하지는 않는다.
    "dominant_token_ratio": 0.5,
    # 단위: 블록 content의 문자 수. 비정상적으로 긴 출력을 찾는다.
    # 측정: 최댓값 14,355(VL Toyota p2), 그다음 8,068(VL RAG 논문 p6 표). 정상적으로 긴 블록도 걸릴 수 있다.
    "maximum_characters": 10000,
    # 단위: 1 - 고유한 세 단어 묶음 수 / 전체 묶음 수. 한 단어 비율이 낮아도 문장이 반복되면 잡는다.
    # 측정: 40토큰 이상 중 최댓값 0.989(VL Toyota p2), 그다음 0.39. 0.39~0.989 사이면 표본 결과가 같다.
    "repeated_trigram_ratio": 0.7,
    # 단위: 1씩 증가하는 번호의 연속 개수. 원문의 정상 목록일 수도 있어 표시만 한다.
    # 측정: 834(VL Toyota p7), 697(VL 슬라이드 p6), 63(Structure 슬라이드 p6), 그다음 3.
    # 3을 넘는 값이면 표본 결과가 같다.
    "long_sequence_length": 50,
    # 단위: 번호 숫자의 자릿수. 이보다 긴 값은 연속 번호 검사에서 뺀다. 빠진 값은 신호로 남지 않는다.
    # Python int()는 기본 4,300자리를 넘는 문자열을 거부하고 큰 수는 변환 비용도 크므로 상한이 필요하다.
    # 측정: 10자리 이상은 Structure 슬라이드 p15의 '1994199519965' 같은 값뿐이고, 연도가 붙어 읽힌 형태다.
    "maximum_sequence_digits": 9,
    # 단위: 병합을 펼친 표 격자의 칸 수. 품질 기준이 아니라 잘못된 rowspan·colspan의 자원 한도다.
    "maximum_grid_slots": 20000,
    # 단위: PDF point(1/72 inch). 같은 행렬로 다시 계산한 좌표의 부동소수점 비교용이며
    # 문장 위치의 허용 오차가 아니다.
    "coordinate_tolerance": 0.00001,
    # PP-StructureV3 표 구조 모델(SLANet_plus·SLANeXt_wired)의 inference.yml max_text_length다.
    # 디코더는 이 길이까지만 구조 토큰을 내므로, 이 수에 닿은 표는 뒤쪽 행이 잘렸을 수 있다.
    # 근거: docs/parsing/log/2026-11-11_structure.md 3.1절(RAG 논문 p6, 다시 센 구조 토큰 501개).
    "table_structure_tokens": 500,
}
# 표 구조 토큰 상한을 적용할 파싱 출처다. VL 표는 VLM이 직접 만들어 이 상한과 관계없다.
STRUCTURE_TABLE_SOURCE = "pp_structure_v3"
# 표 구조 모델 사전의 구역 태그다. 각각 구조 토큰 하나로 센다.
STRUCTURE_SECTION_TOKENS = ("<thead>", "</thead>", "<tbody>", "</tbody>")
# 병합 범위 속성(colspan·rowspan)은 사전에서 값까지 포함한 토큰 하나다.
SPAN_ATTRIBUTE = re.compile(r'\s(?:colspan|rowspan)="\d+"')
TABLE_CELL = re.compile(r"<td(\s[^>]*)?>.*?</td>", re.IGNORECASE | re.DOTALL)
# content_findings()가 내는 규칙 이름이다. 블록 내용을 다른 글자로 바꿨을 때 다시 계산할 대상이다.
CONTENT_RULES = frozenset({"dominant_token", "repeated_trigrams", "long_number_sequence", "output_length"})


class TableGrid(HTMLParser):
    """단일 표의 행·셀·병합 범위를 읽는다. 일반 HTML의 완전한 유효성 검사기는 아니다.

    HTML 표준은 </td>·</th>·</tr> 종료 태그의 생략을 허용한다(WHATWG HTML "Optional tags").
    다음 셀·행이 시작되거나 상위 요소가 닫히면 열린 셀·행을 닫은 것으로 본다.
    생략을 오류로 세면 정상 표가 구조 확인 불가로 분류된다.
    </table>은 생략할 수 없으므로 끝까지 닫히지 않은 표는 계속 거부한다.
    """

    # thead·tbody·tfoot의 시작·종료는 열린 행을 닫는다. 셀 위치 계산에는 영향이 없다.
    SECTION_TAGS = ("thead", "tbody", "tfoot")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.table_count = 0
        self.table_open = False

    def close_cell(self):
        if self.cell is not None:
            self.row.append(self.cell)
            self.cell = None

    def close_row(self):
        self.close_cell()
        if self.row is not None:
            self.rows.append(self.row)
            self.row = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.table_count += 1
            if self.table_count > 1:
                raise ValueError("한 블록의 중첩 표 또는 여러 표는 별도 처리가 필요합니다.")
            self.table_open = True
        elif tag in self.SECTION_TAGS:
            if not self.table_open:
                raise ValueError("표 바깥의 표 구역 태그입니다.")
            self.close_row()
        elif tag == "tr":
            if not self.table_open:
                raise ValueError("표 바깥의 행입니다.")
            self.close_row()
            self.row = []
        elif tag in ("td", "th"):
            # 행 없이 시작한 셀은 브라우저가 행을 만들어 주지만, 모델 출력의 구조 손상일 수
            # 있어 추정하지 않고 거부한다.
            if self.row is None:
                raise ValueError("행 바깥의 셀입니다.")
            self.close_cell()
            rowspan = int(attrs.get("rowspan", "1"))
            colspan = int(attrs.get("colspan", "1"))
            if min(rowspan, colspan) < 1:
                raise ValueError("현재 검사기는 양수 병합 범위만 지원합니다.")
            self.cell = (rowspan, colspan)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.cell is None:
                raise ValueError("열린 셀이 없습니다.")
            self.close_cell()
        elif tag == "tr":
            if self.row is None:
                raise ValueError("열린 행이 없습니다.")
            self.close_row()
        elif tag in self.SECTION_TAGS:
            self.close_row()
        elif tag == "table":
            if not self.table_open:
                raise ValueError("열린 표가 없습니다.")
            self.close_row()
            self.table_open = False

    def measure(self, html):
        """양수 병합 범위를 가진 단일 표를 검사한다. 생략 가능한 종료 태그는 허용한다."""
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


def structure_token_count(html):
    """표 HTML을 표 구조 모델의 출력 토큰 수로 다시 센다. 셀 안 글자는 OCR 결과라 세지 않는다.

    모델 사전은 병합 없는 셀을 "<td></td>" 한 토큰으로 묶는다(merge_no_span_structure: true).
    병합 셀은 "<td", 속성마다 한 토큰, ">", "</td>"로 센다. 행은 "<tr>"·"</tr>" 두 토큰이다.
    html·body·table 태그는 후처리가 덧붙인 것이라 모델 토큰이 아니다.
    """
    count = sum(html.count(tag) for tag in STRUCTURE_SECTION_TOKENS)
    count += html.count("<tr>") + html.count("</tr>")
    for match in TABLE_CELL.finditer(html):
        attributes = match.group(1) or ""
        spans = len(SPAN_ATTRIBUTE.findall(attributes))
        count += 1 if not spans else 3 + spans
    return count


def content_measurements(content, label):
    """블록 내용 하나의 반복·길이 측정값이다. 임계값은 적용하지 않는다.

    content_findings()가 이 값에 LIMITS를 적용하고, tools/limits_distribution.py가 같은 값으로
    저장된 실행의 분포를 잰다. 측정과 판정을 나눠 두 곳이 같은 계산을 쓰게 한다.
    """
    # 표 태그 이름의 반복이 문자 반복 신호로 섞이지 않도록 태그를 제외한다.
    # 이 문자열은 검사에만 쓰며 원본 content에는 반영하지 않는다.
    text = re.sub(r"<[^>]*>", " ", content) if label == "table" else content
    tokens = re.findall(r"\w+", text.casefold())
    measured = {"tokens": len(tokens), "characters": len(content),
                "dominant_token": None, "dominant_count": 0, "dominant_ratio": None,
                "repeated_trigram_ratio": None}
    if tokens:
        token, count = Counter(tokens).most_common(1)[0]
        measured.update(dominant_token=token, dominant_count=count, dominant_ratio=count / len(tokens))
    trigrams = list(zip(tokens, tokens[1:], tokens[2:]))
    if trigrams:
        # 연속한 세 단어 묶음의 중복 비율이다. 긴 문장이 반복되면 한 단어의
        # 점유율이 낮아도 신호를 얻는다. 정상 반복도 표시될 수 있다.
        measured["repeated_trigram_ratio"] = 1 - len(set(trigrams)) / len(trigrams)

    # 번호 목록·차트의 연속 행 번호는 원문의 정상 목록일 수도 있다.
    # 표 행 머리 번호와 "1." 형식 번호를 이 순서로 따로 세어 각각의 최장 연속 길이를 남긴다.
    sequences = [
        re.findall(r"(?m)^\s*(\d+)\s*\|", content),
        re.findall(r"\b(\d+)\.", content),
    ]
    measured["sequence_lengths"] = []
    measured["skipped_long_numbers"] = []
    for values in sequences:
        measured["skipped_long_numbers"] += [v for v in values if len(v) > LIMITS["maximum_sequence_digits"]]
        numbers = [int(v) for v in values if len(v) <= LIMITS["maximum_sequence_digits"]]
        longest = streak = 1 if numbers else 0
        for previous, current in zip(numbers, numbers[1:]):
            streak = streak + 1 if current == previous + 1 else 1
            longest = max(longest, streak)
        measured["sequence_lengths"].append(longest)
    return measured


def content_findings(content, label):
    """블록 내용 하나의 반복·길이 신호를 (규칙, 근거) 목록으로 반환한다. 페이지 정보가 필요 없다.

    inspect_page와 재인식 단계(rerecognize.py)가 같은 기준으로 판정하도록 분리했다.
    """
    found = []
    measured = content_measurements(content, label)
    if measured["tokens"] >= LIMITS["minimum_tokens"]:
        if measured["dominant_ratio"] >= LIMITS["dominant_token_ratio"]:
            found.append(("dominant_token", {"token": measured["dominant_token"],
                                             "count": measured["dominant_count"],
                                             "ratio": measured["dominant_ratio"]}))
        if measured["repeated_trigram_ratio"] >= LIMITS["repeated_trigram_ratio"]:
            found.append(("repeated_trigrams", {"ratio": measured["repeated_trigram_ratio"]}))
    # 자동 삭제하거나 환각으로 확정하지 않고 긴 연속 구간만 표시한다. 두 형식 중 먼저 넘은 쪽을 근거로 남긴다.
    for length in measured["sequence_lengths"]:
        if length >= LIMITS["long_sequence_length"]:
            found.append(("long_number_sequence", {"length": length}))
            break
    if measured["characters"] > LIMITS["maximum_characters"]:
        found.append(("output_length", {"characters": measured["characters"]}))
    return found


def review_status(findings):
    """의심·확정 오류가 있으면 검토 필요, 확인하지 못한 항목만 있으면 미해결로 나눈다.

    원본 PDF가 없어 해시를 대조하지 못한 페이지를 내용이 의심되는 페이지와 같은 상태로
    세면 검토 대상 수가 부풀고 실제 신호가 묻힌다.
    """
    if any(f["status"] in ("confirmed_error", "suspected") for f in findings):
        return "needs_review"
    return "unresolved" if findings else "not_reviewed"


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

        for rule, evidence in content_findings(content, block["label"]):
            add(rule, "suspected", eid, evidence)

        if block["label"] == "table":
            try:
                measured = TableGrid().measure(content)
                tables.append({"element_id": eid, **measured})
                if measured["unfilled_slots"]:
                    add("table_grid", "suspected", eid, measured)
            except (ValueError, TypeError) as exc:
                add("table_grid", "unresolved", eid, {"reason": str(exc)})
            # 구조 토큰이 모델 상한에 닿은 표는 뒤쪽 행이 잘리고 남은 글자가 마지막 칸에 몰렸을 수 있다.
            # 내용은 고치지 않고 신호만 남긴다.
            if parsing.get("source") == STRUCTURE_TABLE_SOURCE:
                tokens_used = structure_token_count(content)
                if tokens_used >= LIMITS["table_structure_tokens"]:
                    add("table_structure_limit", "suspected", eid,
                        {"structure_tokens": tokens_used, "limit": LIMITS["table_structure_tokens"]})

    for gid, members in groups.items():
        if len(members) > 1 and any(not filled for _, filled in members):
            add("merged_content_scope", "suspected", None,
                {"group_id": gid, "members": [eid for eid, _ in members]})
    return {
        "page_id": record["page_id"],
        "processing_status": record["status"],
        "review_status": review_status(findings),
        "block_count": len(blocks),
        "tables": tables,
        "findings": findings,
    }
