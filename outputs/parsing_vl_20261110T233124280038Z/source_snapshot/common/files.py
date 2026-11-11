"""엄격한 JSON 입출력·SHA-256 해시·입력 경로 경계 검사를 담당한다.

parsing과 experiments가 같은 규칙으로 파일을 읽고 쓰도록 한 곳에 모은다.
입력은 호출부가 전달한 경로·객체이고, 출력은 Python 객체·bytes·해시 문자열이다.
저장 함수만 새 파일을 만들며 기존 파일은 덮어쓰지 않는다. 모델은 import하지 않는다.
"""

# 표준 라이브러리: hashlib는 파일 동일성, json은 엄격한 직렬화, pathlib는 경로 경계를 처리한다.
import hashlib
import json
from pathlib import Path

# 1 MiB 단위 읽기는 큰 가중치 전체를 RAM에 올리지 않으려는 입출력 기본값이다.
HASH_BUFFER_BYTES = 1024 * 1024


def read_json(path):
    """NaN·Infinity와 중복 키를 거부해 모호한 설정·보고서를 읽지 않는다."""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"중복 JSON 키: {key}")
            result[key] = value
        return result

    def reject(value):
        raise ValueError(f"표준 JSON 숫자가 아닙니다: {value}")

    return json.loads(Path(path).read_text(encoding="utf-8"),
                      object_pairs_hook=pairs, parse_constant=reject)


def json_bytes(value, *, sort_keys=True):
    """표준 JSON bytes를 만든다. 기본 정렬은 객체 키 순서와 무관한 해시·보고서를 위한 것이다."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=sort_keys, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def write_json(path, value, *, sort_keys=True):
    """직렬화 성공 뒤 배타적으로 저장한다. 디스크 실패는 호출부가 기록한다."""
    # 파일을 열기 전에 직렬화해 형식 오류로 빈 결과 파일이 생기지 않게 한다.
    data = json_bytes(value, sort_keys=sort_keys)
    # x는 파일이 이미 있으면 실패한다. 실험 결과를 묵시적으로 덮어쓰지 않는다.
    with Path(path).open("xb") as stream:
        stream.write(data)


def digest(data):
    """bytes의 SHA-256이다. 원본·이미지·소스의 바이트 단위 동일성을 비교한다."""
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
    """큰 파일도 한 번에 메모리에 올리지 않도록 나누어 SHA-256을 계산한다."""
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(HASH_BUFFER_BYTES), b""):
            result.update(chunk)
    return result.hexdigest()


def inside(root, relative):
    """입력 기록의 상대 경로가 상위 폴더·외부 symlink로 빠져나가는 것을 거부한다."""
    root = Path(root).resolve()
    relative = Path(relative)
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root):
        raise ValueError("기록 경로는 입력 폴더 안의 상대 경로여야 합니다.")
    return target
