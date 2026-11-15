# 코드 리뷰 기록 (2026-11-06)

`main`의 `0fe85bc..fa8a71f`(단일 진입점, `experiments` 패키지, 파서 설정 변경)를 리뷰한 결과와 수정 내용을 정리한다.
같은 시기에 받은 하드코딩 1차 점검 결과도 남은 항목으로 함께 적는다.

- 위치는 리뷰 당시 기준이다. `experiments/common.py`의 민감정보 처리는 이후 `common/redaction.py`로 옮겼다.
- 수정 브랜치는 `fix/experiment-comparison-review`이고, 경로 변경을 담은 `refactor/domain-dependencies` 위에 있다.

## 1. 수정한 항목

근거의 "재현"은 리뷰 때 합성 입력으로 실패를 확인한 항목이고, "코드 판단"은 코드와 문서를 대조한 항목이다.

| # | 위치 | 문제 | 근거 | 수정 |
|---|---|---|---|---|
| 1 | `experiments/automatic.py:81` | 페이지를 하나도 처리하지 못했거나 중단된 실행이 정상 실행을 밀어내고 자동 비교 기준이 됨 | 재현 | 치명 오류·중단, 처리한 페이지가 없는 실행을 제외하고 사유를 `ignored`에 남김. 일부 페이지만 실패한 실행은 유지 |
| 2 | `experiments/common.py:68` | 깨진 URL 문자열 하나로 비교 보고서 전체가 실패하고, 정상 URL의 표기(`%20`→`+`)도 바뀜 | 재현 | 깨진 URL은 자격증명 표식이 없으면 원문 보존, 있으면 통째로 가림. 가릴 값이 없는 URL은 다시 조립하지 않음 |
| 3 | `experiments/evaluate.py:403` | 페이지 결과가 없는 경우를 파서 미검출과 같게 채점해 복구·누락이 왜곡됨 | 재현 | `page_absent` 상태 추가. 기준 실행에 없으면 복구로 세지 않음. 두 실행 모두 없으면 분모에서 빼고 `absent_pages`로 보고 |
| 4 | `experiments/evaluate.py:152` | 허용 표기끼리 포함 관계이면 정상 일치가 다중 대응으로 보류됨 | 재현 | 겹치는 구간을 한 위치로 합침. 겹치지 않는 위치의 반복만 다중 대응으로 판정 |
| 5 | `experiments/compare.py:185` | 복사본이 없는 소스는 내용이 달라도 `unchanged`로 표시됨 | 재현 | 복사본을 읽기 전에 해시로 판정. 해시가 없으면 `unknown` |
| 6 | `experiments/compare.py:128` | 원본 PDF 부재와 페이지 JSON 변조가 채점을 막지 않음 | PDF 부재는 재현, 변조는 코드 판단 | 등록 해시와 다른 페이지 JSON은 미해결 처리. PDF 부재는 문제 목록과 `source_file_unverified_pages`에 표시(2절) |
| 7 | `experiments/compare.py:298` | 시각·출력 경로까지 설정 변경으로 나와 바꾼 변수를 확인할 수 없음 | 코드 판단 | 파서·옵션·DPI·전체 페이지 여부만 `requested_setting_changes`로, 나머지는 `operational_changes`로 분리 |
| 8 | `experiments/record.py:214` | 모델 경로를 못 찾아도 `collected`여서 모델이 같다고 읽힐 수 있음 | 코드 판단 | `collected`, `partial`, `unavailable`로 집계하고 요약에 `model_comparison` 추가 |
| 9 | `configs/experiment_recording.json:8` | `paddlepaddle-gpu`가 빠져 GPU 실행의 버전 변경이 보이지 않음 | 코드 판단 | 수집 목록에 추가 |
| 10 | `experiments/resources.py:71` | 샘플에 단계 ID·시각이 없어 문서와 다르고 강제 종료 시 분석이 어려움 | 코드 판단 | 샘플마다 `stage_run_id`, `sampled_at_utc`, 진행 구간 이름·정보를 기록 |
| 11 | `experiments/compare.py:256~275` | 읽지 못한 페이지를 `different_pdf_pages`로 세고, 한쪽이 없어도 같은 원본으로 표시함 | 코드 판단 | 해시가 없으면 `unconfirmed_pdf_pages`로 구분. `same_source_for_quality`는 확인 불가일 때 null |

