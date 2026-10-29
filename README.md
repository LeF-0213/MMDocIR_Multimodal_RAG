# MMDocIR Multimodal RAG

PDF의 텍스트·표·수식·그림을 원본 위치와 연결하고, 검색 결과의 실패 원인을 분석하는 학습·연구 프로젝트다.
현재 범위는 파싱부터 검색·평가까지이며 답변 생성은 포함하지 않는다.

```text
인덱싱: PDF → 파싱·검증 → 청킹 → 임베딩 → 저장·인덱싱
검색: 질문 → 채널별 후보 검색 → 공통 후보 매핑 → 결합 → 리랭킹 → Top 3
평가: 정답 누락·무관한 결과·최초 실패 단계 기록
```

## 현재 상태

| 항목 | 상태 |
|---|---|
| 입력 | 영어 PDF 9개 문서·27페이지의 특성별 표본 |
| 파싱 | PaddleOCR-VL v1.6 주 경로·PP-StructureV3 비교 경로, 좌표 연결·저장·실행 코드 |
| 실행 연결 | VL의 `parser_options()`·`build_parser()` 인자를 `run.py`와 맞추고 공통 추론 모듈 사용 |
| 검증 | 모델 없는 모의 결과·PDF 텍스트 추출·회전·CropBox 좌표 검증 수행 |
| 실제 실행 | 기존 VL 실행의 27페이지 처리 기록: `ok` 26개·`partial` 1개. 처리 상태이며 품질 판정이 아님 |
| 추가 실행 | 3M 9페이지 VL 파싱 완료(`ok`). 기존 문서 폴더에 JSON·PNG를 추가하고 검토 PDF를 8·9·15페이지로 갱신 |
| Structure 실행 | PP-StructureV3 27페이지(3M 8·9·15 포함) 처리 기록: `ok` 27개, CPU 약 3.4시간. 처리 상태이며 품질 판정이 아님 |
| 남은 확인 | 두 경로의 문자·표·수식·읽기 순서 품질 비교 |
| 이후 단계 | 청킹·임베딩·인덱스·검색·리랭킹·Recall@3 평가는 미구현 |

이전 PyMuPDF 실행은 8개 문서·24페이지 기록이다. 현재 27페이지나 새 모델 파이프라인의 검증 결과로 취급하지 않는다.
선정 목록은 [configs/selection.json](configs/selection.json), 문서별 관찰은 [DATA_SPEC.md](docs/DATA_SPEC.md)에 기록했다.
`3M_2018_10K`의 대상은 8·9·15페이지로 정정했다. 기존 VL 실행의 7·8·15페이지 기록은 보존하고,
9페이지만 추가할 때는 [configs/selection_3m_page9.json](configs/selection_3m_page9.json)을 사용한다.
추가 결과는 기존 실행의 `documents/<문서 ID>/`에 보관했다. 이 문서의 `review_vl.pdf`는
8·9·15페이지 통합 검토본이며, 페이지별 bbox는 각각 6·19·18개다. 별도 추가 실행 폴더는 남기지 않았다.
추가 실행 조건은 `page_00009_run.json`, 현재 검토 대상은 `document.json`과 `review_pages_8_9_15.json`에 기록했다.
실행 루트의 `run.json`·`summary.json`은 최초 실행 기록으로 보존하므로 추가 실행의 전체 요약으로 읽지 않는다.
페이지 번호는 PDF 뷰어의 첫 페이지를 1로 센다. 실제 데이터셋 revision·공식 ID·관련성 라벨 매핑은 미확인이다.

PP-StructureV3 비교 경로를 다시 복구했다. 이전 27페이지 실패 기록과 이번 재실행을 구분한다.
두 결과를 자동으로 합치거나 한 파서를 정답 판정기로 쓰지 않고 원본 PDF와 대조한다.

## 구조와 코드 읽는 순서

```text
.
├─ configs/selection.json
├─ parsing/
│  ├─ pdf_source.py        # 1. 선택 목록·HTTP 입력
│  ├─ pdf_parser.py        # 2. PDF 추출·렌더링·좌표 변환
│  ├─ model_result.py      # 3. 공통 추론·JSON·영역 변환
│  ├─ v1_parser.py         # 4. PaddleOCR-VL 설정과 호출
│  ├─ structure_parser.py  # 5. PP-StructureV3 설정과 호출
│  ├─ review_pdf.py        # 6. 원문 복사본 위 bbox 표시
│  ├─ result_store.py      # 7. JSON·PDF·PNG 저장
│  ├─ run.py               # 8. 실행 진입점
│  └─ __init__.py
├─ docs/
│  ├─ DATA_SPEC.md
│  ├─ PROJECT_PLAN.md      # 설계·기술 선정 근거·실험 계획
│  └─ RAG_LEARNING_PLAN.md # 기본 학습 커리큘럼
├─ requirements.txt
└─ outputs/
```

