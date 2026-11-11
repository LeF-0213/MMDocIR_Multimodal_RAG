# 실행 기록과 전후 비교 사용법

작성일: 2026-11-05. 공통 기록과 파싱 전후 평가의 현재 구현을 설명한다.
전체 설계·후속 단계 확장은 [구현 계획](EXPERIMENT_COMPARISON_PLAN.md),
실제 파싱 실행과 기존 결과 범위는 [루트 README](../../README.md)를 참고한다.

## 1. 입력부터 보고서까지

```text
parsing_run.json → main.py → 선정 목록·기본/지정 기록 설정 → experiments.parsing_stage → parsing.run
 → 로컬 PDF 읽기/HTTP 입력·기준 실행 선택 → 모델 초기화·페이지 처리
 → run.json·페이지 JSON·PDF·PNG + experiment.json·experiment_finished.json·resources.jsonl·source_snapshot/
 → 파싱 기록 종료 → 자동 전후 비교 → 실행 폴더 옆 <실행명>_comparison/comparison.json·comparison.md

기준 실행 + 비교 실행 + 선택적 선정 목록·정답·평가 설정
 → experiments.compare → 콘솔 요약 + 선택적 comparison.json·comparison.md
```

공통 기록과 종료 후 비교는 기본 활성화다. `--experiment-config`를 생략하면 모듈 위치 기준의
`configs/experiment_recording.json`을 사용한다. `--experiment-config`는 설정 교체용이며
`--no-experiment-record`로 공통 기록을, `--no-auto-compare`로 종료 후 비교를 각각 끈다.
기존 파싱 결과도 함께 저장한다. `--help`는 기록 파일을 만들지 않는다.
기존 파싱 경로·DPI·모델 기본값은 이번 작업에서 일괄 변경하지 않았다.
현재 공통 기록을 연결한 실제 단계는 파싱이다. 청킹·임베딩·검색·리랭킹 알고리즘은 구현하지 않았다.
공통 `ExperimentRecorder`의 `stage`, `artifact()`, `reuse()`, `measure()`는 후속 단계에서도 사용할 수 있다.

## 2. 구성 파일과 선택 이유

| 파일 | 책임 |
|---|---|
| [main.py](../../main.py) | 실행 JSON 검증·기존 파싱 호출, 읽기 전용 설정 확인 |
| [파싱 실행 설정](../../configs/parsing_run.json) | 입력·파서·장치·해상도·출력·기록/비교 활성화와 경로 |
| [common/files.py](../../common/files.py) | 엄격한 JSON, 경로 경계, 해시 |
| [common/redaction.py](../../common/redaction.py) | 민감 필드 처리, 비밀 의심 소스 판별 |
| [schema.py](../../experiments/schema.py) | 기록·비교 파일 이름과 스키마 버전 |
| [parsing_stage.py](../../experiments/parsing_stage.py) | 파싱에 기록기·자동 비교 연결, 인자 지정 실행 진입점 |
| [resume_stage.py](../../experiments/resume_stage.py) | 중단된 실행에 재개 세그먼트 기록·자동 비교 연결, 재개 진입점 |
| [parsing/resume.py](../../parsing/resume.py) | 중단 실행의 설정 복원·호환성 검사·저장 페이지 검증, `run.py`에 넘기는 재개 세션 |
| [record.py](../../experiments/record.py) | 시작/종료 manifest, 소스 복사본·Git diff, 모델 파일 출처, 결과물 연결 |
| [resources.py](../../experiments/resources.py) | 하나의 관측 스레드로 전체 실행과 중첩 구간의 RSS·시간·선택적 CUDA 할당량 수집 |
| [evaluate.py](../../experiments/evaluate.py) | 정답을 입력받는 문장·위치·표의 순수 전후 평가 |
| [compare.py](../../experiments/compare.py) | 기존/새 실행 읽기, 입력·설정·코드·모델·비용·품질 비교, 보고서 저장 |
| [automatic.py](../../experiments/automatic.py) | 파싱 시작 시 기준 선택·검증, 종료 후 같은 비교기 호출·상태 저장 |
| [기록 설정](../../configs/experiment_recording.json) | 프로젝트·소스 경로, 수집 범위·간격, 실제 모델 객체 경로, 파서 옵션 |
| [평가 설정](../../configs/experiment_evaluation.json) | 정규화·매칭·IoU·표 자원 한도 |