함께 고친 부분: 보고서 내용을 완성한 뒤 폴더를 만들어, 생성 실패가 빈 폴더를 남겨 재시도를 막는 일을 없앴다.

## 2. 수정하면서 내린 판단

되돌리거나 바꿀 수 있는 결정이므로 근거를 남긴다.

- **원본 PDF가 없어도 채점을 막지 않는다.** `.gitignore`의 `*.pdf` 규칙 때문에 저장소에서 받은 실행에는 항상 PDF가 없다. 막으면 사본에서 채점이 전부 멈춘다. 대신 이 경우 원본 동일성은 기록된 해시로만 판단한 것임을 보고서에 남긴다.
- **비교 실행에서 페이지가 빠지면 새 누락으로 유지한다.** 기존 테스트와 문서가 "예정 페이지 실패도 분모에서 숨기지 않는다"고 정한 동작이다. 복구는 기준 실행의 실패가 확인돼야 하므로 반대 방향만 미해결로 바꿨다.
- **두 실행 모두 없는 페이지는 분모에서 뺀다.** 선정 범위 밖의 정답이 복구율을 낮추지 않게 하려는 것이다. 다만 두 실행 모두 문서 처리에 실패한 경우도 여기에 들어가므로 `absent_pages`와 입력 문제 목록을 함께 봐야 한다.
- **자동 기준에서 제외된 실행도 직접 지정은 가능하다.** `--compare-baseline`으로 고르면 실패한 실행과도 비교할 수 있다.

## 3. 수정하지 않은 항목

AGENTS.md 「하드코딩 금지」에 따라 위치·영향·수정안을 먼저 보고한 항목이며, 반영 여부를 아직 정하지 않았다.
3·4번은 리뷰 뒤 들어온 `pipeline_options`로 코드 수정 없이 값을 바꿀 수 있게 됐지만, 기본값은 여전히 코드에 있다.

| # | 위치 | 고정된 값 | 영향 | 수정안 |
|---|---|---|---|---|
| 1 | `parsing/validate_run.py` `--pdf-dir` | `data/doc_pdfs` | 원본이 없는 환경에서 전 페이지 `source_hash`가 미해결이 되어 `pages_needing_review`가 내용 문제를 구분하지 못함 | 기본값 제거, 지정하지 않으면 `not_checked`로 분리 |
| 2 | `parsing/validate_run.py` `--selection` | `configs/selection.json` | 실행 당시 목록과 달라도 경고가 없어 검사 범위가 조용히 바뀜 | 필수 인자로 바꾸고 실행 폴더의 목록과 차이 출력 |
| 3 | `parsing/structure_parser.py` | `enable_mkldnn: False` | Windows 오류 대응이 기본값으로 모든 환경에 적용됨 | 실행 설정으로 이동, 근거 주석 유지 |
| 4 | `parsing/structure_parser.py`, `parsing/vl_parser.py` | OCR 모델 ID, `pipeline_version` | 기본 모델을 코드에서 정함 | 실행 설정으로 이동 |
| 5 | `parsing/run.py`, 두 어댑터 | `cpu_threads = 4` 중복 | CLI 기본값을 바꿔도 직접 호출 경로는 4를 씀 | 상수 하나로 모으고 어댑터는 필수 인자로 |
| 6 | `parsing/pdf_source.py`, `parsing/run.py` | timeout `30`, dpi `144`, `"cpu"`, `outputs`, `configs/selection.json` | 기본값과 근거가 흩어짐. 작업 폴더 기준이라 실행 위치에 따라 달라짐 | 이름 있는 상수에 근거 주석, 위치 기준 경로 |
| 7 | `parsing/quality_checks.py` | `len(v) < 10` | 10자리 이상 숫자가 조용히 검사에서 빠짐 | `LIMITS`에 추가 |
| 8 | `parsing/quality_checks.py` | `LIMITS` 임계값 | 값별 의미가 없고 코드를 고쳐야 바꿀 수 있음 | 값별 주석, `--limits` 인자 |
| 9 | `parsing/review_pdf.py` | 색·두께·글자 크기 | 이름 없는 값이 흩어져 있음. 검색 품질과는 무관 | 표시 상수로 묶음 |
| 10 | `parsing/validate_run.py`, `experiments/compare.py` | 문서 ID·페이지 파일명 규칙 | `parsing/run.py`와 따로 구현돼 어긋나도 드러나지 않음 | 공용 함수로 통합 |
| 11 | `parsing/run.py` | 환경 패키지 목록 | 기록 설정의 목록과 이중 관리됨. 9번 지적도 여기서 비롯함 | 한 곳으로 통합 |