실행은 `run.py`에서 시작한다. PyMuPDF로 입력을 준비하고 선택한 파서 하나로 페이지 이미지를 파싱한다.
VL과 Structure는 별도 프로세스·결과 폴더로 실행해 출력과 모델 메모리를 구분한다.

### 모듈 호출과 입출력 흐름

다음은 `run.py`의 호출 구조다. 파서별 설정과 초기화는 각 어댑터에서 담당하고,
이미지 추론·원본 JSON·공통 영역 정리는 `model_result.py`에서 처리한다.

```text
run.py
  │
  ├─ pdf_source.py
  │    선택 목록 검증 + HTTP 다운로드
  │              ↓ PDF bytes + 페이지 목록
  ├─ pdf_parser.open_pdf() / prepare_page()
  │    텍스트층·이미지 배치·페이지 정보
  │    BGR 이미지·PNG·좌표 변환 행렬
  │              ↓ BGR 페이지 이미지
  ├─ 선택한 파서 하나
  │    ├─ v1_parser.py        : PaddleOCR-VL
  │    └─ structure_parser.py : PP-StructureV3
  │              ↓ 선택한 파이프라인과 입력 이미지
  │    model_result.py에서 추론·공통 결과 정리
  │              ↓ 모델 원본 JSON + 이미지 좌표가 있는 영역
  ├─ pdf_parser.add_pdf_coordinates()
  │    영역에 회전 전 PDF 좌표 추가
  │              ↓ 원본 참조 + 영역·내용·PDF 좌표
  ├─ review_pdf.py
  │    원본 페이지 복사본에 bbox·번호·내용 주석 표시
  │
  └─ result_store.py
       원본 PDF·PNG·페이지 JSON·검토 PDF·실행 요약 저장
```

이 그림은 모듈의 책임과 데이터 흐름을 나타낸다. 저장은 마지막에 한 번만 수행하지 않는다.
원본 PDF·PNG·페이지 JSON은 준비되는 대로 저장하고, 문서 처리가 끝나면 검토 PDF와 문서 요약을 남긴다.
전체 실행이 끝나면 `summary.json`을 저장한다.

## 실행 환경

- 개인 Coder 컨테이너: CPU 4코어·RAM 16GB. 공용 서버의 GPU·VRAM 할당은 미확인이다.
- 모델 없는 검증 환경: Windows·PowerShell·Python 3.14.8.
- 기존 VL 실제 실행 환경: Windows·Python 3.12.14·PaddleOCR 3.7.0·PaddleX 3.7.2·PaddlePaddle 3.3.1 CPU.
- [requirements.txt](requirements.txt)에 의존성을 기록했다. GPU 실행은 미검증이며, GPU에는 대응 Paddle 패키지와 드라이버가 필요하다.

아래 명령은 프로젝트 루트의 Bash 기준이다. Windows에서는 Git Bash나 WSL 환경에 맞춰 Python 경로를 확인한다.
실제 파싱 전에 PDF 폴더를 제공하는 HTTP 서버와 추론 환경을 준비해야 한다.
기존 PDF 주소는 `http://127.0.0.1:8080/raw/mmdocir_data/extracted/doc_pdfs/`이며,
`127.0.0.1`은 실행 중인 컴퓨터·컨테이너 자신을 뜻한다.

## 파싱 실행

### 실행 전 확인

`parser_options(device, cpu_threads)`는 모델을 로딩하지 않고 설정 딕셔너리를 반환한다.
`build_parser(device, cpu_threads)`는 이 설정으로 모델을 초기화한다.
VL의 JSON 목록 변환과 시간 측정은 공통 모듈을 사용한다. 기존 VL 모델 추론 기록은 있으나 내용의 정확성은 별도 검토해야 한다.

필요한 패키지와 버전은 [requirements.txt](requirements.txt)에 기록했다.
선택한 Python·운영체제·아키텍처에서 패키지를 지원하는지 확인하고, 의존성 준비와 파싱 실행에 같은 Python을 사용한다.

PDF 서버도 먼저 준비한다. `--base-url`과 문서명을 합친 URL에서 PDF 파일을 직접 읽을 수 있어야 한다.
로그인 화면이나 HTML 페이지를 반환하는 주소는 PDF 입력으로 사용할 수 없다.
설치 방식과 Python 지원 범위는 [PaddleOCR-VL 공식 실행 안내](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/PaddleOCR-VL.html)를 참고한다.

### 실행 명령

도움말은 모델 로딩·다운로드·결과 파일 생성을 하지 않는다.

```bash
python -B -m parsing.run --help
```

