"""중단된 파싱 실행에 재개 세그먼트 기록과 자동 전후 비교를 연결하는 실행 모듈이다.

전체 위치: main.py --resume 또는 python -m experiments.resume_stage
  → parsing.resume(설정 복원·호환성 검사·재개 세션) → parsing.run.run_parsing(resume=세션)
  → 종료 기록 → 자동 비교
입력: 중단된 실행 폴더. 원본 위치·비교 기준은 선택해서 바꿀 수 있다.
출력: 같은 실행 폴더의 resume/segment_<번호>/ 기록, summary.json, experiment_finished.json,
      선택적 비교 보고서(<실행명>_comparison/).

기록 설정은 현재 configs 파일이 아니라 중단 전 experiment.json에 남은 설정을 그대로 쓴다.
설정이 바뀌어 pipeline·predict 옵션이 달라지는 것을 막기 위해서다. 중단 전 실행이 기록을 껐다면
재개도 기록 없이 진행한다. --check는 호환성·재사용 예정만 출력하고 파일을 만들지 않는다.
실제 실행은 원본 읽기·모델 로딩/캐시·실행 폴더 안 파일 생성과 잔여 파일 이동을 일으킨다.
"""

# 표준 라이브러리: argparse는 CLI 검증, json은 결과 출력, Path는 경로 처리를 맡는다.
import argparse
import json
from pathlib import Path

# 내부 공통 모듈: read_json은 엄격한 JSON 읽기, clean·known_secrets는 출력 전 민감정보 제거다.
from common.files import read_json
from common.redaction import clean, known_secrets

# 내부 파싱 모듈: resume는 설정 복원·호환성 검사·재개 세션을, run은 실제 처리(run_parsing)와
# 원본 위치 검사를 맡는다. 의존 방향은 experiments → parsing이며 parsing은 이 모듈을 모른다.
from parsing.resume import (FIRST_SEGMENT, RESUME_DIRECTORY, SEGMENT_PREFIX, ResumeError, ResumeSession,
                            check_compatibility, plan_resume, restore_arguments)
from parsing.run import run_parsing, validate_source

# 내부 실험 모듈: automatic은 비교 기준 고정과 종료 후 보고서, schema는 기록 파일 이름,
# record는 세그먼트 기록기를 제공한다(psutil 로딩을 늦추려고 실행 시점에 가져온다).
from . import automatic
from .schema import FINISHED_FILE, MANIFEST_FILE, MODELS_FILE


def arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """재개 대상·원본 위치 교체·비교 옵션을 검증한다. 파서·DPI 같은 실행 설정은 받지 않는다."""
    cli = argparse.ArgumentParser(description="중단된 파싱 실행을 이어서 처리")
    cli.add_argument("--run-dir", type=Path, required=True, help="중단된 실행 폴더")
    source = cli.add_mutually_exclusive_group()
    source.add_argument("--pdf-directory", type=Path, help="원본 PDF 로컬 폴더. 생략하면 기록된 위치")
    source.add_argument("--base-url", help="원본 PDF HTTP(S) 폴더 URL. 생략하면 기록된 위치")
    cli.add_argument("--check", action="store_true", help="호환성·재사용 예정만 출력. 파일·모델 로딩 없음")
    comparison = cli.add_mutually_exclusive_group()
    comparison.add_argument("--compare-baseline", type=Path, help="자동 선택 대신 비교할 기준 실행 폴더")
    comparison.add_argument("--no-auto-compare", action="store_true", help="종료 후 자동 비교 끄기")
    cli.add_argument("--comparison-output-dir", type=Path, help="자동 보고서용 새 폴더")
    cli.add_argument("--comparison-gold", type=Path, help="자동 비교에 사용할 확인된 파싱 정답 JSON")
    cli.add_argument("--comparison-evaluation-config", type=Path, default=automatic.DEFAULT_EVALUATION_CONFIG,
                     help="자동 정답 평가 설정 JSON")
    args = cli.parse_args(argv)
    if args.no_auto_compare and (args.comparison_gold or args.comparison_output_dir):
        cli.error("--no-auto-compare와 자동 비교의 정답·출력 옵션을 함께 사용할 수 없습니다.")
    return args


