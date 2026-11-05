"""단계 공통 실행 manifest·소스·모델 파일·관찰 설정을 기록한다.

CLI가 읽은 기록 설정과 실행 폴더를 받아 시작/종료 manifest를 배타적으로 생성한다.
모델 초기화 이후 capture_parser(), 페이지 저장 시 artifact()를 호출한다.
가중치는 다운로드하지 않고 실제 객체의 설정된 경로에서 파일 해시만 읽는다.
정답·평가 레이블은 이 모듈의 모델 입력이나 문서 표현으로 전달하지 않는다.
"""

# 표준 라이브러리: contextlib는 기록을 끈 호출의 빈 구간, datetime은 UTC 시각,
# importlib는 패키지 버전, subprocess는 shell 없는 Git 조회, uuid는 실행 식별자다.
from contextlib import nullcontext
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
import subprocess
from time import perf_counter
from uuid import uuid4
# hashlib는 HF ETag 대조, inspect는 실제 구현 파일, re는 commit 형식을 확인한다.
# os·platform·sys는 모델을 import하지 않고 CPU·OS·Python 환경을 기록한다.
# pathlib는 소스·가중치 경로, perf_counter는 수집 비용을 처리한다.
import hashlib
import inspect
import re
import os
import platform
import sys
from pathlib import Path

# 내부 모듈: common은 보존·비밀 처리, resources는 프로세스·단계별 자원 관측을 맡는다.
from .common import (FINISHED_FILE, MANIFEST_FILE, RECORD_SCHEMA, RESOURCES_FILE,
                     HASH_BUFFER_BYTES, clean, digest, file_hash, inside, json_bytes, known_secrets,
                     read_json, source_is_sensitive, write_json)
from .resources import ResourceMonitor

# 공식 Git 출력 형식과 manifest 파일명은 환경 설정이 아닌 직렬화 정의다.
SOURCE_DIRECTORY = "source_snapshot"
MODEL_HASH_KIND = "sha256_sorted_file_inventory"
# Hugging Face local_dir 다운로드 메타데이터의 패키지 정의 경로다. 실행 모델 경로와 구분한다.
HF_METADATA_DIRECTORY = Path(".cache") / "huggingface" / "download"


def _file_revision(directory, path, checksum):
    """로컬 HF 메타데이터의 ETag를 실제 bytes와 대조한다. 네트워크·캐시 쓰기는 없다."""
    metadata = directory / HF_METADATA_DIRECTORY / (path.relative_to(directory).as_posix() + ".metadata")
    if not metadata.is_file():
        return {"status": "unavailable"}
    try:
        lines = metadata.read_text(encoding="utf-8").splitlines()
        commit, etag = lines[:2]
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("전체 commit 형식 불일치")
        if len(etag) == 64:
            matches = etag == checksum
        elif len(etag) == 40:
            # Git blob SHA-1은 내용 앞에 종류·바이트 길이·NUL을 넣는다.
            blob = hashlib.sha1(b"blob " + str(path.stat().st_size).encode() + b"\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(HASH_BUFFER_BYTES), b""):
                    blob.update(chunk)
            matches = blob.hexdigest() == etag
        else:
            matches = False
        return {"status": "content_matched" if matches else "content_mismatch",
                "commit": commit, "evidence": metadata.relative_to(directory).as_posix()}
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "unavailable", "reason": type(exc).__name__}