주 파싱 경로의 실행 예시다. 모델 초기화 시 다운로드·캐시가 생길 수 있고, 원본 PDF를 HTTP로 읽어 새 출력 폴더를 만든다.
같은 출력 폴더가 이미 있으면 덮어쓰지 않고 실패한다. CPU 예시는 처리 속도나 16GB 내 동작을 보장하지 않는다.

```bash
python -B -m parsing.run \
  --base-url http://127.0.0.1:8080/raw/mmdocir_data/extracted/doc_pdfs/ \
  --parser vl --device cpu --dpi 144 \
  --output-dir outputs/parsing_vl_01
```

선정한 문서의 전체 페이지를 처리하려면 `--all-pages`를 붙인다. 문서 목록과 원본 선택 설정은 바뀌지 않는다.
중단 후 자동 재개는 미구현이며 다시 실행할 때 새 출력 폴더를 지정한다.

PP-StructureV3는 다음 명령으로 실행한다. 문자 OCR은 PP-OCRv6 medium,
수식은 비교 후보인 `PP-FormulaNet_plus-L`을 사용한다. 이 선택이 VL보다 정확하다는 실측 결론은 아니다.

```bash
python -B -m parsing.run \
  --base-url http://127.0.0.1:8080/raw/mmdocir_data/extracted/doc_pdfs/ \
  --parser structure --formula-model PP-FormulaNet_plus-L \
  --device cpu --cpu-threads 4 --dpi 144
```

## 결과 확인

실행 폴더는 다음 구조로 저장된다. 저장소에는 `outputs/`의 JSON·PNG만 올리고,
PDF 파일(`source.pdf`, `review_*.pdf`)은 `.gitignore`의 `*.pdf` 규칙에 따라 로컬에만 둔다.

```text
outputs/<실행명>/
├─ run.json                  # 환경·설정·코드·선정 목록 해시
├─ selection.json            # 입력 선택 목록 복사본
├─ summary.json              # 문서·페이지 상태와 오류
└─ documents/<문서명 SHA-256>/
   ├─ source.pdf             # HTTP로 받은 원본 bytes
   ├─ document.json          # 원본 해시·페이지와 결과 경로
   ├─ page_00009.png         # 페이지 렌더링
   ├─ page_00009.json        # 원본 추출·모델 출력·영역·좌표
   └─ review_vl.pdf          # Structure 실행이면 review_structure.pdf
```

먼저 `summary.json`에서 실패·부분 실패를 확인하고, 페이지 JSON과 검토 PDF를 대조한다.
`ok`·`empty`는 처리 상태이며 정확성·완전성 판정이 아니다.
검토본의 파란 테두리는 모델 bbox, 숫자는 모델 결과 목록의 순번이다. 주석에 요소 ID·추출 내용·JSON 경로를 남긴다.
번호가 올바른 읽기 순서를 보장하지는 않는다.

문서별 `review_vl.pdf` 또는 `review_structure.pdf`에서 선택한 파서의 영역과 내용을 원본과 대조한다.
비교할 때 원본 PDF 해시·페이지·DPI·PNG 해시가 같은지 확인한다.
페이지 PNG에는 bbox를 덧그리지 않는다. bbox는 원본 페이지를 복사한 검토 PDF에 표시한다.
표시 단위는 모델이 반환한 텍스트·표·수식·그림 등의 레이아웃 영역이다. 단어별·표 셀별 상자를 따로 그리는 기능은 포함하지 않는다.
이미지 픽셀 좌표와 PDF 좌표는 페이지 JSON의 `parsing.blocks`에 함께 보존한다.

## 문서와 작업 방식

| 문서 | 역할 |
|---|---|
| [PROJECT_PLAN.md](docs/PROJECT_PLAN.md) | 모델·프레임워크 선정 근거, 내부 구조·대안, 세 채널 결합·리랭킹·평가 설계, 다음 실험 |
| [DATA_SPEC.md](docs/DATA_SPEC.md) | 선정 문서·페이지·관찰 특성 |
| [RAG_LEARNING_PLAN.md](docs/RAG_LEARNING_PLAN.md) | 별도로 보존하는 기본 학습 커리큘럼 |
| [AGENTS.md](AGENTS.md) | 탑다운 학습, 코드·주석·글쓰기, 파일 변경·브랜치·커밋 규칙 |

현재 범위의 전체 코드를 먼저 보고 주석을 따라 이해·실행·수정한다.
전체 데이터 흐름과 실제 코드의 입력·출력 예시를 먼저 확인하고, 상세 설명은 실행 진입점부터 호출 순서대로 나눠 진행한다.
실제 실행 결과와 설명용 예시는 구분한다.
변경은 main에서 만든 브랜치에서 작업하고 GitLab 머지 리퀘스트의 스쿼시 머지를 따른다.
원격 주소는 임의로 바꾸지 않으며 명시적인 승인 전에는 푸시를 포함한 원격 쓰기를 하지 않는다.