def previous_models(root: Path) -> dict | None:
    """가장 최근 세그먼트의 역할별 모델 파일 목록 해시를 찾는다. 기록이 없으면 None이다."""
    candidates = [(FIRST_SEGMENT, root / MODELS_FILE)]
    base = root / RESUME_DIRECTORY
    if base.is_dir():
        for path in base.glob(f"{SEGMENT_PREFIX}*/{MODELS_FILE}"):
            suffix = path.parent.name.removeprefix(SEGMENT_PREFIX)
            if suffix.isdigit():
                candidates.append((int(suffix), path))
    existing = sorted((number, path) for number, path in candidates if path.is_file())
    if not existing:
        return None
    record = read_json(existing[-1][1])
    return {item["role"]: item.get("inventory_sha256") for item in record["models"]["models"]}


def main(argv: list[str] | None = None) -> int:
    """재개 조건을 검사하고 남은 페이지를 처리한다. 실패가 있으면 1, 실행 오류는 2를 반환한다."""
    options = arguments(argv)
    root = options.run_dir.resolve()
    try:
        args, metadata, selection = restore_arguments(root, pdf_directory=options.pdf_directory,
                                                      base_url=options.base_url)
        # 비교 옵션은 parsing_stage와 같은 이름으로 붙여 automatic 모듈을 그대로 재사용한다.
        for name in ("compare_baseline", "no_auto_compare", "comparison_output_dir",
                     "comparison_gold", "comparison_evaluation_config"):
            setattr(args, name, getattr(options, name))
        validate_source(args, selection)

        manifest = read_json(root / MANIFEST_FILE) if (root / MANIFEST_FILE).is_file() else None
        recording = manifest["recording_config"] if manifest else None
        if recording and not Path(recording["project_root"]).is_dir():
            raise ResumeError("기록 설정의 프로젝트 폴더가 없습니다. 중단 전과 같은 위치에서 재개하세요.")
        secrets = known_secrets(recording["secret_environment_names"]) if recording else ()
        pipeline = recording["pipeline_options"] if recording else None
        predict = recording["predict_options"] if recording else None
        compatibility = check_compatibility(args, metadata, pipeline_overrides=pipeline, predict_overrides=predict)

        if options.check:
            plan = automatic.prepare_comparison(args, selection)
            print(json.dumps(clean({"status": "resumable", "run_dir": str(root), "compatibility": compatibility,
                                    "recording": "previous_config" if recording else "disabled",
                                    "documents": plan_resume(args, selection, root),
                                    "automatic_comparison": plan}, secrets), ensure_ascii=False, indent=2))
            return 0

        comparison_plan = automatic.prepare_comparison(args, selection)
        expected = previous_models(root) if recording else None
        session = ResumeSession(root, metadata, compatibility)
        # 이전 시도의 종료 기록·자동 비교 상태 파일은 실험 기록 쪽 파일이라 여기서 이름을 넘긴다.
        session.start(root_files_to_set_aside=(FINISHED_FILE, automatic.AUTO_COMPARISON_FILE), secrets=secrets)
        recorder = None

        def start_recorder(run_root, requested):
            """세그먼트 폴더에 시작 기록을 남기는 기록기를 만든다. 실행 ID는 중단 전 값을 잇는다."""
            nonlocal recorder
            from .record import ExperimentRecorder
            recorder = ExperimentRecorder(run_root, recording, requested, record_dir=session.directory,
                                          run_id=manifest["run_id"], expected_models=expected)
            return recorder
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"재개 실패: {clean(str(exc))}", flush=True)
        return 2

    try:
        outcome = run_parsing(args, selection, pipeline_overrides=pipeline, predict_overrides=predict,
                              start_recorder=start_recorder if recording else None, secrets=secrets,
                              resume=session)
    except BaseException as exc:
        # run_parsing은 추론 오류를 결과로 돌려주므로, 여기 오는 것은 저장 실패다. 기록기를 닫는다.
        if recorder:
            recorder.finish("failed", {"type": type(exc).__name__, "message": clean(str(exc), secrets)})
        raise
    summary, interrupted = outcome["summary"], outcome["interrupted"]
    if recorder:
        recorder.finish("interrupted" if interrupted else summary["status"], summary["fatal_error"])
    comparison_result = automatic.finish_comparison(comparison_plan, outcome["root"],
                                                    interrupted=interrupted is not None, secrets=secrets)
    if interrupted:
        raise interrupted
    print(json.dumps({"output_dir": str(outcome["root"]), "segment": outcome["segment"],
                      "status": summary["status"], "resume": summary["resume"],
                      "page_status_counts": summary["page_status_counts"],
                      "fatal_error": summary["fatal_error"], "automatic_comparison": comparison_result},
                     ensure_ascii=False))
    return 0 if outcome["success"] and comparison_result["status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
