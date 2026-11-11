"""페이지 하나의 레이아웃 블록을 구조 기반 청크로 나누는 모듈이다. 파일 입출력은 하지 않는다.

전체 위치: chunking/run.py가 페이지 JSON을 읽어 이 모듈의 build_page_chunks()에 넘긴다.
입력: 페이지 기록(parsing.blocks), 청킹 설정, 토큰 측정기(tokens.TokenCounter), 블록별 검사 신호,
      선택적 재인식 처리(overrides: 채택 블록의 대체 글자, 복구하지 못한 블록의 제외 사유).
출력: 청크 목록, 제외 블록 목록(사유 포함), 페이지 통계.

처리 순서
1. 라벨을 역할로 나눈다: 제목(단계별), 본문, 표, 차트, 그림, 캡션, 제외(머리글·바닥글·쪽 번호).
   설정에 없는 라벨은 본문으로 다루고 통계에 따로 센다. 빈 블록은 지우지 않고 제외 사유로 남긴다.
2. 캡션은 가장 가까운 표·차트·그림에 붙인다. 설정한 거리 안에 대상이 없으면 본문으로 다룬다.
3. 읽기 순서(파서가 돌려준 블록 순서)대로 걸으며 제목이 바뀔 때마다 본문 묶음을 끊는다.
   표·차트·그림은 별도 청크로 만들고, 모든 청크 앞에 현재 제목 경로를 붙인다.
4. 최대 토큰을 넘으면 본문은 블록 → 문장 → 토큰 경계 순서로, 표는 행 경계로 나눈다.

청크는 한 페이지 안에서만 만든다. 선정 페이지가 연속하지 않아 페이지를 넘는 문맥을 확인할 수 없다.
제목 경로는 검색 문맥이지 청크 내용이 아니므로 heading_element_ids로 따로 기록한다.
"""

# 표준 라이브러리: dataclass는 블록 묶음, re는 문장 경계·그림 태그 처리에 쓴다.
from dataclasses import dataclass, field
import re

# 내부 모듈: tables는 표·차트 내용을 "열 제목: 값" 행으로 바꾼다.
from .tables import html_table_rows, pipe_table_rows, strip_tags

# 영어 문장 경계다. 마침표·물음표·느낌표 뒤 공백 다음에 대문자·숫자·여는 기호가 오면 나눈다.
# 현재 표본이 영어 PDF라서 정한 언어 규칙이며 약어(e.g.)에서 잘못 나눌 수 있다.
SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9])")
# 블록 내용 안의 그림 참조 태그다. 글자가 아니므로 검색 텍스트에서 지운다.
IMAGE_TAG = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
# 블록 목록을 믿고 청크로 만들 수 있는 페이지 상태다. failed·skipped 페이지는 블록이 없다.
PARSED_STATUSES = frozenset({"ok", "empty", "partial"})
# 청크 종류의 정의다.
TEXT, TABLE, CHART, VISUAL = "text", "table", "chart", "visual"
# 토큰 경계로 자를 때 앞머리와 본문 사이 줄바꿈이 차지할 토큰 여유분이다. 줄바꿈 두 개가 많아야
# 토큰 두 개로 인코딩되는 것을 기준으로 잡았고, 그래도 넘는 청크는 실행 요약에서 따로 센다.
SEPARATOR_TOKEN_MARGIN = 2


# eq=False: 같은 내용의 블록이라도 페이지 안 위치가 다르면 다른 블록이다. 비교는 객체 동일성으로 한다.
@dataclass(eq=False)
class Block:
    """청킹에 쓰는 블록이다. data는 페이지 JSON의 원래 블록이며 수정하지 않는다."""

    order: int
    role: str
    level: int | None
    data: dict
    text: str
    flags: set
    # 청크에 쓸 원래 내용이다. 재인식으로 대체한 블록은 data["content"]가 아니라 대체 글자다.
    content: str = ""
    # 대체 글자의 출처다. 파싱 결과를 그대로 쓰면 None이다.
    replacement: dict | None = None
    captions: list = field(default_factory=list)

    @property
    def element_id(self) -> str:
        return self.data["element_id"]


@dataclass(eq=False)
class Unit:
    """청크에 넣는 최소 조각이다. 같은 블록에서 나온 조각끼리는 공백, 다른 블록과는 줄바꿈으로 잇는다."""

    text: str
    block: Block
    flags: set = field(default_factory=set)
    # 같은 블록의 앞 조각과 이을 때 쓰는 구분자다. 문장 조각은 공백, 표 행·그림 줄은 줄바꿈이다.
    separator: str = " "


