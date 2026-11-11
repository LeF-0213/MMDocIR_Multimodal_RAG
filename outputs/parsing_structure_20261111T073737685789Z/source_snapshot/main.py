"""설정 파일 하나로 PDF 파싱·실행 기록·자동 전후 비교를 시작한다.

기본 입력은 configs/parsing_run.json이며 상대 경로는 그 파일의 폴더 기준이다.
설정을 실행 인자로 변환해 experiments.parsing_stage에 넘기고 처리·저장 로직을 재사용한다.
실제 실행은 PDF 읽기, 모델 다운로드/캐시, 새 실행 폴더와 비교 보고서를 만들 수 있다.
--check는 입력·설정·비교 계획만 읽어 검증하며 모델 로딩·파일 생성은 하지 않는다.
--resume <실행 폴더>는 중단된 실행을 experiments.resume_stage로 넘겨 남은 페이지만 처리한다.
"""

# 표준 라이브러리: argparse는 설정 선택·조회 옵션, json은 확인 결과 출력,
# pathlib는 현재 작업 폴더와 독립적인 설정 경로를 처리한다.
import argparse
import json
from pathlib import Path

# 내부 공통 모듈은 엄격한 JSON·해시·비밀 제거를 담당한다. automatic은 기존 기본 경로와
# 비교 계획을 공유한다. parsing_stage는 CLI 검증과 기록을 포함한 전체 실행을 맡고,
# pdf_source·run은 선정 목록 검증·원본 확인·출력 경로 계산을 제공한다.
from common.files import file_hash, read_json
from common.redaction import clean, known_secrets
from experiments.automatic import CONFIG_DIRECTORY, prepare_comparison
from experiments.parsing_stage import arguments, main as run_parsing
# resume_stage는 중단된 실행의 기록으로 설정을 복원해 남은 페이지만 처리한다.
from experiments.resume_stage import main as run_resume
from parsing.pdf_source import load_selection
from parsing.run import new_output_directory, validate_source

# 설정 파일 위치의 유일한 기본값이다. --config로 교체할 수 있으며 경로·해시를 실행 기록에 남긴다.
DEFAULT_RUN_CONFIG = CONFIG_DIRECTORY / "parsing_run.json"
# 아래 스키마·필드 목록은 실행 값이 아니라 지원하는 설정 형식의 정의다.
RUN_CONFIG_SCHEMA = "project.parsing_run.v1"
RUN_FIELDS = frozenset({"schema_version", "selection", "input", "output_root", "parser", "device",
                        "cpu_threads", "dpi", "http_timeout_seconds", "formula_model", "all_pages",
                        "recording", "comparison"})
SOURCE_FIELDS = frozenset({"kind", "pdf_directory", "base_url"})
RECORDING_FIELDS = frozenset({"enabled", "config"})
COMPARISON_FIELDS = frozenset({"enabled", "baseline", "output_directory", "gold", "evaluation_config"})