기록·비교·평가의 핵심은 Python 표준 라이브러리를 사용한다. 자원 관측은
Paddle·NumPy의 네이티브 메모리를 포함한 OS RSS를 읽으려고 `psutil`을 직접 의존성으로 지정했다.
GPU 수집은 이미 로딩된 Paddle의 allocator API만 사용하며 GPU 전체 메모리와 구분한다.
비교 CLI는 Paddle·NumPy·모델을 import하지 않고 기존 순수 규칙 검사도 재사용한다.

## 3. 기록 설정

기록 JSON의 `schema_version`은 `project.experiment.v1`이다. 프로젝트 경로는 설정 파일 위치 기준이고,
`source_paths`는 그 프로젝트 루트 기준이다. 실행 환경별 값은 설정·CLI에서 받는다.

| 설정 | 의미·현재 예시의 이유 |
|---|---|
| `project_root` | 기록할 프로젝트의 루트. 기본 제공 파일의 `..`는 configs의 상위 폴더를 뜻함 |
| `source_paths`·`source_suffixes` | 명시한 프로젝트 소스·설정만 복사. 모델·데이터 전체를 소스에 중복 저장하지 않음 |
| `git_timeout_seconds` | Git 조회의 대기 상한. 예시의 10초는 작업 중 무한 대기를 피할 설정값 |
| `memory.interval_seconds` | RSS 관측 간격. 예시의 0.1초는 초기 관측 설정이며 순간 최고점 확보나 최적 성능을 보장하지 않음 |
| `memory.include_children` | 자식 프로세스도 PID별로 기록. RSS 합계를 고유 메모리 사용량으로 표시하지 않음 |
| `memory.gpu_device` | null이면 GPU 수집 미요청. 지원 환경에서 장치 ID 또는 Paddle GPU 장치 문자열 지정 |
| `secret_environment_names` | 기록에서 가려야 하는 비밀 값의 환경 변수 이름. 실제 값을 JSON에 적지 않음 |
| `environment_packages` | 모델을 import하지 않고 버전만 확인할 패키지 목록. OS·Python·보이는 논리 CPU 수와 함께 기록하며 CPU quota는 추정하지 않음 |
| `experiment` | 목적·변경 변수·고정 조건·기대 효과를 작성. 실행 의도이며 인과 효과의 검증 결과가 아님 |
| `pipeline_options`·`predict_options` | 실제 생성자·predict와 실행 기록에 함께 적용할 override. 기본 제공 값은 빈 객체 |
| `model_bindings` | parser별 모델 역할·실제 객체 속성 경로·이름/폴더 속성 이름. 모델 이름으로 캐시 경로를 추측하지 않음 |

기본 binding은 설치된 PaddleOCR 3.7.0·PaddleX 3.7.2의 단일 장치 객체 구조를 기준으로 작성했다.
라이브러리·엔진·다중 장치 구조가 달라져 경로를 찾지 못하면 해당 모델을 미확보로 기록한다.
추가 모델을 켰으면 수집할 binding도 명시해야 한다. 수집 범위는 `configured_object_paths_only`다.
가중치를 로딩했다는 사실과 페이지마다 그 모델을 호출했다는 사실은 구분한다.

소스·설정은 보관 당시 bytes와 해시를 연결한다. 비밀 상수나 URL 자격증명이 의심되는 파일은 보류한다.
민감 필드·URL 자격증명·명시한 환경 비밀 값을 기록에서 가린다. 비밀 파일은 소스 allowlist에 넣지 않는다.
실행 종료 때 소스가 바뀌었는지 검사하지만, 실행 도중 한 번 바뀌었다가 원복된 모든 상태까지 복원하지는 않는다.

## 4. 파싱 실행

확인 환경은 macOS·기본 셸 zsh·Python 3.12.10의 `.venv`다. 아래 명령은 Bash 문법이며
프로젝트 루트에서 실행한다. Linux·Windows에서도 같은 Python 모듈을 사용하되 가상환경 Python 경로는 다를 수 있다.

