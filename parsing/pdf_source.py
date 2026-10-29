"""
파싱할 PDF의 선택 목록과 HTTP 입력을 준비하는 모듈이다.
설정의 문서명·1부터 시작하는 페이지 번호·중복을 검증하고 PDF URL을 구성한다.
run.py에 반환한 PDF bytes는 pdf_parser.py가 해석한다.
설정 파일 읽기와 HTTP 다운로드를 수행하며 파일 생성·모델 로딩은 하지 않는다.
"""

# 표준 라이브러리: json은 선택 설정, Path는 파일 경로·UTF-8 읽기를 담당한다.
import json
from pathlib import Path

# quote는 문서명을 URL 경로 한 조각으로 인코딩하고 urlsplit은 주소를 검증한다.
# urlopen은 별도 HTTP 라이브러리 없이 PDF bytes를 읽는 데 사용한다.
from urllib.parse import quote, urlsplit
from urllib.request import urlopen


def load_selection(path: Path) -> dict:
    """필드와 번호 체계를 검사한다. 실제 페이지 존재 여부는 PDF를 연 뒤 확인한다."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("선택 설정의 최상위 값은 객체여야 합니다.")
    if data.get("page_numbering") != "pdf_viewer_1_based":
        raise ValueError("페이지 번호 기준은 pdf_viewer_1_based여야 합니다.")

    documents = data.get("documents")
    if not isinstance(documents, list) or not documents:
        raise ValueError("documents는 비어 있지 않은 목록이어야 합니다.")

    seen_names = set()

    for item in documents:
        if not isinstance(item, dict):
            raise ValueError("documents의 각 항목은 객체여야 합니다.")

        name, pages = item.get("document_name"), item.get("page_numbers")

        # 문서명은 확장자 없는 이름이다. 원본 이름을 strip해서 변경하지 않는다.
        # 출력 경로에는 이 이름 대신 내용 해시를 사용해 OS별 경로 문제를 피한다.
        if (
            not isinstance(name, str)
            or not name.strip()
            or name in (".", "..")
            or "/" in name
            or "\\" in name
            or "\x00" in name
        ):
            raise ValueError(f"문서명은 경로를 제외한 비어 있지 않은 이름이어야 합니다: {name!r}")

        if name in seen_names:
            raise ValueError(f"중복 문서: {name}")

        seen_names.add(name)

        if not isinstance(pages, list) or not pages:
            raise ValueError(f"{name}: 비어 있지 않은 페이지 목록이 필요합니다.")

        # bool은 int의 하위 타입이므로 정확한 int 타입만 받는다.
        if any(type(p) is not int or p < 1 for p in pages):
            raise ValueError(f"{name}: 페이지는 1 이상의 정수여야 합니다.")
        if len(pages) != len(set(pages)):
            raise ValueError(f"{name}: 중복 페이지가 있습니다.")

    # 원본 필드·선정 순서를 보존한다. 처리할 페이지 정렬은 run.py의 책임이다.
    return data


def build_pdf_url(base_url: str, document_name: str) -> str:
    """HTTP(S) PDF 폴더 주소와 문서명을 연결한다."""
    parts = urlsplit(base_url)
    if (
        parts.scheme not in ("http", "https")
        or not parts.netloc
        or parts.query
        or parts.fragment
    ):
        raise ValueError("base_url에는 쿼리·fragment 없는 HTTP(S) PDF 폴더 주소가 필요합니다.")

    filename = quote(f"{document_name}.pdf", safe="")
    return f"{base_url.rstrip('/')}/{filename}"


def read_pdf_bytes(pdf_url: str, timeout: float = 30) -> bytes:
    """응답 전체를 메모리로 읽는다. HTTP 오류를 숨기지 않으며 PDF 형식은 이후 검사한다."""
    # 30초는 연결·읽기 대기의 초기 설정이며 총 다운로드 시간 상한은 아니다.
    # CLI에서 바꿀 수 있다. 재시도를 자동 추가해 비용·실패를 숨기지 않는다.
    with urlopen(pdf_url, timeout=timeout) as response:
        data = response.read()

    if not data:
        raise ValueError("다운로드한 파일이 비어 있습니다.")

    # 원본 bytes가 필요해 전체를 메모리에 보관한다. 페이지 단위 처리만으로
    # 큰 원본 PDF의 메모리 비용까지 제거되는 것은 아니다.
    return data
    
