"""
run.py가 전달한 결과를 새 실행 폴더의 JSON·PDF·PNG 파일로 저장하는 모듈이다.
실행 폴더의 저장 형식(문서 ID, 폴더·파일 이름, 페이지 번호 기준)도 여기서만 정의한다.
저장(run.py, resume.py)과 읽기(validate_run.py, rerecognize.py, experiments.compare, chunking.run)가
같은 규칙을 쓰게 해, 한쪽만 바뀌어 기존 실행 폴더를 잘못 읽는 일을 막는다.
모델 추론·좌표 변환은 하지 않으며 기존 파일을 덮어쓰지 않는다.
디렉터리·파일 생성은 저장 함수가 호출될 때만 발생한다. 형식 정의 함수는 파일을 만들지 않는다.
"""

# 표준 라이브러리: Path는 경로와 배타적 파일 생성을, re는 페이지 결과 파일 이름 해석을 맡는다.
from pathlib import Path
import re

# 내부 공통 모듈: write_json은 타입·NaN 검사와 배타적 저장을, digest는 문서 ID용 SHA-256을 맡는다.
from common.files import digest, write_json

# 실행 폴더의 저장 형식 정의다. 실행 환경에 따라 달라지는 값이 아니며, 바꾸면 기존 실행 폴더를 읽지 못한다.
# PDF 뷰어처럼 첫 페이지를 1로 세는 프로젝트의 페이지 번호 기준이다.
PAGE_NUMBERING = "pdf_viewer_1_based"
DOCUMENT_DIRECTORY = "documents"
SOURCE_PDF_FILE = "source.pdf"
# 실행할 때 쓴 선정 목록을 실행 폴더에 그대로 남기는 파일이다.
SELECTION_FILE = "selection.json"
# 페이지 결과 파일 이름을 해석한다. 숫자 자릿수는 고정하지 않아 큰 페이지 번호도 읽는다.
PAGE_JSON_PATTERN = re.compile(r"page_(\d+)\.json")


def document_id(name: str) -> str:
    """문서명의 SHA-256이다. 파일명 문자와 길이에 영향받지 않는 폴더 이름으로 쓴다."""
    return digest(name.encode("utf-8"))


def page_identifier(doc_id: str, number: int) -> str:
    """프로젝트가 만드는 페이지 식별자다. 원본 데이터셋의 ID가 아니다."""
    return f"{doc_id}:p{number}"


def page_stem(number: int) -> str:
    """페이지 JSON·PNG·모델 부산물 폴더가 함께 쓰는 확장자 없는 이름이다. 정렬되도록 5자리로 채운다."""
    return f"page_{number:05d}"


def document_path(doc_id: str, filename: str = "") -> str:
    """실행 폴더 기준 상대 경로다. 기록 파일에 OS와 무관하게 / 구분자로 남긴다."""
    return f"{DOCUMENT_DIRECTORY}/{doc_id}/{filename}" if filename else f"{DOCUMENT_DIRECTORY}/{doc_id}"


def create_output_directory(output_dir: Path) -> None:
    """기존 실험과 섞이지 않도록 새 실행 폴더만 허용한다."""
    output_dir.mkdir(parents=True, exist_ok=False)


def save_json(path: Path, data: dict) -> None:
    """타입·NaN 검사 후 새 UTF-8 JSON 파일을 만든다. 파일을 읽기 쉽도록 필드 순서를 유지한다."""
    # 기록 도중 디스크 오류가 나면 부분 파일이 남을 수 있으며 예외를 상위에 전달한다.
    write_json(path, data, sort_keys=False)


def save_bytes(path: Path, data: bytes) -> None:
    """PDF·PNG bytes를 변형 없이 배타적으로 저장한다."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("저장할 데이터는 비어 있지 않은 bytes여야 합니다.")
    with path.open("xb") as output:
        output.write(data)


def save_bytes_under(base: Path, relative: str, data: bytes) -> Path:
    """모델이 만든 상대 경로의 파일을 base 안에만 저장하고 실제 경로를 반환한다."""
    target = (base / relative).resolve()
    # 참조 경로는 model_result.py에서도 검사하지만, 저장 위치는 이 모듈이 다시 확인한다.
    # symlink를 따라간 실제 경로가 base 밖이면 저장하지 않는다.
    if not target.is_relative_to(base.resolve()):
        raise ValueError(f"저장 경로가 결과 폴더를 벗어납니다: {relative!r}")
    target.parent.mkdir(parents=True, exist_ok=True)
    save_bytes(target, data)
    return target