### 기본 실행

```bash
.venv/bin/python -B main.py
```

설정은 `configs/parsing_run.json`에서 읽는다. 기본 입력은 계속 사용하기로 확인한
`http://127.0.0.1:8080/raw/mmdocir_data/extracted/doc_pdfs/`다. HTTP 서버는 별도로 실행 중이어야 한다.
이 주소로 실제 연결·다운로드·신규 OCR 추론은 실행하지 않았다.
명령 실행 시 원본 읽기·모델 다운로드/캐시·새 실행 폴더·선택적 자동 보고서 생성이 발생한다.
로컬 입력을 선택하면 원본 bytes는 변경하지 않고 실행 폴더에 `source.pdf` 복사본과 해시를 보존한다.

| 실행 설정 | 의미·현재 값의 이유 |
|---|---|
| `schema_version` | `project.parsing_run.v1`. 알 수 없는 필드·잘못된 타입은 실행 전에 거부 |
| `selection` | 현재 설정 폴더의 선정 JSON. 문서·페이지를 코드에 넣지 않음 |
| `input.kind`·`input.pdf_directory`·`input.base_url` | 기본 `http`는 pdf_directory=null과 확인한 PDF 폴더 URL. `local`은 원본 폴더와 base_url=null. 두 입력을 동시에 사용하지 않음 |
| `output_root` | 실행 결과를 쌓을 폴더. 실행마다 UTC 시각으로 새 폴더를 생성 |
| `parser`·`formula_model` | VL이면 formula_model=null, Structure이면 실제 수식 모델 ID 명시 |
| `device`·`cpu_threads` | 기본 CPU·4스레드는 기존 시작 설정을 유지. 현재 하드웨어의 최적값으로 측정하지 않음 |
| `dpi` | 기존 144 DPI 유지. 작은 문자 품질과 비용은 별도 실험 필요 |
| `http_timeout_seconds` | 기존 30초 유지. HTTP 연결·읽기에만 적용하며 로컬 읽기 제한 시간이 아님 |
| `all_pages` | false이면 선정 페이지만, true이면 선정 문서의 전체 페이지 |
| `recording.enabled`·`recording.config` | 공통 기록 활성화와 기록 설정 JSON 위치 |
| `comparison.enabled`·`comparison.baseline` | 비교 활성화. baseline=null이면 최근 호환 실행을 자동 선택 |
| `comparison.output_directory` | null이면 새 실행 폴더 옆에 자동 보고서 생성. 고정 폴더를 지정하면 재실행 때 덮어쓰지 않고 거부 |
| `comparison.gold`·`comparison.evaluation_config` | 정답이 없으면 미측정. 실제 정답과 평가 설정의 경로를 지정하면 종료 후 같은 평가기 사용 |

모든 상대 파일·폴더 경로는 실행 JSON의 폴더 기준이며, 장치·수치·모델 ID는 실행 코드 안에 새로 고정하지 않는다.
서버 없이 로컬 파일을 읽으려면 input을 `kind=local`, `pdf_directory=../data/doc_pdfs`, `base_url=null`로 바꾼다.
현재 로컬 폴더에 선정 문서 9개가 존재함을 확인했으며 PDF 내용·추론 성공까지 확인한 것은 아니다.
`--config "$RUN_CONFIG"`로 다른 설정 파일을 선택할 수 있다. `main.py`는 입력 처리·저장 로직을 복제하지 않고
`experiments.parsing_stage`로 전달한다. 공통 소스 복사본 대상에 `main.py`와 `common`도 포함했다.
`parsing.run`은 실험 기록을 모르며, 직접 실행하면 파싱 결과만 남기고 기록·자동 비교를 하지 않는다.
`run.json`의 `launch_config`는 실제 읽은 설정·경로·SHA-256을 보존하며 민감 URL 값은 가린다.

```bash
.venv/bin/python -B main.py --check
```

