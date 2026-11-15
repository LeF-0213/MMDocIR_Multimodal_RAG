"""저장된 파싱 실행에서 규칙 검사 임계값(parsing/quality_checks.py의 LIMITS)에 쓰는 측정값의 분포를 잰다.

전체 위치: PDF → 파싱(outputs/parsing_*) → 규칙 검사(validate_run) … 이 스크립트는 규칙 검사의 임계값 주석에
적을 근거 수치를 만든다. 임계값을 고르거나 바꾸지 않는다.
측정은 quality_checks.content_measurements()를 그대로 불러 규칙 검사와 같은 계산을 쓴다.

입력: 파싱 실행 폴더 하나 이상. 각 폴더에 저장된 selection.json(그 실행이 쓴 선정 목록)의 페이지만 읽는다.
출력: 표준 출력에 JSON 요약. 값별로 큰 순서의 상위 값과 위치(실행·문서·페이지)를 보여 준다.
부작용: 파일을 만들거나 바꾸지 않는다. 모델 로딩·네트워크 호출을 하지 않는다.
실행: 프로젝트 루트에서 python3 -B -m tools.limits_distribution outputs/parsing_vl_... outputs/parsing_structure_...
측정값은 이미 본 개발 표본의 분포다. 일반적인 기준의 근거나 평가 성능으로 쓰지 않는다.
"""

# 표준 라이브러리: argparse는 실행 인자, json은 요약 출력, pathlib은 실행 폴더 경로를 다룬다.
import argparse
import json
from pathlib import Path

# 내부 모듈: read_json은 중복 키·NaN을 거부하는 엄격한 JSON 읽기다.
# pdf_source는 선정 목록 검증, result_store는 실행 폴더의 문서 ID·파일 이름 규칙을 맡는다.
# quality_checks는 규칙 검사와 같은 측정 함수와 현재 임계값(LIMITS)을 준다.
from common.files import read_json
from parsing.pdf_source import load_selection
from parsing.quality_checks import LIMITS, content_measurements
from parsing.result_store import SELECTION_FILE, document_id, document_path, page_stem

# 값마다 보여 줄 상위 개수의 기본값이다. 임계값 바로 위·아래 값을 보려면 최댓값 몇 개면 충분하다.
DEFAULT_TOP = 4
# 짧은 블록 반복 비율을 볼 최소 토큰 수다. 세 단어 묶음을 만들 수 있는 가장 작은 길이다.
MINIMUM_MEASURED_TOKENS = 3


def run_blocks(run: Path):
    """실행 폴더에 저장된 선정 목록의 페이지에서 (위치, 측정값)을 낸다. 없는 페이지 파일은 건너뛴다."""
    selection = load_selection(run / SELECTION_FILE)
    for item in selection["documents"]:
        doc_id = document_id(item["document_name"])
        for number in sorted(item["page_numbers"]):
            path = run / document_path(doc_id, f"{page_stem(number)}.json")
            if not path.is_file():
                continue
            for block in (read_json(path).get("parsing") or {}).get("blocks", []):
                where = {"run": run.name, "document_name": item["document_name"], "page_number": number,
                         "element_id": block.get("element_id")}
                yield where, content_measurements(block["content"], block.get("label"))


def top_values(rows, key, count):
    """key 값이 큰 순서로 상위 count개를 위치와 함께 낸다. 값이 없는 블록은 뺀다."""
    present = [(measured[key], where) for where, measured in rows if measured[key] is not None]
    present.sort(key=lambda pair: pair[0], reverse=True)
    return [{"value": value, **where} for value, where in present[:count]]


def summarize(rows, top):
    """LIMITS 항목별 분포 요약을 만든다. 임계값과 비교한 판정은 하지 않는다."""
    minimum = LIMITS["minimum_tokens"]
    long_blocks = [row for row in rows if row[1]["tokens"] >= minimum]
    short_blocks = [row for row in rows if MINIMUM_MEASURED_TOKENS <= row[1]["tokens"] < minimum]
    for _, measured in rows:
        measured["longest_sequence"] = max(measured["sequence_lengths"])
    short_dominant = [measured["tokens"] for _, measured in short_blocks
                      if measured["dominant_ratio"] >= LIMITS["dominant_token_ratio"]]
    return {
        "limits": dict(LIMITS),
        "block_count": len(rows),
        "minimum_tokens": {
            "blocks_at_or_above": len(long_blocks),
            "short_blocks": len(short_blocks),
            # 짧은 블록에 반복 비율 기준을 적용했다면 걸렸을 블록 수와 그 토큰 수다.
            "short_blocks_reaching_dominant_ratio": len(short_dominant),
            "their_token_counts": sorted(set(short_dominant)),
        },
        "dominant_ratio": top_values(long_blocks, "dominant_ratio", top),
        "repeated_trigram_ratio": top_values(long_blocks, "repeated_trigram_ratio", top),
        "characters": top_values(rows, "characters", top),
        "longest_sequence": top_values(rows, "longest_sequence", top),
        "skipped_long_numbers": [{"values": measured["skipped_long_numbers"], **where}
                                 for where, measured in rows if measured["skipped_long_numbers"]],
    }


def main(argv=None):
    cli = argparse.ArgumentParser(description="저장된 파싱 실행의 LIMITS 측정값 분포")
    cli.add_argument("runs", nargs="+", type=Path, help="파싱 실행 폴더. 각 폴더의 selection.json 페이지만 읽음")
    cli.add_argument("--top", type=int, default=DEFAULT_TOP, help="값마다 보여 줄 상위 개수")
    args = cli.parse_args(argv)
    if args.top < 1:
        cli.error("--top은 1 이상이어야 합니다.")
    rows = []
    for run in args.runs:
        if not (run / SELECTION_FILE).is_file():
            cli.error(f"실행 폴더에 {SELECTION_FILE}이 없습니다: {run}")
        rows.extend(run_blocks(run))
    summary = summarize(rows, args.top)
    summary["runs"] = [run.name for run in args.runs]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
