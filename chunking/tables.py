"""표 블록의 내용을 "열 제목: 값" 행 목록으로 바꾸는 모듈이다.

전체 위치: 청킹 안에서 structure.py가 표·차트 블록을 청크 텍스트로 만들 때 호출한다.
입력: VL의 <table>…, Structure의 <html><body><table>… 문자열, 차트의 "열 | 열" 파이프 표 문자열.
출력: TableRows(열별 제목 경로, 행별 셀 값, 빈 셀·그림 셀 수, 변환 성공 여부).
원본 문자열은 바꾸지 않으며, 호출부가 원본 HTML을 청크 메타데이터에 따로 보존한다.

설계 이유
- 표를 HTML 그대로 임베딩하면 태그가 토큰을 차지하고, 행만 이어 붙이면 숫자가 어느 열의 값인지
  사라진다. 각 값에 열 제목을 붙이면 "2018 Net sales: 32,765"처럼 값의 의미가 청크 안에 남는다.
- rowspan·colspan은 격자로 펼쳐 병합 셀의 값을 덮인 칸마다 반복한다. 다단 헤더는 위에서 아래로
  열 제목을 " / "로 이어 경로로 만든다. 빈 칸을 0으로 바꾸지 않고 값이 없는 쌍은 건너뛴다.
- 파서가 표 구조를 잘못 인식했으면 변환 결과도 틀린다. 이 모듈은 구조를 고치지 않고 변환만 한다.
"""

# 표준 라이브러리: html.parser.HTMLParser는 외부 의존 없이 태그 이벤트를 받아 행·셀을 모은다.
# html.unescape는 &amp; 같은 문자 참조를 원래 글자로 돌리고, re는 공백 정리, dataclass는 결과 묶음이다.
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import re

# 셀 안 그림 참조를 대신할 표시다. 그림 내용은 표 텍스트로 알 수 없어 존재만 남긴다.
IMAGE_CELL_MARKER = "[image]"
# 다단 헤더의 위아래 제목을 잇는 구분자와 행 안 셀 쌍의 구분자다. 출력 형식의 정의다.
HEADER_PATH_SEPARATOR = " / "
PAIR_SEPARATOR = "; "
# 파이프 표의 열 구분자다. 차트 블록이 이 형식으로 값을 돌려준다.
PIPE = "|"


@dataclass
class TableRows:
    """표 변환 결과다. ok=False이면 rows는 비어 있고 호출부가 태그를 지운 텍스트로 대신한다."""

    ok: bool
    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    empty_cells: int = 0
    image_cells: int = 0
    reason: str | None = None

    def row_texts(self) -> list[str]:
        """행마다 "열 제목: 값" 쌍을 이은 문자열이다. 값이 빈 칸은 쌍을 만들지 않는다."""
        texts = []
        for row in self.rows:
            pairs = []
            for index, value in enumerate(row):
                if not value:
                    continue
                header = self.headers[index] if index < len(self.headers) else ""
                pairs.append(f"{header}: {value}" if header and header != value else value)
            if pairs:
                texts.append(PAIR_SEPARATOR.join(pairs))
        return texts


def normalize_cell(text: str) -> str:
    """셀 안 줄바꿈·연속 공백을 공백 하나로 줄인다. 글자 자체는 바꾸지 않는다."""
    return re.sub(r"\s+", " ", unescape(text)).strip()