확인 명령은 설정·선정 목록·로컬 파일 존재·비교 계획만 읽는다. 모델 로딩·HTTP 요청·출력 폴더 생성은 하지 않는다.
PDF 내용 유효성·모델 다운로드 가능성·실제 추론·품질을 확인한 것으로 해석하지 않는다.
출력 경로는 생성 예정 경로이며 실제 생성하지 않는다.

### 기존 CLI로 직접 실행

아래 명령을 사용할 때만 각 경로·주소·장치·스레드·DPI 환경 변수에 이번 실행 값을 먼저 지정한다.

도움말은 모델 로딩·다운로드·결과 파일 생성을 하지 않는다.

```bash
.venv/bin/python -B -m experiments.parsing_stage --help
.venv/bin/python -B -m experiments.compare --help
```

다음은 VL 파싱 명령 형식이다. HTTP 읽기·모델 다운로드/캐시·새 결과 폴더와 소스 복사본·자원 로그 생성이 발생한다.
기존 출력 폴더는 거부한다. 기본 기록 설정을 쓰는 명령은 다음과 같다.

```bash
.venv/bin/python -B -m experiments.parsing_stage \
  --base-url "$PDF_BASE_URL" --selection "$SELECTION_PATH" \
  --parser vl --device "$DEVICE" --cpu-threads "$CPU_THREADS" --dpi "$DPI" \
  --output-dir "$RUN_OUTPUT"
```

Structure에서는 파서 선택을 `structure`로 바꾸고 `--formula-model "$FORMULA_MODEL"`을 추가한다.
예시 모델 ID를 실행 코드에 새로 고정하지 않는다. 전후 실행은 서로 다른 출력 폴더에 보존한다.

### 종료 후 자동 비교

실행 폴더의 바로 위 폴더에서 같은 파서·선정 문서/페이지·`--all-pages` 여부의 종료 실행을 찾는다.
선정 목록의 JSON 공백·문서/페이지 순서는 비교 대상 선택에 영향을 주지 않는다.
`run.json`의 시간대가 있는 시작 시각이 가장 최근인 실행을 고른다. 일부 페이지만 실패한 종료도 대상에 포함한다.
미종료·치명 오류·중단·처리한 페이지가 없는 실행·손상·공통 시작/종료 ID 불일치는 제외 사유를 남긴다.
최근 시각이 같으면 임의로 고르지 않는다.
선택한 기준과 메타데이터 해시는 파싱 시작 전에 고정하며, 종료 시 변경을 감지하면 비교 실패로 남긴다.
모델·DPI·설정 차이는 비교할 변수이므로 기준 선정에서 일치시킬 조건으로 강제하지 않는다.
원본 PDF 해시가 다르면 품질 비교에서 구분하며, 비교 결과만으로 변경의 인과 효과를 확정하지 않는다.

첫 실행처럼 기준이 없으면 기록만 보존하고 비교는 `skipped`로 남긴다.
기준이 있으면 파싱 자원 기록을 닫은 뒤 실행 폴더 옆의 새 `<실행명>_comparison/`에 보고서를 만든다.
`--all-pages` 실행에서는 저장한 전체 페이지를 비교하며 선정 페이지 목록으로 다시 잘라내지 않는다.
다른 파서·다른 상위 폴더와 비교하려면 기준을 직접 지정한다.

```bash
.venv/bin/python -B -m experiments.parsing_stage \
  --base-url "$PDF_BASE_URL" --selection "$SELECTION_PATH" --output-dir "$RUN_OUTPUT" \
  --compare-baseline "$BASELINE_RUN" --comparison-output-dir "$COMPARISON_OUTPUT"
```

이 명령은 파싱·공통 기록·보고서 파일을 생성한다. 확인한 정답까지 평가하려면
`--comparison-gold "$GOLD_PATH"`를 추가한다. 기본 평가 설정을 교체할 때는
`--comparison-evaluation-config "$EVALUATION_CONFIG"`를 추가한다. 정답 없이 품질은 미측정이다.
공통 기록의 수집 간격·모델 binding·실험 의도 등을 바꾸려면
`--experiment-config "$RECORDING_CONFIG"`를 추가한다.
두 자동 기능을 모두 끄려면 `--no-experiment-record --no-auto-compare`를 함께 지정한다.
이때도 기존 파싱 기록과 자동 비교 비활성화 상태 파일은 남긴다.