def configured_arguments(path: Path):
    """명시한 값만 기존 CLI에 전달한다. 설정 오타·잘못된 타입을 실행 전에 거부한다."""
    path = Path(path).resolve()
    checksum = file_hash(path)
    config = read_json(path)
    if file_hash(path) != checksum:
        raise ValueError("읽는 동안 실행 설정 파일이 바뀌었습니다.")
    if not isinstance(config, dict) or set(config) != RUN_FIELDS or config.get("schema_version") != RUN_CONFIG_SCHEMA:
        raise ValueError("파싱 실행 설정의 스키마·필수 필드를 확인하세요.")
    for key, fields in (("input", SOURCE_FIELDS), ("recording", RECORDING_FIELDS), ("comparison", COMPARISON_FIELDS)):
        if not isinstance(config[key], dict) or set(config[key]) != fields:
            raise ValueError(f"{key} 설정 필드를 확인하세요: {sorted(fields)}")
    for value in (config["all_pages"], config["recording"]["enabled"], config["comparison"]["enabled"]):
        if type(value) is not bool:
            raise ValueError("all_pages와 enabled는 JSON true/false여야 합니다.")
    for key in ("cpu_threads", "dpi"):
        if type(config[key]) is not int:
            raise ValueError(f"{key}는 정수여야 합니다.")
    if type(config["http_timeout_seconds"]) not in (int, float):
        raise ValueError("http_timeout_seconds는 숫자여야 합니다.")
    for key in ("parser", "device"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise ValueError(f"{key}는 비어 있지 않은 문자열이어야 합니다.")
    if config["formula_model"] is not None and (not isinstance(config["formula_model"], str) or not config["formula_model"].strip()):
        raise ValueError("formula_model은 모델 ID 문자열 또는 null이어야 합니다.")

    def resolve(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("필수 파일·폴더 경로는 비어 있지 않은 문자열이어야 합니다.")
        return (path.parent / value).resolve()

    selection_path = resolve(config["selection"])
    output = new_output_directory(resolve(config["output_root"]), config["parser"])
    invocation = ["--selection", str(selection_path), "--output-dir", str(output)]
    for key, flag in (("parser", "--parser"), ("device", "--device"), ("cpu_threads", "--cpu-threads"),
                      ("dpi", "--dpi"), ("http_timeout_seconds", "--timeout")):
        invocation.extend([flag, str(config[key])])
    source = config["input"]
    if source["kind"] == "local":
        if source["base_url"] is not None:
            raise ValueError("local 입력의 base_url은 null이어야 합니다.")
        invocation.extend(["--pdf-directory", str(resolve(source["pdf_directory"]))])
    elif source["kind"] == "http":
        if source["pdf_directory"] is not None or not isinstance(source["base_url"], str) or not source["base_url"].strip():
            raise ValueError("http 입력은 pdf_directory=null과 base_url 문자열이 필요합니다.")
        invocation.extend(["--base-url", source["base_url"]])
    else:
        raise ValueError("input.kind는 local 또는 http여야 합니다.")
    if config["all_pages"]:
        invocation.append("--all-pages")
    if config["formula_model"] is not None:
        invocation.extend(["--formula-model", config["formula_model"]])
    recording = config["recording"]
    if recording["enabled"]:
        invocation.extend(["--experiment-config", str(resolve(recording["config"]))])
    else:
        invocation.append("--no-experiment-record")
    comparison = config["comparison"]
    if not comparison["enabled"]:
        if any(comparison[key] is not None for key in ("baseline", "output_directory", "gold")):
            raise ValueError("비교를 끄면 baseline·output_directory·gold는 null이어야 합니다.")
        invocation.append("--no-auto-compare")
    else:
        for key, flag in (("baseline", "--compare-baseline"), ("output_directory", "--comparison-output-dir"),
                          ("gold", "--comparison-gold"), ("evaluation_config", "--comparison-evaluation-config")):
            if comparison[key] is not None:
                invocation.extend([flag, str(resolve(comparison[key]))])
    return invocation, {"schema_version": RUN_CONFIG_SCHEMA, "path": str(path), "sha256": checksum,
                        "config": config, "relative_path_base": str(path.parent)}


def main(argv=None):
    """설정 기반 실행과 읽기 전용 확인을 구분하고 기존 파싱 종료 코드를 보존한다."""
    cli = argparse.ArgumentParser(description="설정 파일로 파싱·실행 기록·자동 비교 실행")
    cli.add_argument("--config", type=Path, default=DEFAULT_RUN_CONFIG, help="파싱 실행 JSON 설정 교체")
    cli.add_argument("--check", action="store_true", help="모델 로딩·파일 생성 없이 설정과 입력 경로 확인")
    cli.add_argument("--resume", type=Path, help="중단된 실행 폴더를 이어서 처리. 설정은 그 실행의 run.json에서 복원")
    options = cli.parse_args(argv)
    if options.resume is not None:
        # 재개는 현재 설정 파일의 파서·DPI가 아니라 중단된 실행의 기록을 따른다.
        # 원본 위치·비교 기준을 바꾸려면 python -m experiments.resume_stage를 직접 실행한다.
        return run_resume(["--run-dir", str(options.resume)] + (["--check"] if options.check else []))
    try:
        invocation, context = configured_arguments(options.config)
        if options.check:
            args = arguments(invocation)
            selected = load_selection(args.selection)
            validate_source(args, selected)
            secrets = ()
            if args.experiment_config:
                # 기록 설정 검증은 psutil만 로딩하며 OCR 모델·관측 스레드를 시작하지 않는다.
                from experiments.record import load_record_config
                recording = load_record_config(args.experiment_config)
                secrets = known_secrets(recording["secret_environment_names"])
            plan = prepare_comparison(args, selected)
            print(json.dumps(clean({"status": "configuration_valid", "launch_config": context,
                "document_count": len(selected["documents"]),
                "selected_page_count": sum(len(item["page_numbers"]) for item in selected["documents"]),
                "all_pages": args.all_pages, "output_directory": str(args.output_dir),
                "automatic_comparison": plan}, secrets), ensure_ascii=False, indent=2))
            return 0
        return run_parsing(invocation, launch_context=context)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        cli.exit(2, f"파싱 실행 실패: {clean(str(exc))}\n")


if __name__ == "__main__":
    raise SystemExit(main())
