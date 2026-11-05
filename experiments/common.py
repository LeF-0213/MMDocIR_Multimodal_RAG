"""공통 기록의 엄격한 JSON 입출력·해시·경로 검사·민감정보 처리를 담당한다.

record와 compare가 전달한 경로·객체를 처리하고 모델은 import하지 않는다.
저장 함수만 새 파일을 만들며 기존 파일은 덮어쓰지 않는다.
비밀 값이 의심되는 소스는 복사하지 않고, 기록 객체의 민감 필드는 가린다.
"""

# 표준 라이브러리: ast는 Python 소스의 상수 비밀 대입, hashlib는 파일 동일성,
# json은 엄격한 직렬화, os는 명시한 환경 비밀 값, re는 필드 이름을 검사한다.
import ast
import hashlib
import json
import os
import re
# pathlib는 경로 경계, urllib.parse는 URL의 자격증명·민감 질의값을 처리한다.
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 아래 이름은 실행 환경이 아닌 공통 파일 형식의 정의다.
MANIFEST_FILE = "experiment.json"
FINISHED_FILE = "experiment_finished.json"
RESOURCES_FILE = "resources.jsonl"
REDACTED = "[REDACTED]"
RECORD_SCHEMA = "project.experiment.v1"
COMPARE_SCHEMA = "project.experiment_comparison.v1"
GOLD_SCHEMA = "project.parsing_gold.v1"
# 1 MiB 단위 읽기는 큰 가중치 전체를 RAM에 올리지 않으려는 입출력 기본값이다.
HASH_BUFFER_BYTES = 1024 * 1024
# 실제 비밀 값이 아니라 가려야 하는 필드 이름의 정의다.
SECRET_NAMES = frozenset({"password", "passwd", "token", "api_key", "access_token",
                          "secret", "authorization", "cookie", "access_key", "api_token"})


def secret_name(name):
    """토큰 수·토크나이저 설정을 비밀 값으로 오판하지 않도록 필드 단위로 검사한다."""
    name = re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")
    return name in SECRET_NAMES or any(name.endswith("_" + k) for k in SECRET_NAMES)


def known_secrets(environment_names=()):
    """명시한 환경 변수의 값만 메모리에서 사용하고 이름·값을 자동 출력하지 않는다."""
    return tuple(os.environ[name] for name in environment_names if os.environ.get(name))


def diagnostic_clean(value, secrets=()):
    """모델 원본 내용은 보존하고 error/errors 진단 필드만 가린다."""
    if isinstance(value, dict):
        return {key: clean(item, secrets) if key in ("error", "errors")
                else diagnostic_clean(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [diagnostic_clean(item, secrets) for item in value]
    return value


def clean(value, secrets=()):
    """민감 필드·URL 자격증명·명시한 환경 비밀을 가린 JSON 기본 객체를 반환한다."""
    if isinstance(value, dict):
        return {str(k): REDACTED if secret_name(k) and v not in (None, "")
                else clean(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v, secrets) for v in value]
    if isinstance(value, Path):
        return clean(str(value), secrets)
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, REDACTED)
        def redact_url(match):
            parts = urlsplit(match[0])
            # user:password와 민감 질의 필드만 제거하며 서버 주소 자체는 비교용으로 남긴다.
            host = parts.netloc.rsplit("@", 1)[-1]
            query = [(k, REDACTED if secret_name(k) else v)
                     for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            return urlunsplit((parts.scheme, host, parts.path, urlencode(query), parts.fragment))
        return re.sub(r'''https?://[^\s"'<>\)]+''', redact_url, value)
    if value is None or type(value) in (int, float, bool):
        return value
    raise TypeError(f"기록할 수 없는 객체 타입: {type(value).__name__}")


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


def json_bytes(value):
    """객체 키 순서와 무관한 해시·보고서를 위해 정렬된 표준 JSON bytes를 만든다."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")


def write_json(path, value):
    """직렬화 성공 뒤 배타적으로 저장한다. 디스크 실패는 호출부가 기록한다."""
    data = json_bytes(value)
    with Path(path).open("xb") as stream:
        stream.write(data)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_hash(path):
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


def source_is_sensitive(data, suffix, secrets=()):
    """확인 가능한 비밀 상수·필드를 발견하면 원문 소스의 보관을 보류한다."""
    text = data.decode("utf-8")
    if any(secret in text for secret in secrets):
        return True
    for url in re.findall(r"https?://[^\s\"')]+", text):
        if clean(url, secrets) != url:
            return True
    if suffix == ".json":
        value = json.loads(text)
        return clean(value, secrets) != value
    if suffix == ".py":
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if (any(isinstance(t, ast.Name) and secret_name(t.id) for t in targets)
                        and isinstance(node.value, ast.Constant)
                        and isinstance(node.value.value, str) and node.value.value):
                    return True
            if isinstance(node, ast.Dict):
                for key, item in zip(node.keys, node.values):
                    if (isinstance(key, ast.Constant) and secret_name(key.value)
                            and isinstance(item, ast.Constant) and item.value not in (None, "")):
                        return True
    # .env·인증 파일은 수집 allowlist 밖에 두며 일반 텍스트의 명백한 비밀 대입도 보류한다.
    for match in re.finditer(r'''(?im)^\s*["']?([a-z_]\w*)["']?\s*[:=]\s*(.+)$''', text):
        if secret_name(match[1]) and match[2].strip().rstrip(",") not in ("null", "None", '""', "''"):
            return True
    return False