### 기록 파일의 의미

| 파일 | 내용·수집 범위 |
|---|---|
| `run.json` | 기존 시작 기록. 실제 전달 옵션과 기록 설정 파일 해시 |
| `experiment.json` | 시작 시각·실행/단계 ID·요청 설정·실험 의도·소스 목록·Git 상태 |
| `source_snapshot/` | 수집을 허용한 프로젝트 파일의 원문 복사본 |
| `experiment_finished.json` | 종료 상태·해석 설정·구성 모델 파일·자원 구간·결과물/재사용 연결·소스 변경 감지 |
| `experiment_models.json` | 모델 로딩 직후의 구성 모델 파일 목록. 종료 기록 없이 끊겨도 재개 때 비교 근거로 남음(2026-11-10 이후 실행) |
| `resume/segment_<번호>/` | 재개 세그먼트의 시작·종료 기록, 세그먼트 manifest·자원 기록·소스 복사본, 옮겨 둔 잔여 파일(`partial/`) |
| `resources.jsonl` | 단계 ID·UTC 시각·진행 구간 이름과 PID를 포함한 점진적 RSS·선택적 CUDA 관측 |
| `auto_comparison.json` | 기준 선택·제외 근거, 완료/생략/비활성화/실패, 보고서 경로·해시. 자원 기록 종료 뒤 생성 |
| 기존 PDF·PNG·페이지 JSON | 원본·이미지·모델 결과. 새 페이지 JSON의 `experiment` 필드가 실행 ID를 연결 |

해석 설정은 확인한 버전의 `_merged_paddlex_config`를 읽으며 수집 경로와 구현 파일 해시를 남긴다.
관찰 설정은 각 페이지의 raw `model_settings`에 있다. 반환되지 않는 디코딩 값을 확인한 것으로 표시하지 않는다.
메모리 최고값은 관측 최대값이며 현재 GPU 동기화는 하지 않는다. 관측 실패는 별도 기록한다.
모델 초기화·다운로드, PDF 읽기·렌더링·예측·저장, 소스/가중치 기록 비용이 전체 시간에 포함된다.
기록을 켠 실행과 끈 실행의 시간을 순수 추론 속도 차이로 단정하지 않는다.
자동 비교의 시간·메모리는 이 파싱 자원 기록에 포함하지 않는다.

모델 목록은 파일 상대 경로·크기·SHA-256에 기반한다. 같은 내용의 다른 캐시 폴더는 동일한 목록 해시가 된다.
HF local_dir 메타데이터가 있으면 ETag와 실제 bytes를 대조해 commit 근거를 기록한다.
로컬 메타데이터 내용과의 대조이며 공식 서버를 새로 조회하는 검증은 아니다.
전체 파일의 근거가 일치하지 않으면 모델 전체의 `verified_revision`은 null이다.
요청한 revision·실제 파일 해시·로컬 출처 선언은 별도 필드다. 임의의 요청 revision을 검증값에 복사하지 않는다.

### 중단된 실행 재개

전원 차단·메모리 부족·Ctrl+C로 끊긴 실행은 같은 폴더에서 이어서 처리한다.
정상 종료한 실행(`summary.json`에 치명 오류 없음)은 재개하지 않는다.

```bash
.venv/bin/python -B main.py --resume "$INTERRUPTED_RUN" --check   # 재사용·재처리 예정만 출력, 파일 변경 없음
.venv/bin/python -B main.py --resume "$INTERRUPTED_RUN"           # 남은 페이지 처리
```

비교 기준은 처음 실행이 `run.json`의 `automatic_comparison`에 고정한 계획을 그대로 잇는다.
원본 위치나 비교 기준을 바꾸려면 재개 모듈을 직접 실행한다.

```bash
.venv/bin/python -B -m experiments.resume_stage --run-dir "$INTERRUPTED_RUN" \
  --pdf-directory "$PDF_DIRECTORY" --compare-baseline "$BASELINE_RUN"
```