class _GridParser(HTMLParser):
    """<tr>·<td>·<th>를 따라 행별 셀(내용, rowspan, colspan, 헤더 여부)을 모은다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.tables += 1
        elif tag == "tr":
            self._close_cell()
            self._close_row()
            self.row = []
        elif tag in ("td", "th"):
            self._close_cell()
            if self.row is None:
                self.row = []
            self.cell = {"text": [], "header": tag == "th", "image": False,
                         "rowspan": _span(attrs.get("rowspan")), "colspan": _span(attrs.get("colspan"))}
        elif tag == "img" and self.cell is not None:
            self.cell["image"] = True
        elif tag == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            self._close_cell()
        elif tag == "tr":
            self._close_cell()
            self._close_row()

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"].append(data)

    def _close_cell(self):
        if self.cell is not None and self.row is not None:
            self.cell["text"] = normalize_cell("".join(self.cell["text"]))
            self.row.append(self.cell)
        self.cell = None

    def _close_row(self):
        if self.row:
            self.rows.append(self.row)
        self.row = None

    def close(self):
        super().close()
        # 닫는 태그를 생략한 HTML도 마지막 셀·행을 잃지 않게 한다.
        self._close_cell()
        self._close_row()


def _span(value) -> int:
    """rowspan·colspan 값이 없거나 숫자가 아니면 1로 본다. 0 이하도 1로 다룬다."""
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _expand(rows: list[list[dict]]) -> list[list[dict | None]]:
    """병합 셀을 격자로 펼친다. 덮인 칸에는 같은 셀 객체를 넣어 값과 헤더 여부를 함께 반복한다."""
    grid: list[list[dict | None]] = []
    pending: dict[tuple[int, int], dict] = {}
    for r, row in enumerate(rows):
        line: list[dict | None] = []
        c = 0
        cells = iter(row)
        cell = next(cells, None)
        while cell is not None or any(key[0] == r and key[1] >= c for key in pending):
            if (r, c) in pending:
                line.append(pending.pop((r, c)))
                c += 1
                continue
            if cell is None:
                # 위 행의 rowspan이 오른쪽에 남아 있으면 그 사이 빈 칸을 채운다.
                line.append(None)
                c += 1
                continue
            for dc in range(cell["colspan"]):
                line.append(cell)
                for dr in range(1, cell["rowspan"]):
                    pending[(r + dr, c + dc)] = cell
            c += cell["colspan"]
            cell = next(cells, None)
        grid.append(line)
    width = max((len(line) for line in grid), default=0)
    return [line + [None] * (width - len(line)) for line in grid]


def html_table_rows(html: str, header_rows_without_th: int) -> TableRows:
    """HTML 표를 행 목록으로 바꾼다. <th>가 없으면 앞의 header_rows_without_th 행을 헤더로 본다."""
    parser = _GridParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser는 깨진 입력에서도 대부분 진행하지만 예외를 숨기지 않는다.
        return TableRows(ok=False, reason=f"html_parse_error: {type(exc).__name__}")
    if not parser.rows:
        return TableRows(ok=False, reason="no_rows")
    grid = _expand(parser.rows)
    # 위에서부터 모든 셀이 <th>인 행을 헤더로 본다. 없으면 설정한 행 수를 헤더로 쓴다.
    header_count = 0
    for line in grid:
        if line and all(cell is None or cell["header"] for cell in line) and any(cell for cell in line):
            header_count += 1
        else:
            break
    if header_count == 0:
        header_count = min(header_rows_without_th, max(len(grid) - 1, 0))
    width = len(grid[0]) if grid else 0
    headers = []
    for column in range(width):
        path = []
        for line in grid[:header_count]:
            cell = line[column]
            text = cell["text"] if cell else ""
            # 병합된 상위 제목이 아래 칸까지 반복되면 경로에 한 번만 넣는다.
            if text and (not path or path[-1] != text):
                path.append(text)
        headers.append(HEADER_PATH_SEPARATOR.join(path))
    rows, empty, images = [], 0, 0
    for line in grid[header_count:]:
        values = []
        for cell in line:
            if cell is None or (not cell["text"] and not cell["image"]):
                empty += 1
                values.append("")
            elif cell["image"] and not cell["text"]:
                images += 1
                values.append(IMAGE_CELL_MARKER)
            else:
                values.append(cell["text"])
        rows.append(values)
    return TableRows(ok=True, headers=headers, rows=rows, empty_cells=empty, image_cells=images)


def pipe_table_rows(text: str) -> TableRows:
    """첫 줄이 열 제목인 "a | b" 형식을 행 목록으로 바꾼다. 형식이 아니면 ok=False다."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or any(PIPE not in line for line in lines):
        return TableRows(ok=False, reason="not_pipe_table")
    split = [[normalize_cell(part) for part in line.strip().strip(PIPE).split(PIPE)] for line in lines]
    # 마크다운 구분선(---|---)은 값이 아니므로 건너뛴다.
    split = [cells for cells in split if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c)]
    headers, body = split[0], split[1:]
    empty = sum(1 for row in body for value in row if not value)
    return TableRows(ok=True, headers=headers, rows=body, empty_cells=empty)


def strip_tags(text: str) -> str:
    """표 변환에 실패했을 때 쓰는 대체 텍스트다. 태그를 공백으로 바꾸고 글자는 그대로 둔다."""
    return normalize_cell(re.sub(r"<[^>]*>", " ", text))
