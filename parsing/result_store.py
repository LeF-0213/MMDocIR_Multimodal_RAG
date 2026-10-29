"""
run.py가 전달한 결과를 새 실행 폴더의 JSON·PDF·PNG 파일로 저장하는 모듈이다.
모델 추론·좌표 변환은 하지 않으며 기존 파일을 덮어쓰지 않는다.
디렉터리·파일 생성은 이 모듈의 함수가 호출될 때 발생한다.
"""

# 표준 라이브러리: json은 표준 JSON 직렬화, Path는 경로와 배타적 파일 생성을 맡는다.
import json
from pathlib import Path


def create_output_directory(output_dir: Path) -> None:
    """기존 실험과 섞이지 않도록 새 실행 폴더만 허용한다."""
    output_dir.mkdir(parents=True, exist_ok=False)


def save_json(path: Path, data: dict) -> None:
    """타입·NaN 검사 후 새 UTF-8 JSON 파일을 만든다."""
    # 파일을 열기 전에 직렬화해 형식 오류로 빈 결과 파일이 생기지 않게 한다.
    serialized = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    # x는 파일이 이미 있으면 실패한다. 실험 결과를 묵시적으로 덮어쓰지 않는다.
    # 기록 도중 디스크 오류가 나면 부분 파일이 남을 수 있으며 예외를 상위에 전달한다.
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(serialized + "\n")


def save_bytes(path: Path, data: bytes) -> None:
    """PDF·PNG bytes를 변형 없이 배타적으로 저장한다."""
    if not isinstance(data, bytes) or not data:
        raise ValueError("저장할 데이터는 비어 있지 않은 bytes여야 합니다.")
    with path.open("xb") as output:
        output.write(data)