| 확인·처리 | 규칙 |
|---|---|
| 실행 설정 | 현재 설정 파일이 아니라 중단된 실행의 `run.json`·`selection.json`·`experiment.json`에서 복원 |
| 코드 | `run.json`의 파일별 코드 해시(`parsing/`·`common/`)가 모두 같아야 함. 바뀌거나 추가된 파일이 하나라도 있으면 거부 |
| 환경 | 패키지 버전·Python 빌드·파서 옵션·predict 옵션이 같아야 함. 플랫폼은 기록만 함 |
| 모델 파일 | 이전 세그먼트의 `experiment_models.json`이 있으면 역할별 목록 해시를 비교. 없으면 미확인으로 기록 |
| 원본 PDF | 다시 읽은 bytes의 해시가 실행 폴더의 `source.pdf`와 다르면 거부(`ResumeError`) |
| 완료 문서 | `document.json`이 있으면 원본·페이지·PNG·그림·검토 PDF 해시를 확인하고 그대로 재사용. 손상되면 거부 |
| 미완료 문서의 페이지 | 유효한 페이지 JSON은 재사용하고 검토 PDF 페이지만 다시 만듦. 없거나 손상된 페이지는 잔여 파일을 `partial/`로 옮긴 뒤 다시 처리 |
| `failed` 페이지 | 실행 결과로 보고 다시 처리하지 않음 |
| 자동 비교 | 재개 때 `--compare-baseline`·`--no-auto-compare`를 지정하지 않으면 중단 전 계획을 복원(`mode: restored`). 자동 선택으로 골랐던 기준도 다시 찾지 않음. 고정한 기준이 없어졌거나 메타데이터 해시가 바뀌었으면 비교만 생략하고 재개는 진행. 시작할 때 비교를 생략·해제한 실행은 그 상태를 이음 |
| 시간·메모리 | `summary.json`의 경과 시간은 마지막 세그먼트만 측정(`elapsed_scope`). 비교 보고서는 전체 시간·메모리를 `not_comparable_resumed`로 표시. 페이지별 시간은 비교 가능 |

재개는 새 실행과 같은 `parsing/run.py`의 `run_parsing()`·`process_document()`로 처리한다.
`parsing/resume.py`는 설정 복원·호환성 검사·저장 페이지 검증과 재개 세션(세그먼트 폴더·잔여 파일 이동·재사용 통계)만 맡는다.
처리 경로가 하나라서 처음 실행한 페이지와 재개한 페이지가 같은 규칙으로 만들어진다.
이 기능을 넣으며 `parsing/run.py`가 바뀌었으므로, 2026-11-10 이전에 시작해 중단된 실행은 코드가 달라 재개할 수 없다.
그런 실행은 새 폴더로 처음부터 다시 실행하며, 이후 실행부터 코드를 고치지 않은 상태에서 재개할 수 있다.

## 5. 기존 실행 또는 새 실행 비교

다음 명령은 두 입력을 읽고 콘솔 요약만 출력한다. 입력 파일을 수정하지 않는다.
`BASELINE_RUN`, `CANDIDATE_RUN`, `SELECTION_PATH`에 비교할 실행과 선정 목록의 실제 경로를 지정한다.

```bash
.venv/bin/python -B -m experiments.compare \
  --baseline "$BASELINE_RUN" --candidate "$CANDIDATE_RUN" \
  --selection "$SELECTION_PATH"
```

선정 목록을 생략하면 저장된 모든 페이지 JSON을 읽는다. 과거에 선정에서 제외한 페이지도 들어갈 수 있으므로
의도한 평가 범위가 있으면 선정 목록을 명시한다. 추가 페이지의 별도 실행 기록도 페이지별 근거에 연결한다.
누락·실패·손상 페이지와 입력 해시 불일치는 보고서에 남긴다. 같은 DPI라도 실제 PNG 해시를 확인한다.
과거 실행에 없는 소스 복사본·메모리·가중치 revision은 미확보로 남긴다.

보고서 파일을 보존하려면 두 입력 폴더 밖의 새 `COMPARISON_OUTPUT`을 지정한다.
새 폴더와 `comparison.json`·`comparison.md`가 생성되며 기존 폴더는 거부한다.

