"""단계 실행의 시간·RSS·선택적 Paddle CUDA 메모리를 관측한다.

record가 전달한 측정 설정과 출력 파일을 받아 JSONL 샘플과 구간 요약을 만든다.
한 모니터 스레드가 여러 중첩 구간을 함께 관측하므로 구간마다 스레드를 늘리지 않는다.
측정 실패는 추론 예외와 분리하며 실제 GPU 전체 사용량·순간 최고점을 보장하지 않는다.
"""

# 표준 라이브러리: contextmanager는 구간 수명, json은 점진적 기록,
# os는 자기 PID, sys는 이미 로딩된 Paddle 조회, threading은 한 개의 측정 스레드다.
from contextlib import contextmanager
import json
import os
import sys
import threading
# perf_counter는 경과 시간, uuid4는 충돌하지 않는 구간 ID를 생성한다.
from time import perf_counter
from uuid import uuid4

# psutil은 OS 프로세스 RSS와 자식 프로세스를 읽는다. 모델 네이티브 메모리도 RSS에 반영된다.
# 설치·모델 로딩은 하지 않으며 기록 모듈을 사용할 때만 import한다.
import psutil

# 내부 모듈: clean은 구간 설명과 오류에 명시한 비밀 값이 섞이지 않게 한다.
from .common import clean


class ResourceMonitor:
    """설정으로 받은 간격·프로세스 범위에 따라 샘플을 저장하고 구간별로 집계한다."""

    def __init__(self, path, interval_seconds, include_children, gpu_device, secrets=()):
        if (isinstance(interval_seconds, bool) or not isinstance(interval_seconds, (int, float))
                or not 0 < interval_seconds < float("inf")):
            raise ValueError("측정 간격은 유한한 양수 초여야 합니다.")
        if type(include_children) is not bool:
            raise ValueError("자식 프로세스 포함 여부는 bool이어야 합니다.")
        self.interval = interval_seconds
        self.children = include_children
        self.gpu_device = gpu_device
        self.secrets = secrets
        self.stream = path.open("x", encoding="utf-8")
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.active = {}
        self.completed = []
        self.errors = []
        self.started = perf_counter()
        # 전체 실행 구간을 항상 남겨 짧은 실행이나 명시적 하위 구간이 없는 실행도 관측한다.
        self.active["entire_run"] = {"scope_id": "entire_run", "name": "entire_run", "details": {},
            "sample_count": 0, "unavailable_samples": 0, "processes": {}, "status": "completed"}
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        self.sample()

    def _gpu(self):
        if self.gpu_device is None:
            return {"status": "not_requested"}
        paddle = sys.modules.get("paddle")
        if paddle is None:
            return {"status": "unavailable", "reason": "Paddle 로딩 전"}
        try:
            # Paddle 3.3.1의 API는 장치 ID·gpu 문자열을 받는다. 장치 전체 메모리가 아니다.
            return {"status": "ok", "device": self.gpu_device,
                    "scope": "paddle_allocator",
                    "allocated_bytes": paddle.device.cuda.memory_allocated(self.gpu_device),
                    "reserved_bytes": paddle.device.cuda.memory_reserved(self.gpu_device)}
        except Exception as exc:
            return {"status": "unavailable", "reason": clean(str(exc), self.secrets)}

    def sample(self):
        with self.lock:
            sample = {"elapsed_seconds": perf_counter() - self.started,
                      "active_scopes": list(self.active), "processes": [], "gpu": self._gpu()}
            try:
                parent = psutil.Process(os.getpid())
                processes = [parent]
                if self.children:
                    try:
                        processes += parent.children(recursive=True)
                    except Exception as exc:
                        sample["children_error"] = type(exc).__name__
                for process in processes:
                    try:
                        sample["processes"].append({"pid": process.pid,
                            "created": process.create_time(), "rss_bytes": process.memory_info().rss})
                    except Exception as exc:
                        sample["processes"].append({"pid": process.pid, "error": type(exc).__name__})
            except Exception as exc:
                sample["error"] = type(exc).__name__
            try:
                self.stream.write(json.dumps(clean(sample, self.secrets), ensure_ascii=False,
                                             allow_nan=False) + "\n")
                self.stream.flush()
            except OSError as exc:
                reason = clean(str(exc), self.secrets)
                if reason not in self.errors:
                    self.errors.append(reason)
            for scope in self.active.values():
                scope["sample_count"] += 1
                for p in sample["processes"]:
                    if "rss_bytes" not in p:
                        scope["unavailable_samples"] += 1
                        continue
                    key = f"{p['pid']}:{p['created']}"
                    stats = scope["processes"].setdefault(key, {
                        "pid": p["pid"], "created": p["created"],
                        "first_rss_bytes": p["rss_bytes"], "sample_count": 0,
                        "observed_max_rss_bytes": p["rss_bytes"]})
                    stats["last_rss_bytes"] = p["rss_bytes"]
                    stats["observed_max_rss_bytes"] = max(stats["observed_max_rss_bytes"], p["rss_bytes"])
                    stats["sample_count"] += 1
                if sample.get("error") or sample.get("children_error"):
                    scope["unavailable_samples"] += 1
                scope["last_gpu"] = sample["gpu"]
                if sample["gpu"]["status"] == "ok":
                    scope["observed_max_gpu_allocated_bytes"] = max(
                        scope.get("observed_max_gpu_allocated_bytes", 0), sample["gpu"]["allocated_bytes"])
            return sample

    def _loop(self):
        while not self.stop_event.wait(self.interval):
            try:
                self.sample()
            except Exception as exc:
                # 관측 스레드의 예외를 모델 실행에 전파하지 않고 누락 이유를 남긴다.
                reason = clean(f"{type(exc).__name__}: {exc}", self.secrets)
                if reason not in self.errors:
                    self.errors.append(reason)

    @contextmanager
    def measure(self, name, **details):
        scope_id = str(uuid4())
        start = perf_counter()
        scope = {"scope_id": scope_id, "name": name, "details": clean(details, self.secrets),
                 "sample_count": 0, "unavailable_samples": 0, "processes": {}, "status": "completed"}
        with self.lock:
            self.active[scope_id] = scope
        self.sample()
        try:
            yield scope
        except BaseException:
            scope["status"] = "failed"
            raise
        finally:
            self.sample()
            with self.lock:
                scope["elapsed_seconds"] = perf_counter() - start
                self.active.pop(scope_id)
                self.completed.append(scope)

    def close(self, status="completed"):
        self.stop_event.set()
        self.thread.join()
        self.sample()
        overall = self.active.pop("entire_run")
        overall.update(elapsed_seconds=perf_counter() - self.started, status=status)
        self.completed.append(overall)
        self.stream.close()
        main_processes = [p for p in overall["processes"].values() if p["pid"] == os.getpid()]
        return {"interval_seconds": self.interval, "include_children": self.children,
                "peak_kind": "sampled_maximum", "gpu_device": self.gpu_device,
                "scopes": self.completed, "collection_errors": self.errors,
                "rss_sum_is_unique_memory": False, "gpu_synchronized": False,
                "main_observed_peak_rss_bytes": max((p["observed_max_rss_bytes"] for p in main_processes), default=None)}
