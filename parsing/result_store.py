"""
run.py가 전달한 결과를 새 실행 폴더의 JSON·PDF·PNG 파일로 저장하는 모듈이다.
모델 추론·좌표 변환은 하지 않으며 기존 파일을 덮어쓰지 않는다.
디렉터리·파일 생성은 이 모듈의 함수가 호출될 때 발생한다.
"""

# 표준 라이브러리: Path는 경로와 배타적 파일 생성을 맡는다.
from pathlib import Path

# 내부 공통 모듈: write_json은 타입·NaN 검사와 배타적 저장을 맡는다.
from common.files import write_json


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

