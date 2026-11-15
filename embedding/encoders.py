"""임베딩 모델 어댑터. 원격 텍스트 모델과 로컬 멀티모달 모델을 같은 형태의 출력으로 감싼다.

전체 위치: 청킹(chunks.jsonl)·파싱 페이지 이미지 → 임베딩〔현재〕 → 인덱싱
- OpenRouterTextEncoder: OpenRouter의 OpenAI 호환 `/embeddings` API로 청크 텍스트와 질의를 인코딩한다.
  API 키는 설정에 적힌 환경 변수 이름으로만 읽고, 값은 기록에 남기지 않는다.
- QwenVLEncoder: Qwen3-VL-Embedding을 Sentence Transformers로 로컬 CPU에서 실행한다.
  페이지 이미지와 텍스트 질의가 같은 표현 공간에 놓인다(모델 카드 주장. 이 프로젝트에서 검색 품질로 확인할 대상).
출력: 행마다 L2 정규화한 float32 numpy 배열. 정규화 전 노름은 호출부가 기록할 수 있게 함께 돌려준다.
실행 부작용: 원격 인코더는 네트워크 호출과 과금이 생긴다. 로컬 인코더는 처음 실행 때 가중치를 HF 캐시에 내려받는다.
"""

# 표준 라이브러리: json·urllib는 추가 의존성 없이 HTTP 요청을 보내고, os는 환경 변수에서 키를 읽는다.
# time은 재시도 대기와 호출 시간 측정에 쓴다.
import json
import os
import time
import urllib.error
import urllib.request

# NumPy: 벡터 배열·정규화. 파싱 단계부터 이미 쓰는 의존성이다.
import numpy as np

# OpenRouter 응답이 이 HTTP 상태면 일시적 실패로 보고 재시도한다. 429는 호출 한도, 5xx는 서버·공급자 오류다.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
# 0 벡터는 방향이 없어 코사인 유사도를 정의할 수 없다. 이보다 작은 노름은 실패로 처리한다.
MIN_VECTOR_NORM = 1e-12


def normalize_rows(vectors):
    """행마다 L2 정규화한다. 정규화 뒤 내적이 코사인 유사도가 된다."""
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1)
    if vectors.ndim != 2 or (norms < MIN_VECTOR_NORM).any():
        raise ValueError("임베딩이 2차원 배열이 아니거나 노름이 0인 행이 있습니다.")
    return vectors / norms[:, None], norms


class OpenRouterTextEncoder:
    """OpenRouter `/embeddings`로 텍스트를 인코딩한다. 문서와 질의의 입력 형식만 다르다."""

    def __init__(self, settings):
        self.settings = settings
        key = os.environ.get(settings["api_key_env"], "")
        if not key.strip():
            raise ValueError(f"환경 변수 {settings['api_key_env']}가 비어 있습니다. 키를 셸 프로필에 설정하세요.")
        self._key = key
        self.calls = []

    def _post(self, texts):
        body = json.dumps({"model": self.settings["model_id"], "input": texts,
                           "encoding_format": "float"}).encode("utf-8")
        request = urllib.request.Request(
            self.settings["api_base"].rstrip("/") + "/embeddings", data=body, method="POST",
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"})
        for attempt in range(self.settings["max_retries"] + 1):
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.settings["timeout_seconds"]) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS or attempt == self.settings["max_retries"]:
                    # 응답 본문에는 키가 없지만, 요청 헤더는 출력하지 않는다.
                    raise RuntimeError(f"OpenRouter HTTP {exc.code}: {exc.read()[:500]!r}") from None
            except urllib.error.URLError as exc:
                if attempt == self.settings["max_retries"]:
                    raise RuntimeError(f"OpenRouter 연결 실패: {exc.reason}") from None
            time.sleep(self.settings["retry_backoff_seconds"] * (attempt + 1))
        data = sorted(payload["data"], key=lambda item: item["index"])
        if len(data) != len(texts):
            raise RuntimeError(f"요청 {len(texts)}개에 응답 {len(data)}개가 왔습니다.")
        # 실제 응답 모델·공급자·토큰 사용량을 남긴다. 공급자가 바뀌면 같은 모델 ID라도 결과가 다를 수 있다.
        self.calls.append({"inputs": len(texts), "seconds": time.perf_counter() - started,
                           "model": payload.get("model"), "provider": payload.get("provider"),
                           "usage": payload.get("usage"), "retries": attempt})
        return [item["embedding"] for item in data]

    def _encode(self, texts):
        rows = []
        for start in range(0, len(texts), self.settings["batch_size"]):
            rows += self._post(texts[start:start + self.settings["batch_size"]])
        return normalize_rows(rows)

    def encode_documents(self, texts):
        """문서는 지시문 없이 원문 그대로 넣는다(Qwen3-Embedding 모델 카드의 사용법)."""
        return self._encode(list(texts))

    def encode_queries(self, queries):
        """질의는 `Instruct: <과제>\\nQuery:<질문>` 형식으로 넣는다(Qwen3-Embedding 모델 카드의 형식)."""
        template = "Instruct: {instruction}\nQuery:{query}"
        return self._encode([template.format(instruction=self.settings["query_instruction"], query=q)
                             for q in queries])

    def record(self):
        """실행 기록용 요약. 키 값은 넣지 않고 사용한 환경 변수 이름만 남긴다."""
        usage = [call["usage"] or {} for call in self.calls]
        return {"provider": "openrouter", "model_id": self.settings["model_id"],
                "api_base": self.settings["api_base"], "api_key_env": self.settings["api_key_env"],
                "api_key_used": True, "calls": len(self.calls),
                "response_models": sorted({str(c["model"]) for c in self.calls}),
                "response_providers": sorted({str(c["provider"]) for c in self.calls}),
                "prompt_tokens": sum(u.get("prompt_tokens", 0) for u in usage),
                "cost": sum(u.get("cost", 0) or 0 for u in usage),
                "retries": sum(c["retries"] for c in self.calls),
                "request_seconds": sum(c["seconds"] for c in self.calls)}


