"""파싱 실행에 공통 기록과 자동 전후 비교를 연결하는 실행 모듈이다.

parsing은 실험 기록을 모르므로, 이 모듈이 CLI 옵션을 더해 기록기를 만들고 parsing.run에 넘긴다.
입력은 실행 인자와 선정 목록이고, 출력은 파싱 결과 + 실험 기록 + 비교 보고서다.
main.py가 호출하며 python -m experiments.parsing_stage로 인자를 직접 지정할 수도 있다.
실행 시 모델 캐시·새 결과 폴더·보고서 파일이 생긴다. --help는 모델 로딩·파일 생성을 하지 않는다.
"""

# 표준 라이브러리: argparse는 기록·비교 옵션 검증, json은 실행 요약 출력, Path는 경로 인자를 처리한다.
import argparse
import json
from pathlib import Path

# 내부 모듈: common은 해시·비밀 제거, parsing.pdf_source는 선정 목록 검증,
# parsing.run은 파싱 옵션과 실행 본체를 제공한다. 이 방향으로만 의존해 parsing을 독립시킨다.
from common.files import digest
from common.redaction import clean, known_secrets
from parsing.pdf_source import load_selection
from parsing.run import add_parsing_arguments, run_parsing, validate_parsing_arguments, validate_source

# automatic은 기준 실행 선택과 종료 후 비교를 맡는다. 기본 설정 경로도 이 모듈이 소유한다.
from . import automatic


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """파싱 옵션에 기록·비교 옵션을 더해 검증한다. 기록은 끄지 않는 한 기본 활성화다."""
    cli = argparse.ArgumentParser(description="PDF 파싱과 실행 기록·자동 전후 비교")
    add_parsing_arguments(cli)
    recording = cli.add_mutually_exclusive_group()
    recording.add_argument("--experiment-config", type=Path,
                           help="기본 활성화된 공통 기록의 JSON 설정 교체. 프로젝트 경로는 설정 파일 기준")
    recording.add_argument("--no-experiment-record", action="store_true", help="공통 소스·모델·메모리 기록 끄기")
    comparison = cli.add_mutually_exclusive_group()
    comparison.add_argument("--compare-baseline", type=Path, help="자동 선택 대신 비교할 기준 실행 폴더")
    comparison.add_argument("--no-auto-compare", action="store_true", help="파싱 종료 후 자동 비교 끄기")
    cli.add_argument("--comparison-output-dir", type=Path, help="자동 보고서용 새 폴더. 기본은 실행 폴더 옆 <실행명>_comparison")
    cli.add_argument("--comparison-gold", type=Path, help="자동 비교에 사용할 확인된 파싱 정답 JSON")
    cli.add_argument("--comparison-evaluation-config", type=Path, default=automatic.DEFAULT_EVALUATION_CONFIG,
                     help="자동 정답 평가 설정 JSON. 기본은 프로젝트 configs의 평가 설정")

    args = cli.parse_args(argv)
    if not args.no_experiment_record and args.experiment_config is None:
        args.experiment_config = automatic.DEFAULT_RECORDING_CONFIG
    if args.no_auto_compare and (args.comparison_gold or args.comparison_output_dir):
        cli.error("--no-auto-compare와 자동 비교의 정답·출력 옵션을 함께 사용할 수 없습니다.")
    validate_parsing_arguments(cli, args)
    return args


def main(argv: list[str] | None = None, *, launch_context: dict | None = None) -> int:
    """기록기·비교 계획을 준비해 파싱을 실행하고, 실패가 있으면 종료 코드 1을 반환한다."""
    args = arguments(argv)
    selection = load_selection(args.selection)
    recording = None
    if args.experiment_config:
        # 지연 import로 --help는 추가 기록 라이브러리(psutil)조차 로딩하지 않는다.
        from .record import load_record_config
        recording = load_record_config(args.experiment_config)
    secrets = known_secrets(recording["secret_environment_names"]) if recording else ()

    # 잘못된 입력은 출력 생성·모델 로딩 전에 검출한다.
    validate_source(args, selection)
    # 기준을 지금 고정한다. 추론 중 새로 생긴 실행으로 비교 대상이 바뀌지 않는다.
    comparison_plan = automatic.prepare_comparison(args, selection)

    extra_metadata = {
        "automatic_comparison": comparison_plan,
        "experiment_recording": {"enabled": recording is not None,
                                 "config_path": str(args.experiment_config.resolve()) if recording else None},
    }
    if launch_context is not None:
        extra_metadata["launch_config"] = launch_context
    if recording:
        extra_metadata["experiment_config_sha256"] = digest(args.experiment_config.read_bytes())

    recorder = None

    def start_recorder(root, metadata):
        """run.json을 저장한 뒤 parsing.run이 호출한다. 시작 기록에 같은 실행 조건을 남긴다."""
        nonlocal recorder
        from .record import ExperimentRecorder
        recorder = ExperimentRecorder(root, recording, metadata)
        return recorder

    try:
        outcome = run_parsing(
            args,
            selection,
            pipeline_overrides=recording["pipeline_options"] if recording else None,
            predict_overrides=recording["predict_options"] if recording else None,
            extra_metadata=extra_metadata,
            start_recorder=start_recorder if recording else None,
            secrets=secrets,
        )
    except BaseException as exc:
        # run_parsing은 추론 오류를 결과로 돌려주므로, 기록기가 있는 채 여기 오는 것은 저장 실패다.
        if recorder:
            recorder.finish("failed", {"type": type(exc).__name__, "message": clean(str(exc), secrets)})
        raise

    summary, interrupted = outcome["summary"], outcome["interrupted"]
    if recorder:
        recorder.finish("interrupted" if interrupted else summary["status"], summary["fatal_error"])
    # 자원 기록을 먼저 닫아 보고서 생성 시간을 파싱 비용으로 세지 않는다.
    comparison_result = automatic.finish_comparison(comparison_plan, outcome["root"],
                                                    interrupted=interrupted is not None, secrets=secrets)
    if interrupted:
        raise interrupted
    print(
        json.dumps(
            {
                "output_dir": str(outcome["root"]),
                "status": summary["status"],
                "page_status_counts": summary["page_status_counts"],
                "fatal_error": summary["fatal_error"],
                "automatic_comparison": comparison_result,
            },
            ensure_ascii=False,
        )
    )

    return 0 if outcome["success"] and comparison_result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
