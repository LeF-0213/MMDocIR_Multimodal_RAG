"""Qdrant 연결, 컬렉션 이름, 포인트 ID 규칙을 한 곳에 둔다.

전체 위치: 임베딩(outputs/embedding_*) → 인덱싱〔현재〕 → 검색
입력: 인덱싱 설정의 qdrant 항목, 임베딩 실행 폴더 이름, 레코드 ID(청크·페이지 ID).
출력: QdrantClient, 컬렉션 이름(임베딩 실행 폴더 이름과 같음), UUID 포인트 ID.
컬렉션 이름을 임베딩 실행 폴더 이름과 같게 두어, 검색 결과가 어느 벡터 파일에서 왔는지 이름만으로 추적한다.
"""

# 표준 라이브러리: os는 서버 API 키를 환경 변수에서 읽고, uuid는 문자열 ID를 Qdrant가 받는 UUID로 결정적으로 바꾼다.
import os
import uuid

# qdrant-client: Qdrant 공식 Python 클라이언트. path를 주면 서버 없이 같은 프로세스에서 도는 로컬 모드,
# url을 주면 서버에 연결한다. 로컬 모드는 HNSW를 만들지 않고 항상 전수 비교한다(공식 문서의 로컬 모드 설명).
from qdrant_client import QdrantClient

# 포인트 ID 네임스페이스. uuid5는 (네임스페이스, 이름)이 같으면 같은 UUID를 낸다.
# NAMESPACE_URL은 표준 정의값이고, 이름에 레코드 ID를 넣어 같은 청크가 늘 같은 포인트 ID를 받게 한다.
POINT_ID_NAMESPACE = uuid.NAMESPACE_URL
POINT_ID_PREFIX = "mmdocir-project:"
STORE_FIELDS = frozenset({"path", "url", "api_key_env"})


def connect(settings, base):
    """path와 url 중 정확히 하나만 받는다. 상대 path는 설정 파일 폴더 기준이다."""
    if set(settings) != STORE_FIELDS or (settings["path"] is None) == (settings["url"] is None):
        raise ValueError("qdrant 설정에는 path와 url 중 하나만 값을 넣으세요.")
    if settings["path"] is not None:
        location = (base / settings["path"]).resolve()
        return QdrantClient(path=str(location)), {"mode": "local", "path": str(location)}
    key =os.environ.get(settings["api_key_env"]) if settings["api_key_env"] else None
    return (QdrantClient(url=settings["url"], api_key=key),
            {"mode": "server", "url": settings["url"], "api_key_used": bool(key)})


def collection_name(embedding_run):
    return embedding_run.name


def point_id(record_id):
    return str(uuid.uuid5(POINT_ID_NAMESPACE, POINT_ID_PREFIX + record_id))


def record_id(record):
    """텍스트 채널은 청크 ID, 이미지 채널은 페이지 ID가 레코드 ID다."""
    return record.get("chunk_id") or record["page_id"]
