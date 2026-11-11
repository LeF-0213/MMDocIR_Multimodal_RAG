"""기록 객체의 민감 필드·URL 자격증명·환경 비밀 값을 가리고 비밀 의심 소스를 판별한다.

parsing과 experiments가 저장 직전에 호출하며, 입력 객체를 바꾸지 않고 새 객체를 반환한다.
비밀 값이 의심되는 소스는 호출부가 복사하지 않도록 판별 결과만 돌려준다.
파일을 만들지 않고 모델을 import하지 않는다.
"""

# 표준 라이브러리: ast는 Python 소스의 상수 비밀 대입, json은 JSON 소스 해석,
# os는 명시한 환경 비밀 값, re는 필드 이름을 검사한다.
import ast
import json
import os
import re
# pathlib는 경로 값 변환, urllib.parse는 URL의 자격증명·민감 질의값을 처리한다.
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
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
            original = match[0]
            try:
                parts = urlsplit(original)
            except ValueError:
                # OCR 결과처럼 형식이 깨진 URL은 구조를 믿을 수 없다. 자격증명 표식이 있으면
                # 통째로 가리고, 없으면 모델 출력 원문을 보존한다.
                names = re.findall(r"[?&]([^=&#]+)=", original)
                return REDACTED if "@" in original or any(secret_name(k) for k in names) else original
            pairs = parse_qsl(parts.query, keep_blank_values=True)
            # 가릴 값이 없으면 다시 조립하지 않는다. 조립하면 %20이 +로 바뀌는 등 표기가 달라진다.
            if "@" not in parts.netloc and not any(secret_name(k) for k, _ in pairs):
                return original
            # user:password와 민감 질의 필드만 제거하며 서버 주소 자체는 비교용으로 남긴다.
            host = parts.netloc.rsplit("@", 1)[-1]
            query = [(k, REDACTED if secret_name(k) else v) for k, v in pairs]
            return urlunsplit((parts.scheme, host, parts.path, urlencode(query), parts.fragment))
        return re.sub(r'''https?://[^\s"'<>\)]+''', redact_url, value)
    if value is None or type(value) in (int, float, bool):
        return value
    raise TypeError(f"기록할 수 없는 객체 타입: {type(value).__name__}")


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