후속(2026-11-12, 브랜치 `fix/hardcoded-defaults-v2`): 2·5~11번을 반영했다. 2026-11-06에 만든 브랜치
`fix/hardcoded-defaults`(커밋 `c05ca6c`, 머지하지 않음)를 참고해 당시 main에 맞춰 다시 적용했다.

| # | 반영 내용 |
|---|---|
| 1 | 이 브랜치 이전에 main에서 다른 방식으로 처리됐다. `--pdf-dir` 기본값이 없고, 생략하면 실행 폴더의 `source.pdf` 사본과 대조한다 |
| 2 | 수정안을 바꿨다. 필수 인자 대신 실행 폴더에 저장된 `selection.json`을 기본값으로 쓰고, 다른 목록을 주면 차이를 요약에 남긴다. 기존 명령이 깨지지 않고 기본값이 코드가 아닌 그 실행의 기록에서 온다 |
| 3·4 | 반영하지 않았다. 반영 여부 미정 |
| 5·6 | `parsing/run.py`의 이름 있는 상수(`DEFAULT_*`)로 모으고, 경로는 파일 위치 기준으로 바꿨다. 어댑터의 `cpu_threads`, `read_pdf_bytes`의 timeout은 필수 인자다 |
| 7·8 | `LIMITS`에 `maximum_sequence_digits`를 더하고(값 9, 판정 동일, 규칙 0.3.1), 값마다 단위·역할·측정 분포를 주석에 적었다. 선택 근거는 기록이 없어 쓰지 않았다. `--limits` 인자는 넣지 않았다 |
| 9 | 표시 상수로 묶었다 |
| 10 | `parsing/result_store.py`에 모았다. 리뷰 뒤 생긴 `resume.py`·`rerecognize.py`·`chunking/run.py`의 같은 규칙도 함께 옮겼다 |
| 11 | `parsing/run.py`의 `ENVIRONMENT_PACKAGES` 하나로 합쳤다. 기록 설정에서는 `pillow`가 빠져 실제로 목록이 어긋나 있었다 |

## 4. 검증

- `requirements.txt`와 같은 버전의 `PyMuPDF`, `psutil`과 범위 안의 `numpy 2.5.4`를 임시 가상환경에 설치해 전체 테스트를 실행했다. Python 3.12.3, Linux, 105개 전부 통과했고 이 중 23개가 이번에 추가한 회귀 테스트다.
- 저장된 VL·Structure 실행에 비교기를 실행했다. `source.pdf`가 없는 문서 9개가 실행마다 문제 9건으로 표시되고, 기록이 없는 과거 실행의 모델 비교는 `unavailable`로 나왔다. 수정 전에는 문제가 0건이었다.
- 실행하지 않은 것: 실제 모델 추론, Windows 환경, GPU 환경. 회귀 테스트를 수정 전 코드에 돌려 실패를 다시 확인하지는 않았고, 수정 전 실패는 리뷰 때의 재현에 근거한다.

## 5. 정답 파일 현황

채점기는 구현됐지만 실제 원문 정답 파일은 아직 없다. 지금까지의 채점 결과는 테스트의 모의 정답에 대한 것뿐이고, 저장된 실행의 품질은 `unmeasured`다.
정답 구조와 채점 규칙은 [실행 비교 사용법](../planning/EXPERIMENT_COMPARISON_USAGE.md) 6절을 따른다.
