"""
추출 영역을 원본 PDF의 복사본에 표시하는 검토 모듈이다.
run.py가 원본 문서·페이지 번호·PDF 좌표가 붙은 파싱 결과를 전달한다.
검토 문서에 페이지를 복사하고 영역 테두리·목록 번호·내용 주석을 추가한 뒤
표시 개수와 경고를 반환한다. 원본 문서와 모델 결과를 수정하지 않는다.
PDF bytes 생성은 run.py, 파일 저장은 result_store.py가 맡는다.

좌표 근거: PyMuPDF Page 문서의 회전 전 좌표 규칙과 rotation_matrix.
https://pymupdf.readthedocs.io/en/latest/page.html
"""

# PyMuPDF는 페이지 복사·PDF 주석·선 그리기를 제공한다.
# 이미 사용하는 PDF 라이브러리로 원문을 유지하며 별도 래스터 PDF 변환을 피한다.
import pymupdf


def add_review_page(
    review: pymupdf.Document,
    source: pymupdf.Document,
    page_number: int,
    result: dict,
    json_reference: str,
) -> dict:
    """선택 페이지 한 장을 복사하고 PDF 좌표 기준의 영역을 표시한다."""
    # 원본의 내용·회전·주석을 복사한다. 원본 파일에 save하지 않는다.
    review.insert_pdf(
        source,
        from_page=page_number - 1,
        to_page=page_number - 1,
        links=False,
        annots=True,
    )
    page = review[-1]
    report = {
        "review_page_number": len(review),
        "status": "ok",
        "drawn_count": 0,
        "warnings": [],
        "json_path": json_reference,
    }

    # 페이지 상태는 영역이 없거나 파싱에 실패한 페이지도 검토 PDF에 남기기 위한
    # 메모다. 모든 실패 페이지에 가짜 bbox를 만드는 방식은 사용하지 않는다.
    # 뷰어 좌상단 위치를 회전 전 좌표로 옮겨 PDF 회전이 있는 페이지도 배치한다.
    note_point = pymupdf.Point(10, 10) * page.derotation_matrix
    note = page.add_text_annot(note_point, f"JSON: {json_reference}\n상태: {result['status']}")
    note.set_info(title="파싱 실행 정보", subject=json_reference)
    note.update()

    # PDF 내용·주석 API의 좌표는 회전 전 페이지 기준이다. 화면에 보이는 영역을
    # 역회전해 같은 좌표계로 맞춘 뒤 경계 검사를 한다.
    bounds = page.rect * page.derotation_matrix

    for block in result.get("blocks", []):
        element_id = block["element_id"]
        bbox = block.get("bbox_pdf_points")
        if bbox is None:
            report["warnings"].append({"element_id": element_id, "reason": "PDF 좌표 없음"})
            continue

        rect = pymupdf.Rect(bbox)
        if rect.is_empty or rect.is_infinite:
            report["warnings"].append({"element_id": element_id, "reason": "빈 영역 또는 무한 영역"})
            continue

        # 경계 초과는 경고로 남긴다. JSON의 원래 좌표를 잘라 정상처럼 숨기지 않는다.
        visible = rect & bounds
        if not bounds.contains(rect):
            report["warnings"].append({"element_id": element_id, "reason": "페이지 경계 초과"})
        if visible.is_empty:
            continue

        # 렌더링용 테두리만 표시 영역으로 제한한다. 원본 숫자는 JSON에 유지한다.
        annot = page.add_rect_annot(visible)
        annot.set_colors(stroke=(0.05, 0.25, 0.85))
        annot.set_border(width=0.7)
        annot.set_info(
            title=element_id,
            subject=f"{block['label']} | {json_reference}",
            content=(
                f"요소 ID: {element_id}\n원본 목록 인덱스: {block['source_list_index']}\n"
                f"모델 읽기 순서: {block.get('model_block_order')}\n"
                f"JSON: {json_reference}\n\n{block['content']}"
            ),
        )
        annot.update()

        # 작은 숫자는 원본 목록의 1부터 시작하는 순번이다. 올바른 읽기 순서의
        # 판정값이 아니다. 전체 ID·추출 내용은 주석 또는 JSON에서 확인한다.
        # 한글 본문을 작은 상자에 욱여넣지 않아 원문 가림을 줄인다.
        label = str(block["source_list_index"] + 1)
        point = pymupdf.Point(visible.x0 + 1, min(visible.y0 + 7, bounds.y1 - 2))
        page.insert_text(point, label, fontsize=7, color=(0.05, 0.25, 0.85), overlay=True)
        report["drawn_count"] += 1

    if report["warnings"]:
        report["status"] = "warning"

    return report