def load_record_config(path):
    """상대 경로는 설정 파일 위치를 기준으로 해석한다. 로딩·다운로드 부작용은 없다."""
    path = Path(path).resolve()
    config = read_json(path)
    required = {"schema_version", "project_root", "source_paths", "source_suffixes",
                "git_timeout_seconds", "memory", "experiment", "model_bindings",
                "secret_environment_names", "environment_packages", "pipeline_options", "predict_options"}
    if not isinstance(config, dict) or set(config) != required:
        raise ValueError(f"기록 설정의 필수 필드를 확인하세요: {sorted(required)}")
    if config["schema_version"] != RECORD_SCHEMA:
        raise ValueError("지원하지 않는 기록 설정 스키마입니다.")
    for key in ("source_paths", "source_suffixes", "model_bindings", "secret_environment_names", "environment_packages"):
        if not isinstance(config[key], list):
            raise ValueError(f"{key}는 목록이어야 합니다.")
    for key in ("pipeline_options", "predict_options", "experiment", "memory"):
        if not isinstance(config[key], dict):
            raise ValueError(f"{key}는 객체여야 합니다.")
    memory = config["memory"]
    if set(memory) != {"interval_seconds", "include_children", "gpu_device"}:
        raise ValueError("memory에는 간격·자식 프로세스·GPU 장치 설정이 필요합니다.")
    if (type(memory["include_children"]) is not bool
            or isinstance(memory["interval_seconds"], bool)
            or not isinstance(memory["interval_seconds"], (int, float))
            or not 0 < memory["interval_seconds"] < float("inf")):
        raise ValueError("메모리 간격과 자식 프로세스 설정이 잘못됐습니다.")
    timeout = config["git_timeout_seconds"]
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout < float("inf"):
        raise ValueError("Git 조회 제한 시간은 유한한 양수 초여야 합니다.")
    if (not config["source_paths"] or not config["source_suffixes"]
            or any(not isinstance(x, str) or not x for x in config["source_paths"] + config["source_suffixes"]
                   + config["secret_environment_names"] + config["environment_packages"])):
        raise ValueError("소스 목록·확장자·환경 변수 이름을 확인하세요.")
    root = (path.parent / config["project_root"]).resolve()
    if not root.is_dir():
        raise ValueError("기록할 프로젝트 루트가 없습니다.")
    for relative in config["source_paths"]:
        inside(root, relative)
    for binding in config["model_bindings"]:
        if (not isinstance(binding, dict) or not isinstance(binding.get("role"), str)
                or not binding["role"] or not isinstance(binding.get("object_path"), list)
                or any(not isinstance(x, str) or not x for x in binding["object_path"])):
            raise ValueError("모델 binding에는 역할과 객체 속성 경로 목록이 필요합니다.")
        if any(not isinstance(binding.get(key), str) or not binding[key] for key in ("name_attribute", "directory_attribute")):
            raise ValueError("모델 binding에 이름·폴더 속성 이름을 지정해야 합니다.")
    config["project_root"] = str(root)
    # 설정 파일 내용도 소스 복사본과 연결하되 비밀 값은 실제 저장 시 가린다.
    return config


def git_record(root, timeout, secrets, source_paths):
    """Git이 없어도 기록을 중단하지 않는다. 토큰을 읽는 인증 명령은 실행하지 않는다."""
    result = {}
    commands = {"commit": ["rev-parse", "HEAD"], "branch": ["branch", "--show-current"],
                "status": ["status", "--porcelain=v1"],
                "staged_diff": ["diff", "--cached", "--no-ext-diff", "--no-textconv"],
                "unstaged_diff": ["diff", "--no-ext-diff", "--no-textconv"],
                "untracked": ["ls-files", "--others", "--exclude-standard"]}
    for name, args in commands.items():
        try:
            limited = [*args, "--", *source_paths] if name not in ("commit", "branch") else args
            output = subprocess.run(["git", "-C", str(root), *limited], capture_output=True,
                                    check=True, timeout=timeout).stdout.decode("utf-8")
            if name.endswith("diff"):
                # diff의 +/- 표식을 제외한 비밀 대입이 있으면 원문 diff 전체를 보류한다.
                inspected = "\n".join(line.lstrip(" +-") for line in output.splitlines())
                if source_is_sensitive(inspected.encode(), ".txt", secrets):
                    result[name] = {"status": "withheld", "reason": "비밀 대입이 의심되는 diff"}
                    continue
            result[name] = {"status": "ok", "text": clean(output.rstrip(), secrets)}
        except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
            result[name] = {"status": "unavailable", "reason": type(exc).__name__}
    return result


def _attribute(value, path):
    """모델 객체 경로는 설정에서 받는다. 없는 경로를 캐시 탐색으로 대신하지 않는다."""
    for part in path:
        value = value[part] if isinstance(value, dict) else getattr(value, part)
    return value


