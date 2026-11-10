"""임베딩 모델의 토크나이저로 청크 길이를 재고, 너무 긴 문장을 토큰 경계에서 나누는 모듈이다.

전체 위치: 청킹 안에서 structure.py가 청크 크기를 판단할 때 호출한다.
입력: 설정의 모델 저장소 ID·revision·파일 이름. 출력: 토큰 수, 토큰 경계로 자른 문자열, 출처 기록.
모델 가중치는 받지 않는다. 처음 실행하면 토크나이저·설정 JSON을 Hugging Face 캐시에 내려받는다.

설계 이유
- 단어 수·문자 수는 실제 입력 길이와 다르다. 예를 들어 "Revenue increased 12%"는 3단어지만
  Qwen3-Embedding-8B 토크나이저로는 숫자가 한 자리씩 나뉘어 6토큰이다. 청크 크기와 잘림은
  실제로 임베딩할 모델의 규칙으로 재야 한다.
- 문서 입력에는 지시문이 없고 끝 토큰이 붙는다(config_sentence_transformers.json의 document 프롬프트가
  빈 문자열). 그래서 add_special_tokens 설정을 따라 끝 토큰까지 센다.
- 모델 위치 한도는 내려받은 config.json의 max_position_embeddings에서 읽는다. 코드에 숫자를 적지 않는다.
- 토크나이저를 바꾸면 모든 청크의 토큰 수를 다시 세야 하므로 저장소 ID·revision·파일 해시를 기록한다.
"""

# 표준 라이브러리 json은 내려받은 설정 파일 읽기, Path는 캐시 파일 경로 처리를 맡는다.
import json
from pathlib import Path

# 내부 공통 모듈: file_hash는 실제로 읽은 토크나이저 파일의 SHA-256을 남긴다.
from common.files import file_hash


class TokenCounter:
    """고정 revision의 토크나이저로 토큰 수를 센다. 같은 문자열은 다시 인코딩하지 않는다."""

    def __init__(self, settings: dict):
        # huggingface_hub(Hugging Face 공식 클라이언트)의 hf_hub_download는 revision을 고정해
        # 파일 하나만 내려받고 로컬 캐시 경로를 돌려준다. 가중치 같은 다른 파일은 받지 않는다.
        from huggingface_hub import hf_hub_download
        # tokenizers(Hugging Face의 Rust 토크나이저)는 tokenizer.json만으로 인코딩한다.
        # transformers 없이도 같은 규칙으로 토큰과 원문 위치(offsets)를 얻을 수 있어 선택했다.
        from tokenizers import Tokenizer

        self.settings = settings
        repo, revision = settings["repo_id"], settings["revision"]
        tokenizer_path = Path(hf_hub_download(repo, settings["tokenizer_file"], revision=revision))
        config_path = Path(hf_hub_download(repo, settings["model_config_file"], revision=revision))
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        model_config = json.loads(config_path.read_text(encoding="utf-8"))
        self.model_position_limit = model_config.get("max_position_embeddings")
        self.add_special_tokens = settings["add_special_tokens"]
        self.special_token_count = len(self.tokenizer.encode("", add_special_tokens=True).ids)
        self._cache: dict[str, int] = {}
        self.record = {
            "repo_id": repo, "revision": revision,
            "tokenizer_file": settings["tokenizer_file"], "tokenizer_sha256": file_hash(tokenizer_path),
            "model_config_file": settings["model_config_file"], "model_config_sha256": file_hash(config_path),
            "model_position_limit": self.model_position_limit,
            "add_special_tokens": self.add_special_tokens,
            "special_tokens_per_input": self.special_token_count if self.add_special_tokens else 0,
            "weights_downloaded": False,
        }

    def count(self, text: str) -> int:
        """임베딩 입력으로 넣을 때의 토큰 수다. 설정에 따라 끝 토큰을 포함한다."""
        if text not in self._cache:
            self._cache[text] = len(self.tokenizer.encode(text, add_special_tokens=self.add_special_tokens).ids)
        return self._cache[text]

    def split(self, text: str, limit: int) -> list[str]:
        """문장 하나가 limit보다 길 때 토큰 경계에서 자른다. 자른 조각은 원문의 연속 부분 문자열이다."""
        budget = max(1, limit - (self.special_token_count if self.add_special_tokens else 0))
        encoding = self.tokenizer.encode(text, add_special_tokens=False)
        offsets = encoding.offsets
        pieces = []
        for start in range(0, len(offsets), budget):
            window = offsets[start:start + budget]
            piece = text[window[0][0]:window[-1][1]].strip()
            if piece:
                pieces.append(piece)
        return pieces or [text]