```bash
.venv/bin/python -B -m experiments.compare \
  --baseline "$BASELINE_RUN" --candidate "$CANDIDATE_RUN" \
  --selection "$SELECTION_PATH" --output-dir "$COMPARISON_OUTPUT"
```

JSON에는 요청/해석/관찰 설정, 실제 소스 unified diff, 모델 파일 차이, 출력·기존 규칙 검사,
시간·관측 최대 RSS·최상위 예측 호출 수의 전후 값과 차이, 정답 평가의 상세 근거를 담는다.
호출 수는 파서의 페이지 예측 호출이며 내부 구성 모델별 호출 수·청구 비용을 추정하지 않는다.
Markdown은 같은 JSON에서 요약과 소스 diff·품질 집계를 생성한다.
질의·검색 후보·리랭킹 지표를 파싱 출력에서 추정하지 않는다.

## 6. 정답 파일과 품질 비교

실제 문장·표 정답 파일은 아직 만들지 않았다. 테스트 안의 정답은 모의 데이터다.
정답이 없으면 `quality.status=unmeasured`이며 형식 검사 통과를 정확도 점수로 바꾸지 않는다.
정답에는 아래 구조가 필요하다. ID·해시·문자·좌표는 실제 원본에서 확인한 값으로 작성한다.

| 위치 | 필드 |
|---|---|
| 최상위 | `schema_version`: `project.parsing_gold.v1`, `version`, `split`: `development` 또는 `test`, `pages`: 목록 |
| 페이지 | `document_id`, `page_number`(PDF 첫 페이지부터 1), `source_sha256`, `coordinate_system`: `pymupdf_unrotated_page_points`, `review_status`: `confirmed` 또는 `unresolved` |
| 페이지의 문장 목록 `sentences` | 각 항목의 유일한 `id`, `text`, 선택적 `alternatives`, PDF 사각형 목록 `bboxes` |
| 페이지의 표 목록 `tables` | 각 항목의 유일한 `id`, 표 사각형 목록 `bboxes`, `cells` |
| 정답 셀 | 유일한 `id`, `row`·`column`(0부터), `rowspan`·`colspan`(양수), `text`, 선택적 `headers` 문자열 경로. 아이콘은 `kind=image`로 명시 |

문장 내용은 NFC·공백 처리 뒤 정확 부분 문자열을 먼저 찾고, 블록 분할·결합을 추적한다.
같은 문장이 여러 번 나타나거나 서로 다른 정답이 같은 출력 구간을 소비하면 미해결로 남긴다.
정확 대응이 없을 때의 블록 유사도는 `SequenceMatcher` 비율이며 CER/WER나 의미 정확도 자체가 아니다.
`missing`은 설정한 매칭 정책에서의 미검출이다. OCR이 의미상 확실히 누락했다고 자동 증명하는 판정은 아니다.
`page_absent`는 그 실행에 페이지 결과가 아예 없는 상태다. 기준 실행의 페이지가 없으면 복구로 세지 않고 미해결로 둔다.
비교 실행에서 맞던 항목이 사라진 경우는 새 누락이다. 두 실행 모두 없는 페이지는 분모에서 빼고 `absent_pages`로 보고한다.
허용 표기끼리 포함 관계여서 구간이 겹치면 한 위치로 합치고, 겹치지 않는 위치에 반복될 때만 다중 대응으로 본다.
매칭·문자·후보 한도에 닿으면 누락 대신 미해결로 기록한다.

위치는 PDF 사각형 합집합의 IoU로 계산한다. 큰 외접 bbox 하나로 바꾸지 않는다.
기존 블록은 `block_proxy` 위치 점수로 보고한다. 출력이 명시적으로 문장 위치를 제공한 경우만
문장 수준으로 구분하며 기존 bbox에 문장 수준이라는 필드를 임의로 붙이지 않는다.
표는 논리 격자의 앵커 셀을 비교하며 값 일치와 행·열·병합·헤더 대응을 따로 집계한다.
헤더 연결은 명시적 HTML `th`만 해석한다. `td`나 시각적 강조에서 헤더를 추론하는 기능은 없다.
이미지·아이콘 셀은 텍스트만으로 평가하지 않고 미해결로 남긴다. 빈 문자열이 같다는 이유로 아이콘 복원을 통과시키지 않는다.
문서별 지식으로 숫자의 의미나 올바른 헤더를 자동 생성하지 않는다.