def label_roles(labels: dict) -> dict:
    """설정의 라벨 목록을 {라벨: (역할, 제목 단계)}로 바꾼다. 같은 라벨이 두 역할에 있으면 거부한다."""
    roles = {}

    def put(label, role, level=None):
        if label in roles:
            raise ValueError(f"라벨이 두 역할에 있습니다: {label}")
        roles[label] = (role, level)

    for level, names in enumerate(labels["title_levels"]):
        for name in names:
            put(name, "title", level)
    for role in ("text", "table", "chart", "visual", "caption", "excluded"):
        for name in labels[role]:
            put(name, role)
    return roles


def _box(block: dict):
    """캡션 거리 계산에 쓸 사각형이다. PDF 좌표를 먼저 쓰고 없으면 이미지 좌표를 쓴다."""
    for key in ("bbox_pdf_points", "bbox_image_pixels"):
        value = block.get(key)
        if isinstance(value, list) and len(value) == 4:
            return value
    return None


def _vertical_gap(a, b) -> float:
    """두 사각형의 세로 간격이다. 겹치면 0이다. 캡션은 대상의 바로 위·아래에 오는 경우가 많다."""
    return max(0.0, a[1] - b[3], b[1] - a[3])


class PageChunker:
    """페이지 하나를 처리하는 동안의 설정·측정기·결과를 묶는다. 페이지마다 새로 만든다."""

    def __init__(self, record: dict, settings: dict, counter, flags_by_element: dict,
                 overrides: dict | None = None):
        self.record = record
        self.settings = settings
        self.counter = counter
        self.flags_by_element = flags_by_element
        # {element_id: {"content": 대체 글자, "source": 출처}} 또는 {element_id: {"exclude": 사유, "source": 출처}}
        self.overrides = overrides or {}
        self.roles = label_roles(settings["labels"])
        self.markers = settings["kind_markers"]
        self.max_tokens = settings["max_tokens"]
        self.overlap_tokens = settings["overlap_tokens"]
        self.chunks: list[dict] = []
        self.excluded: list[dict] = []
        self.stats = {"unmapped_labels": {}, "captions_attached": 0, "captions_as_text": 0,
                      "title_only_chunks": 0, "hard_splits": 0, "table_parse_failures": 0}

    # 1. 역할 분류 ---------------------------------------------------------------
    def classify(self) -> list[Block]:
        blocks = []
        for order, data in enumerate(self.record["parsing"]["blocks"]):
            label = data["label"]
            role, level = self.roles.get(label, (None, None))
            if role is None:
                # 설정에 없는 라벨은 내용을 잃지 않도록 본문으로 다루고 통계에 남긴다.
                role = "text"
                self.stats["unmapped_labels"][label] = self.stats["unmapped_labels"].get(label, 0) + 1
            if role == "excluded":
                self.exclude(data, "excluded_label")
                continue
            override = self.overrides.get(data["element_id"])
            if override and "exclude" in override:
                # 생성 한도에서 잘린 반복 출력을 복구하지 못한 블록이다. 지우지 않고 사유·출처를 남긴다.
                self.exclude(data, override["exclude"], override["source"])
                continue
            raw = override["content"] if override else data["content"]
            text = raw if role == "table" else IMAGE_TAG.sub(" ", raw).strip()
            if not text.strip() and role != "visual":
                # VL 병합 인식에서는 내용이 같은 그룹의 다른 블록에 들어가 빈 블록이 남는다.
                self.exclude(data, "empty_content")
                continue
            flags = set(self.flags_by_element.get(data["element_id"], ()))
            blocks.append(Block(order, role, level, data, text, flags, content=raw,
                                replacement=override["source"] if override else None))
        return blocks

    def exclude(self, data: dict, reason: str, source: dict | None = None):
        item = {"element_id": data["element_id"], "label": data["label"], "reason": reason,
                "characters": len(data.get("content") or "")}
        if source is not None:
            item["rerecognition"] = source
        self.excluded.append(item)

    # 2. 캡션 연결 ---------------------------------------------------------------
    def attach_captions(self, blocks: list[Block]) -> list[Block]:
        targets = [b for b in blocks if b.role in ("table", "chart", "visual") and _box(b.data)]
        limit = self.settings["caption_max_gap_points"]
        remaining = []
        for block in blocks:
            if block.role != "caption":
                remaining.append(block)
                continue
            box = _box(block.data)
            candidates = sorted(((_vertical_gap(box, _box(t.data)), abs(t.order - block.order), t)
                                 for t in targets), key=lambda item: item[:2]) if box else []
            if candidates and candidates[0][0] <= limit:
                candidates[0][2].captions.append(block)
                self.stats["captions_attached"] += 1
            else:
                # 붙일 대상이 없으면 캡션 글자를 잃지 않도록 본문으로 다룬다.
                block.role = "text"
                remaining.append(block)
                self.stats["captions_as_text"] += 1
        return remaining

    # 3. 읽기 순서대로 묶기 -------------------------------------------------------
    def build(self) -> None:
        blocks = self.attach_captions(self.classify())
        headings: list[Block | None] = []
        section: list[Block] = []
        used_titles: set[str] = set()
        titles: list[Block] = []

        def heading_blocks():
            return [h for h in headings if h is not None]

        def flush():
            if section:
                current = heading_blocks()
                used_titles.update(h.element_id for h in current)
                self.emit_text(section[:], current)
                section.clear()

        for block in blocks:
            if block.role == "title":
                flush()
                titles.append(block)
                # 상위 제목이 바뀌면 하위 제목 문맥을 지운다.
                del headings[block.level:]
                headings.extend([None] * (block.level - len(headings)))
                headings.append(block)
            elif block.role in ("table", "chart", "visual"):
                flush()
                current = heading_blocks()
                used_titles.update(h.element_id for h in current)
                self.emit_structured(block, current)
            else:
                section.append(block)
        flush()
        # 뒤따르는 내용이 없어 어떤 청크의 문맥으로도 쓰이지 않은 제목은 단독 청크로 남긴다.
        for title in titles:
            if title.element_id not in used_titles:
                self.stats["title_only_chunks"] += 1
                self.emit_text([title], [], extra_flags={"title_only"})

    # 4. 길이 맞추기와 청크 생성 ----------------------------------------------------
    def prefix_lines(self, headings: list[Block], block: Block | None = None, marker: str | None = None):
        lines = []
        if headings:
            lines.append(" > ".join(h.text for h in headings))
        if block is not None:
            lines += [f"{self.markers['caption']} {c.text}" for c in block.captions]
        if marker:
            lines.append(marker)
        return lines

    def compose(self, head: list[str], units: list[Unit]) -> str:
        body = []
        for unit in units:
            if body and body[-1][0] is unit.block:
                body[-1] = (unit.block, f"{body[-1][1]}{unit.separator}{unit.text}")
            else:
                body.append((unit.block, unit.text))
        parts = ["\n".join(head)] if head else []
        parts.append("\n".join(text for _, text in body))
        return "\n\n".join(p for p in parts if p)

    def split_units(self, head: list[str], block: Block, pieces: list[str], separator: str = " ") -> list[Unit]:
        """조각이 혼자서도 한도를 넘으면 문장 → 토큰 경계 순서로 더 나눈다."""
        units = []
        for piece in pieces:
            if self.counter.count(self.compose(head, [Unit(piece, block)])) <= self.max_tokens:
                units.append(Unit(piece, block, separator=separator))
                continue
            for sentence in SENTENCE_BOUNDARY.split(piece):
                if self.counter.count(self.compose(head, [Unit(sentence, block)])) <= self.max_tokens:
                    units.append(Unit(sentence, block, separator=separator))
                    continue
                budget = (self.max_tokens - self.counter.count(self.compose(head, []) or " ")
                          - SEPARATOR_TOKEN_MARGIN)
                for part in self.counter.split(sentence, budget):
                    self.stats["hard_splits"] += 1
                    units.append(Unit(part, block, {"hard_split"}, separator=separator))
        return units

    def pack(self, head: list[str], units: list[Unit]) -> list[list[Unit]]:
        """조각을 앞에서부터 한도까지 채운다. 겹침은 앞 청크 끝의 조각을 통째로 반복한다."""
        groups, current = [], []
        for unit in units:
            if not current or self.counter.count(self.compose(head, current + [unit])) <= self.max_tokens:
                current.append(unit)
                continue
            groups.append(current)
            tail = self.overlap_tail(current)
            current = tail + [unit]
            if tail and self.counter.count(self.compose(head, current)) > self.max_tokens:
                current = [unit]
        if current:
            groups.append(current)
        return groups

    def overlap_tail(self, units: list[Unit]) -> list[Unit]:
        if self.overlap_tokens <= 0:
            return []
        tail, total = [], 0
        for unit in reversed(units):
            total += self.counter.count(unit.text)
            if total > self.overlap_tokens:
                break
            tail.insert(0, unit)
        # 앞 청크 전체를 반복하면 겹침이 아니라 중복이므로 한 조각 이상은 남긴다.
        return tail if len(tail) < len(units) else tail[1:]

    def emit_text(self, blocks: list[Block], headings: list[Block], extra_flags: set | None = None):
        head = self.prefix_lines(headings)
        units = []
        for block in blocks:
            units += self.split_units(head, block, [block.text])
        groups = self.pack(head, units)
        for index, group in enumerate(groups):
            self.add_chunk(TEXT, self.compose(head, group), group, headings,
                           {"split": {"index": index, "count": len(groups)}}, extra_flags)

    def emit_structured(self, block: Block, headings: list[Block]):
        """표·차트는 행 단위로, 그림은 줄 단위로 나눈다. 앞머리(제목·캡션·종류 표시)는 조각마다 반복한다."""
        content = block.content
        table = None
        if block.role == "table":
            table = html_table_rows(content, self.settings["table_header_rows_without_th"])
        elif block.role == "chart":
            table = pipe_table_rows(block.text)
        marker = self.markers[block.role]
        head = self.prefix_lines(headings, block, marker)
        extra_flags = set()
        if table is not None and table.ok and table.row_texts():
            pieces = table.row_texts()
            table_meta = {"format": "html" if block.role == "table" else "pipe", "headers": table.headers,
                          "row_count": len(table.rows), "empty_cells": table.empty_cells,
                          "image_cells": table.image_cells, "source_content": content}
        else:
            if block.role == "table":
                self.stats["table_parse_failures"] += 1
                extra_flags.add("table_parse_failed")
            text = strip_tags(content) if block.role == "table" else block.text
            pieces = [line for line in text.splitlines() if line.strip()] or ([text] if text.strip() else [])
            table_meta = None
        if not pieces and not block.captions:
            self.exclude(block.data, "empty_visual")
            return
        units = self.split_units(head, block, pieces, separator="\n") if pieces else []
        groups = self.pack(head, units) if units else [[]]
        for index, group in enumerate(groups):
            extra = {"split": {"index": index, "count": len(groups)}}
            if table_meta is not None:
                extra["table"] = {**table_meta, "piece_rows": len(group)}
            self.add_chunk(block.role, self.compose(head, group) if group else "\n".join(head),
                           group, headings, extra, extra_flags, structured=block)

    def add_chunk(self, kind: str, text: str, units: list[Unit], headings: list[Block], extra: dict,
                  extra_flags: set | None = None, structured: Block | None = None):
        members: list[Block] = []
        for block in ([structured] if structured else []) + [u.block for u in units]:
            if block not in members:
                members.append(block)
        if structured:
            members += [c for c in structured.captions if c not in members]
        flags = set(extra_flags or ())
        for block in members:
            flags |= block.flags
        for unit in units:
            flags |= unit.flags
        page = self.record
        self.chunks.append({
            "chunk_id": None,
            "id_origin": "project_generated",
            "kind": kind,
            "document_id": page["document_id"],
            "document_name": page["document_name"],
            "page_id": page["page_id"],
            "page_number": page["page_number"],
            "text": text,
            "token_count": self.counter.count(text),
            "heading": " > ".join(h.text for h in headings) or None,
            "heading_element_ids": [h.element_id for h in headings],
            "element_ids": [b.element_id for b in members],
            "caption_element_ids": [c.element_id for c in structured.captions] if structured else [],
            "blocks": [{"element_id": b.element_id, "label": b.data["label"], "role": b.role,
                        "bbox_pdf_points": b.data.get("bbox_pdf_points"),
                        "bbox_image_pixels": b.data.get("bbox_image_pixels"),
                        "content_source": "rerecognition" if b.replacement else "parsing",
                        **({"rerecognition": b.replacement} if b.replacement else {})} for b in members],
            "flags": sorted(flags),
            **extra,
        })


def build_page_chunks(record: dict, settings: dict, counter, flags_by_element: dict,
                      parser_tag: str, overrides: dict | None = None) -> tuple[list[dict], list[dict], dict]:
    """페이지 하나의 청크·제외 블록·통계를 반환한다. 청크 ID는 페이지 안 순번으로 만든다.

    overrides는 재인식 결과다. 채택된 블록은 대체 글자로, 복구하지 못한 블록은 제외 기록으로 처리한다.
    """
    parsing = record.get("parsing") or {}
    if record.get("status") not in PARSED_STATUSES or parsing.get("status") not in PARSED_STATUSES:
        return [], [{"element_id": None, "label": None, "characters": 0,
                     "reason": f"page_not_parsed: {record.get('status')}/{parsing.get('status')}"}], {}
    chunker = PageChunker(record, settings, counter, flags_by_element, overrides)
    chunker.build()
    # 읽기 순서를 따라 만든 순서 그대로 번호를 붙인다. 제목 단독 청크는 맨 뒤에 온다.
    for index, chunk in enumerate(chunker.chunks):
        chunk["chunk_id"] = f"{record['page_id']}:{parser_tag}:chunk:{index}"
    return chunker.chunks, chunker.excluded, chunker.stats