def model_inventory(parser, bindings, secrets=()):
    """로딩 객체와 연결된 폴더를 해시한다. 요청 revision과 검증된 revision을 혼동하지 않는다."""
    result = []
    for binding in bindings:
        started = perf_counter()
        item = {"role": binding["role"], "object_path": binding["object_path"],
                "requested_source": clean(binding.get("source"), secrets),
                "requested_revision": clean(binding.get("revision"), secrets),
                "verified_revision": None, "revision_status": "unverified",
                "usage_scope": "loaded_object_not_per_page_call", "files": []}
        try:
            obj = _attribute(parser, binding["object_path"])
            # 속성 이름은 설정으로 지정하며 모델 이름에서 폴더를 추정하지 않는다.
            name_attr = binding["name_attribute"]
            dir_attr = binding["directory_attribute"]
            item["name"] = clean(getattr(obj, name_attr, None), secrets)
            directory = Path(getattr(obj, dir_attr)).resolve()
            if not directory.is_dir():
                raise ValueError("실제 모델 폴더가 없습니다.")
            item["directory"] = clean(str(directory), secrets)
            try:
                implementation = inspect.getsourcefile(type(obj))
                if implementation and Path(implementation).is_file():
                    item["implementation"] = {"path": clean(implementation, secrets), "sha256": file_hash(implementation)}
            except (OSError, TypeError):
                item["implementation"] = None
            for path in sorted(directory.rglob("*")):
                if (not path.is_file() or ".cache" in path.relative_to(directory).parts
                        or path.relative_to(directory).as_posix() == binding.get("provenance_file")):
                    continue
                before = path.stat()
                checksum = file_hash(path)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError("모델 파일이 해시 계산 중 변경됐습니다.")
                item["files"].append({"path": path.relative_to(directory).as_posix(),
                                      "size": after.st_size, "sha256": checksum,
                                      "revision": _file_revision(directory, path, checksum)})
            if not item["files"]:
                raise ValueError("모델 폴더에서 파일을 확보하지 못했습니다.")
            inventory = [{k: f[k] for k in ("path", "size", "sha256")} for f in item["files"]]
            item["inventory_sha256"] = digest(json_bytes(inventory))
            item["hash_kind"] = MODEL_HASH_KIND
            item["status"] = "ok"
            verified = [f["revision"] for f in item["files"] if f["revision"]["status"] == "content_matched"]
            commits = {f["commit"] for f in verified}
            if len(verified) == len(item["files"]) and len(commits) == 1:
                item["verified_revision"] = next(iter(commits))
                item["revision_status"] = "local_hf_metadata_content_matched"
            # 검증 근거 파일은 해당 모델 폴더 안에서 지정한 경우에만 읽는다.
            provenance = binding.get("provenance_file")
            if provenance:
                source = read_json(inside(directory, provenance))
                # provenance가 실제 모델 bytes와 같은 목록을 주장하는지 확인한다.
                if source.get("inventory_sha256") == item["inventory_sha256"]:
                    item["declared_provenance"] = clean(source, secrets)
                    # 로컬 선언의 일치만으로 공식 서버의 commit을 검증했다고 표시하지 않는다.
                    item["provenance_status"] = "local_declaration_inventory_matched"
                else:
                    item["provenance_error"] = "모델 파일 목록과 출처 기록의 해시 불일치"
        except (AttributeError, KeyError, TypeError, OSError, ValueError) as exc:
            item["status"] = "unavailable"
            item["reason"] = clean(f"{type(exc).__name__}: {exc}", secrets)
        item["collection_seconds"] = perf_counter() - started
        result.append(item)
    return {"status": "collected" if bindings else "not_configured",
            "coverage": "configured_object_paths_only", "models": result}