평가 설정의 0.8 유사도·0.5 IoU 등은 개발용 시작값이며 검증된 최적 기준이 아니다.
적용 설정과 정답 파일 해시를 보고서에 남기고 최종 평가 표본으로 반복 조정하지 않는다.
정확 복구율은 확인된 정확 항목 수/확인된 정답 수이며, 모호한 대응 수를 함께 보고한다.
표 전체 누락과 예정 페이지 실패도 분모에서 숨기지 않는다. 판단 보류 정답은 별도 수로 남긴다.

```bash
.venv/bin/python -B -m experiments.compare \
  --baseline "$BASELINE_RUN" --candidate "$CANDIDATE_RUN" \
  --selection "$SELECTION_PATH" --gold "$GOLD_PATH" \
  --evaluation-config "$EVALUATION_CONFIG" --output-dir "$COMPARISON_OUTPUT"
```

이 명령은 새 보고서를 생성한다. 원문 PDF·모델 출력·정답 파일은 덮어쓰지 않는다.
정답을 매칭·평가에 사용하지만 모델 추론이나 검색용 문서 표현에는 전달하지 않는다.

## 7. 검증과 실패 확인

다음 명령은 테스트 내부의 임시 폴더에 모의 PDF·모델 파일·기록·정답을 만들고 종료 시 정리한다.
모델 가중치 다운로드·실제 OCR 추론·기존 outputs 수정은 하지 않는다.

```bash
.venv/bin/python -B -m unittest discover -s tests -v
.venv/bin/python -m pip check
```

실패한 실제 실행도 새 출력 폴더로 구분한다. 종료 manifest가 없으면 중단 또는 미완성으로 표시한다.
원본·PNG 해시 오류와 등록 뒤 바뀐 페이지 JSON은 품질 채점에서 미해결로 처리하고, 소스 복사본 변조 시 diff를 만들지 않는다.
원본 PDF가 아예 없으면 `.gitignore` 규칙상 흔한 상태이므로 채점을 막지 않는다. 대신 입력 문제 목록과
`source_file_unverified_pages`에 남기며, 이때 원본 동일성은 기록된 해시로만 판단한 것이다.
`requested_setting_changes`는 파서·옵션·DPI·전체 페이지 여부만 비교하고, 시각·경로 같은 값은 `operational_changes`로 분리한다.
요약의 `model_comparison`이 `verified`가 아니면 `model_changes`가 비어 있어도 모델이 같다고 읽지 않는다.
보고서 출력이 입력 폴더 안이거나 기존 폴더이면 비교 CLI가 종료 코드 2로 거부한다.
파싱은 기존과 같이 부분 실패·실패가 있으면 종료 코드 1을 반환한다.
파싱 자체가 성공했어도 자동 보고서 생성이 실패하면 종료 코드 1이며, 파싱 결과는 보존한다.
이 경우 `summary.json`의 파싱 상태와 `auto_comparison.json`의 비교 실패를 구분한다.
파싱 중단 시에는 자원 종료 기록과 자동 비교 생략 상태를 남기고 중단 예외를 다시 전달한다.
메모리 실패·모델 파일 경로 미확보는 기록 상태에 남으며 인식 정확도나 가중치 revision을 대신 만들지 않는다.

후속 단계는 `ExperimentRecorder(..., stage=...)`에 실제 단계 이름·요청 조건을 전달한다.
`reuse()`에는 원본 실행·단계·결과물 ID와 기대 해시를 전달해 이전 결과를 입력 참조로 연결한다.
원본 ID는 호출부의 선언이며 현재 파일의 해시 일치와 구분한다.
새 결과는 `artifact()`에 등록하고, 먼저 등록한 입력/출력 ID만 부모로 연결해 생성 순환을 막는다.
단계별 검색 품질 평가기는 후속 알고리즘을 구현할 때 추가한다.
