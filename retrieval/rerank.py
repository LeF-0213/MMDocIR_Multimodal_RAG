"""Qwen3-VL-Reranker를 로컬 CPU에서 실행해 (질의, 페이지 이미지) 쌍의 관련성 점수를 낸다.

전체 위치: 결합 후보 상위 k → 리랭킹〔현재〕 → 최종 순위
입력: 질문 문자열, PIL 페이지 이미지 목록. 출력: 후보마다 실수 점수(로짓 차이, 클수록 관련). 확률이 아니다.
리랭커는 질의와 후보를 함께 읽으므로 후보 수만큼 모델을 실행한다. 후보 밖의 페이지는 새로 찾지 못한다.
실행 부작용: 처음 실행 때 가중치를 HF 캐시에 내려받는다. fp32 2B 모델에서 약 13GB 메모리를 관측했다.
"""

# 표준 라이브러리: time은 쌍마다 걸린 시간을 잰다.
import time


class QwenVLReranker:
    def __init__(self, settings):
        # torch: 스레드·dtype. sentence_transformers.CrossEncoder: 모델 카드가 안내하는 로딩과 점수 계산(yes/no 로짓).
        import torch
        from sentence_transformers import CrossEncoder

        self.settings = settings
        torch.set_num_threads(settings["cpu_threads"])
        started = time.perf_counter()
        self.model = CrossEncoder(settings["repo_id"], revision=settings["revision"], device=settings["device"],
                                  model_kwargs={"dtype": getattr(torch, settings["dtype"])})
        self.load_seconds = time.perf_counter() - started
        self.pair_seconds = []

    def score(self, question, images):
        """한 쌍씩 실행해 최대 메모리를 낮추고 쌍마다 시간을 남긴다."""
        scores = []
        for image in images:
            started = time.perf_counter()
            scores.append(float(self.model.predict([(question, image)], prompt=self.settings["instruction"])[0]))
            self.pair_seconds.append(time.perf_counter() - started)
        return scores

    def record(self):
        return {"repo_id": self.settings["repo_id"], "revision": self.settings["revision"],
                "device": self.settings["device"], "dtype": self.settings["dtype"],
                "cpu_threads": self.settings["cpu_threads"], "instruction": self.settings["instruction"],
                "load_seconds": self.load_seconds, "pairs": len(self.pair_seconds),
                "pair_seconds_total": sum(self.pair_seconds)}