class QwenVLEncoder:
    """Qwen3-VL-Embedding을 로컬에서 실행한다. 페이지 이미지는 한 장씩 넣어 최대 메모리를 낮춘다."""

    def __init__(self, settings):
        # PyTorch·Sentence Transformers는 로딩이 무겁고 텍스트 채널에는 필요 없으므로 여기서 import한다.
        # torch: CPU 스레드 수와 가중치 dtype 지정. sentence_transformers: 모델 카드가 안내하는 로딩·풀링·정규화 구성.
        import torch
        from sentence_transformers import SentenceTransformer

        self.settings = settings
        torch.set_num_threads(settings["cpu_threads"])
        started = time.perf_counter()
        self.model = SentenceTransformer(settings["repo_id"], revision=settings["revision"],
                                         device=settings["device"],
                                         model_kwargs={"dtype": getattr(torch, settings["dtype"])})
        self.load_seconds = time.perf_counter() - started
        self.item_seconds = []

    def encode_images(self, images):
        """PIL 이미지 목록을 문서 지시문과 함께 인코딩한다. 이미지마다 걸린 시간을 남긴다."""
        rows = []
        for image in images:
            started = time.perf_counter()
            rows.append(self.model.encode([image], prompt=self.settings["document_prompt"])[0])
            self.item_seconds.append(time.perf_counter() - started)
        return normalize_rows(rows)

    def encode_queries(self, queries):
        """텍스트 질의를 질의 지시문과 함께 인코딩한다."""
        return normalize_rows(self.model.encode(list(queries), prompt=self.settings["query_prompt"]))

    def record(self):
        # Sentence Transformers의 첫 모듈이 Hugging Face 프로세서를 들고 있다. transformers 5의 Qwen2VLImageProcessor는
        # 최대 픽셀 수를 size.longest_edge에 둔다(이름과 달리 변 길이가 아니라 픽셀 수). 패치 16px·병합 2라 32×32px가 시각 토큰 하나다.
        image_processor = getattr(getattr(self.model[0], "processor", None), "image_processor", None)
        size = getattr(image_processor, "size", None)
        return {"provider": "local", "repo_id": self.settings["repo_id"], "revision": self.settings["revision"],
                "device": self.settings["device"], "dtype": self.settings["dtype"],
                "cpu_threads": self.settings["cpu_threads"],
                "document_prompt": self.settings["document_prompt"], "query_prompt": self.settings["query_prompt"],
                "dimension": self.model.get_embedding_dimension(),
                # 이미지 전처리가 넘지 않도록 줄이는 최대 픽셀 수다. 실제 입력 크기는 레코드마다 따로 남긴다.
                "image_max_pixels": getattr(size, "longest_edge", None),
                "image_patch_size": getattr(image_processor, "patch_size", None),
                "image_merge_size": getattr(image_processor, "merge_size", None),
                "load_seconds": self.load_seconds, "image_seconds_total": sum(self.item_seconds)}