class ExperimentRecorder:
    """단계 공통 기록 API다. 후속 청킹·검색은 stage 이름과 결과물 종류만 달리 연결한다."""

    def __init__(self, root, config, requested, stage="parsing"):
        self.root = Path(root).resolve()
        self.config = config
        self.project = Path(config["project_root"]).resolve()
        self.secrets = known_secrets(config["secret_environment_names"])
        self.started = perf_counter()
        self.artifacts = []
        self.inputs = []
        self.resolved = {"status": "not_collected"}
        self.models = {"status": "not_collected", "models": []}
        self.run_id = str(uuid4())
        self.stage_id = str(uuid4())
        self.monitor = ResourceMonitor(self.root / RESOURCES_FILE,
                                       **config["memory"], secrets=self.secrets)
        try:
            self.manifest = {"schema_version": RECORD_SCHEMA, "run_id": self.run_id,
                "stage_run_id": self.stage_id, "stage": stage, "status": "started",
                "started_at_utc": datetime.now(timezone.utc).isoformat(),
                "experiment": clean(config["experiment"], self.secrets),
                "recording_config": clean(config, self.secrets), "requested": clean(requested, self.secrets),
                "environment": self._environment(),
                "sources": self._snapshot(),
                "git": git_record(self.project, config["git_timeout_seconds"], self.secrets, config["source_paths"])}
            write_json(self.root / MANIFEST_FILE, self.manifest)
        except BaseException:
            self.monitor.close("failed")
            raise

    def _environment(self):
        """지정한 패키지 버전과 OS·Python 정보를 읽는다. 모델이나 인증 환경은 import하지 않는다."""
        packages = {}
        for name in self.config["environment_packages"]:
            try:
                packages[name] = version(name)
            except PackageNotFoundError:
                packages[name] = None
        return clean({"python": sys.version, "executable": sys.executable,
                      "platform": platform.platform(), "machine": platform.machine(),
                      "visible_logical_cpu_count": os.cpu_count(),
                      "cpu_quota": None, "packages": packages}, self.secrets)

    def _snapshot(self):
        started = perf_counter()
        files = {}
        for relative in self.config["source_paths"]:
            source = inside(self.project, relative)
            if not source.exists():
                files[str(relative)] = {"status": "missing"}
                continue
            for path in sorted(source.rglob("*") if source.is_dir() else [source]):
                if not path.is_file() or path.suffix not in self.config["source_suffixes"]:
                    continue
                resolved = path.resolve()
                if not resolved.is_relative_to(self.project) or resolved.is_relative_to(self.root):
                    continue
                name = path.relative_to(self.project).as_posix()
                if name in files:
                    continue
                try:
                    data = path.read_bytes()
                    item = {"sha256": digest(data), "size": len(data)}
                    if source_is_sensitive(data, path.suffix, self.secrets):
                        item.update(status="withheld", reason="비밀 값이 의심되는 소스")
                    else:
                        destination = self.root / SOURCE_DIRECTORY / name
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        with destination.open("xb") as stream:
                            stream.write(data)
                        item.update(status="ok", snapshot=destination.relative_to(self.root).as_posix())
                    files[name] = item
                except (OSError, UnicodeError, ValueError, SyntaxError) as exc:
                    files[name] = {"status": "unavailable", "reason": type(exc).__name__}
        return {"files": files, "collection_seconds": perf_counter() - started}

    def capture_parser(self, parser):
        """설치된 래퍼의 해석 설정과 명시한 객체 경로의 모델 파일을 수집한다."""
        with self.measure("record_model_provenance"):
            try:
                value = getattr(parser, "_merged_paddlex_config")
                self.resolved = {"status": "ok", "collector": "PaddleOCR._merged_paddlex_config",
                                 "value": clean(value, self.secrets)}
            except (AttributeError, TypeError) as exc:
                self.resolved = {"status": "unavailable", "reason": type(exc).__name__}
            try:
                implementation = inspect.getsourcefile(type(parser))
                self.resolved["implementation_sha256"] = file_hash(implementation) if implementation else None
            except (OSError, TypeError):
                self.resolved["implementation_sha256"] = None
            bindings = [b for b in self.config["model_bindings"]
                        if b.get("parser") in (None, self.manifest["requested"].get("parser"))]
            self.models = model_inventory(parser, bindings, self.secrets)

    def measure(self, name, **details):
        return self.monitor.measure(name, **details)

    def artifact(self, path, kind, parents=(), **identity):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("결과물은 실행 출력 폴더 안에 있어야 합니다.")
        registered = {item["artifact_id"] for item in self.artifacts + self.inputs}
        if any(not isinstance(parent, str) or parent not in registered for parent in parents):
            raise ValueError("부모 결과물은 먼저 등록하거나 재사용 출처로 연결해야 합니다.")
        artifact_id = str(uuid4())
        self.artifacts.append({"artifact_id": artifact_id, "kind": kind,
            "path": path.relative_to(self.root).as_posix(), "sha256": file_hash(path),
            "parents": list(parents), "identity": clean(identity, self.secrets),
            "stage_run_id": self.stage_id})
        return artifact_id

    def reuse(self, path, *, origin_run_id, origin_stage_id, origin_artifact_id, expected_sha256):
        """이전 단계 파일을 수정·복사하지 않고 해시를 대조해 입력 참조로 등록한다."""
        if not all(isinstance(value, str) and value for value in
                   (origin_run_id, origin_stage_id, origin_artifact_id, expected_sha256)):
            raise ValueError("재사용에는 원본 실행·단계·결과물 ID와 해시가 필요합니다.")
        path = Path(path).resolve()
        actual = file_hash(path)
        if actual != expected_sha256:
            raise ValueError("재사용할 입력 파일의 해시가 다릅니다.")
        artifact_id = str(uuid4())
        self.inputs.append({"artifact_id": artifact_id, "status": "reused", "path": clean(str(path), self.secrets),
            "sha256": actual, "origin_run_id": origin_run_id, "origin_stage_id": origin_stage_id,
            "origin_artifact_id": origin_artifact_id})
        return artifact_id

    def finish(self, status, error=None):
        resources = self.monitor.close(status)
        changed = []
        for name, item in self.manifest["sources"]["files"].items():
            if item.get("sha256"):
                try:
                    if file_hash(inside(self.project, name)) != item["sha256"]:
                        changed.append(name)
                except (OSError, ValueError):
                    changed.append(name)
        result = {"schema_version": RECORD_SCHEMA, "run_id": self.run_id,
            "stage_run_id": self.stage_id, "status": status,
            "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": perf_counter() - self.started, "resolved": self.resolved,
            "models": self.models, "resources": resources, "artifacts": self.artifacts,
            "input_artifacts": self.inputs,
            "source_changes_during_run": changed, "error": clean(error, self.secrets)}
        write_json(self.root / FINISHED_FILE, result)
        return result


def measure(recorder, name, **details):
    """기록을 요청하지 않은 기존 실행도 같은 호출부를 유지하게 한다."""
    return recorder.measure(name, **details) if recorder else nullcontext()
