# 파서별 문제와 공통 파싱 문제

최초 정성 검토: 2026-10-30. 설치 패키지·저장 결과 추가 확인과 분류 정리: 2026-11-02.
최초 검토 기준 커밋: `3e54d62`. 추가 코드 확인 기준: `dcd6d29`.
기존 정성 관찰, 저장 결과 재검사, 설치된 함수의 모의 입력 검사를 구분한다.

```text
PDF → 파싱 → 원본 대조·품질 검사〔현재〕 → 청킹 → 임베딩 → 인덱스 → 검색·평가
```

현재 목표는 파싱 출력이 원문의 내용·구조·위치를 보존하는지 확인하는 것이다.
파싱에 실패한 영역을 다음 청킹 단계에 그대로 넘기지 않도록 실패 유형과 근거를 정리한다.
2026-10-30 정성 검토는 저장된 결과와 원본의 대조였다.
2026-11-02에는 로컬 `.venv`에 패키지를 설치해 결과 객체 API와 일부 함수를 실행 확인했다.
모델 가중치를 사용하는 재추론, 파싱 코드 수정, 출력 교정, 검색 품질 측정은 수행하지 않았다.

이 검토 절차를 VL·Structure 뒤에 적용하는 설계는 [품질 검증 단계 구현 계획](../../planning/PARSING_VALIDATION_PLAN.md)에 정리했다.
이 문서는 관찰과 근거, 계획 문서는 향후 입력·출력·자동화 범위와 완료 기준을 담당한다.
이후 첫 규칙 검사를 파일로 구현해 실행한 현황은 계획 문서 7절과 README의 규칙 검사 절에 기록했다.
아래 최초 정성 검토와 당시의 개선 제안을 실제 규칙 검사·모델 재실행 결과와 구분한다.

문제는 3절에서 **VL → Structure → 공통 인식 문제 → PDF 텍스트층 추출 → 저장·변환** 순서로 읽는다.
4절은 발생 경로와 이미지 API, 5절은 다음 개선 실험을 설명한다.
여기서 모델별 구분은 두 파이프라인의 출력 기준이다. Structure 안의 문자 OCR·표·수식·차트 중
어느 모델이 최초 원인인지 확인하지 못한 오류를 PP-OCRv6의 단독 결함으로 단정하지 않는다.

## 1. 평가 범위와 실행 기록

- [선정 목록](../../../configs/selection.json)의 9개 문서·27페이지를 비교했다.
- 페이지 번호는 PDF 첫 페이지를 1로 센 순번이며 인쇄된 쪽 번호와 구분한다.
- VL 결과: [parsing_vl_20261027T015138481163Z](../../../outputs/parsing_vl_20261027T015138481163Z).
- Structure 결과: [parsing_structure_20261028T232222203813Z](../../../outputs/parsing_structure_20261028T232222203813Z).
- VL의 최초 실행은 3M 7·8·15페이지를 포함한다. 현재 비교는 8·9·15페이지이며,
  추가된 9페이지의 별도 실행 기록을 사용한다. 과거 7페이지는 비교에서 제외했다.
- VL 루트의 `run.json`·`summary.json`은 최초 실행 기록이다. 추가 실행을 포함하는 새 요약으로 해석하지 않는다.
- 실제 실행 기록의 환경은 Windows·Python 3.12.14·PaddleOCR 3.7.0·PaddleX 3.7.2·
  PaddlePaddle 3.3.1 CPU·PyMuPDF 1.28.2·NumPy 2.3.5다. 페이지 렌더링은 144 DPI다.
- 이번 읽기 검사 환경은 macOS·기본 셸 zsh·Bash 명령·Python 3.12.10이다.
- 두 실행 모두 `model_weight_revision`은 `null`이다. 모델 이름과 실제 가중치 revision 고정은 별개다.
- 특성별 목적 표본이다. 이 결과를 전체 MMDocIR의 오류율이나 파서의 일반적인 우열로 확대하지 않는다.

### 구조·무결성 검사 결과

| 검사 | 확인 결과 |
|---|---|
| 비교 입력 | 27쌍의 원본 PDF 해시가 서로 같고 로컬 원본 PDF bytes의 SHA-256과 일치 |
| 페이지 이미지 | 27쌍의 기록된 PNG 해시·페이지 형상·native 추출 결과 일치. 각 PNG의 실제 해시도 기록과 일치 |
| 현재 비교 페이지 상태 | VL `ok` 26·`partial` 1, Structure `ok` 27 |
| 공통 블록 수 | VL 445개, Structure 384개. 블록 수는 정확도 지표가 아님 |
| 요소 ID | 각 실행의 현재 비교 범위에서 중복 없음 |
| 좌표 계산 | 저장된 이미지→PDF 행렬로 재계산한 bbox가 저장된 PDF bbox와 일치 |
| 내용이 빈 블록 | VL: text 23·image 1·vision_footnote 1. Structure: image 7 |
| 라이브러리 반환값→공통 블록 | 현재 54개 페이지 JSON에서 내용과 이미지 bbox의 변경 없음 |
| 이미지 참조 | 설명서 VL 표의 HTML에 이미지 경로 4개가 있으나 대응 파일이 문서 출력 폴더에 없음 |

빈 문자열은 누락 확정이 아니다. 병합 그룹의 후속 블록이거나 글자가 없는 이미지일 수 있다.
좌표 계산 일치도 그 좌표 안에 출력 문장이 실제로 존재한다는 뜻은 아니다.
VL Toyota 21페이지의 `partial`은 한 bbox가 PDF 오른쪽 경계를 약 0.11pt 넘은 경고다.
이미지 경계 안의 좌표가 PDF로 변환되면서 생긴 작은 초과이며 내용 오류의 크기를 나타내지 않는다.

## 2. 평가를 수행한 방법과 한계

2026-10-30 검토에서는 저장된 27개 페이지 PNG를 `view_image`로 열어 이 대화의 이미지 이해 기능으로 읽었다.
Python으로 페이지 JSON의 `content`, `label`, bbox, 원본 텍스트층과 HTML 구조를 조회하고 두 파서를 비교했다.
로컬 원본 PDF 해시를 확인하고 일부 원본 텍스트는 pypdf로도 읽었다.
별도 OCR·VLM을 새로 실행하거나 학습된 자동 평가 모델의 점수를 받지는 않았다.
읽기 검사에 사용한 일회성 Python 코드는 저장소의 검증 프로그램으로 추가하지 않았다.

판정은 다음 근거를 함께 사용했다.

1. **원본과 출력의 직접 모순:** 원문은 두 열인데 한쪽 bbox에 양쪽 문장이 들어감, 원문 표의 열이 출력에서 빠짐.
2. **출력 자체의 이상:** 같은 단어의 긴 반복, 연속 번호 나열, 영어 그림에 무관한 문자가 나타남.
3. **위치와 내용의 대응:** 출력 문장이 그 bbox 안의 원문인지 확인. 페이지 전체에 글자가 존재하는 것만으로 통과시키지 않음.
4. **두 파서의 불일치:** 재검토 대상을 고르는 신호로 사용. 어느 파서도 정답으로 가정하지 않음.
5. **라이브러리·프로젝트 경계 추적:** 문제 내용이 `parsing.raw`에도 있는지 확인해 최초로 관찰되는 실패 구간을 좁힘.

이미지를 읽는 AI의 판단도 오류가 날 수 있다. 특히 작은 글자·회전 문자·복잡한 그래프의 정밀 수치는
시각 판단만으로 정답을 만들지 않는다. 별도의 정답 전사본·셀 매핑·좌표 라벨이 없어
CER/WER, 표 구조 정확도, 문장 위치 정확도, 전체 통과율은 측정하지 않았다.
검토 내용은 정성 평가와 일부 구조 검사이며 표준 벤치마크 결과가 아니다.
[OpenAI 공식 이미지 이해 문서](https://developers.openai.com/api/docs/guides/images-vision#limitations)도 작은 글자·회전·그래프·정밀 위치 판단의 한계를 설명한다.
이 일반 안내로 현재 대화 모델의 비공개 이미지 전처리·네트워크 구조를 추정하지 않는다.

## 3. 문제 분류

### 3.1 PaddleOCR-VL에서 관찰된 문제

| 문제 | 실제 관찰과 근거 | 원인 확인 수준 |
|---|---|---|
| 비정상 반복·원문에 없는 내용 | Toyota 2페이지 블록 20에 무관한 국제학회 문장과 `China` 2,029회 반복. 7페이지 도식에서도 반복 관찰. [2페이지 JSON][toyota2-vl] · [PNG][toyota2-img] | 반복이 필터에 남는 경로는 추가 확인. 최초 생성 원인은 미확인 |
| 문장과 bbox의 귀속 불일치 | 게슈탈트 8페이지 왼쪽 블록 1에 Closure·Continuity가 함께 들어가고 오른쪽 블록 2는 비어 있음. 두 블록의 `group_id=1`. 뉴스와 NUS 22페이지에도 영역 간 내용 병합 관찰. [게슈탈트 JSON][gestalt-vl] · [PNG][gestalt-img] | [영역 병합 경로](#문장bbox-불일치의-유력한-발생-경로)와 일치. 2026-11-11 병합 해제 재파싱에서 병합 그룹·빈 text 블록이 사라짐. 정답 대조는 미수행([재파싱 결과](2026-11-11_vl-merge-off-reparse.md)) |
| 단어 오인식·정보 누락 | 3M 9페이지 `Elected`를 `Executed`로 인식. NUS 9페이지 전공 조합 일부 누락 | 결과에서 확인. 세부 발생 단계는 미확인 |
| 표의 열·헤더 손상 | DETR 13페이지 Table 3의 헤더 대응과 마지막 Δ열 손실. 3M 15페이지에도 여분 빈 열·헤더 문제. [DETR JSON][detr13-vl] · [PNG][detr13-img] | 표 인식 결과의 손상 확인. 검출·입력 crop·생성 중 최초 원인은 미확인 |
| 그림 내부 문자 오인식 | RAG 3페이지의 `dismissal`을 `原始`로 출력. [JSON][rag3-vl] · [PNG][rag3-img] | 출력과 원문 불일치 관찰. 작은 문자 인식과 생성 과정의 기여는 분리하지 못함 |
| 근거가 맞지 않는 그래프 수치표 | 슬라이드 15페이지의 생성 수치표가 원본 막대 변동과 맞지 않음 | 정성 관찰. 정밀 수치 정답과 전체 오류율은 미측정 |

2026-11-02 설치된 PaddleX 3.7.2의 `truncate_repetitive_content()`에 Toyota 2페이지의
저장 문자열 14,355자를 다시 넣었을 때 문자열이 그대로 반환됐다. 줄바꿈 2개가 있어
단일 행의 구절·문자 반복 검사에서 제외되고, 비어 있지 않은 줄 수가 기본 임계값 10보다 적어
줄 반복 검사에서도 제외된다. 이 확인은 이미 저장된 문자열을 함수에 재입력한 결과이며,
실제 추론 당시의 후처리 전 출력이나 최초 반복 생성 원인을 확보한 것은 아니다.

### 3.2 PP-StructureV3에서 관찰된 문제

문자 검출·인식은 PP-OCRv6 medium, 지정한 수식 모델은 PP-FormulaNet_plus-L이다.
표·레이아웃·차트 모듈이 별도로 연결되므로 아래 문제를 모두 문자 OCR 모델의 오류로 묶지 않는다.

| 문제 | 실제 관찰과 근거 | 원인 확인 수준 |
|---|---|---|
| 표 값의 행 이동 | 3M 15페이지 `Same period last year`의 `0.85`를 다음 행으로 이동. [JSON][3m15-st] · [PNG][3m15-img] | 값이 남아 있어도 행 대응이 손상됨. 문자 인식과 셀 배정 중 최초 원인은 미확인 |
| 여러 행을 한 셀에 합침 | RAG 6페이지 SANTA 등 하단 항목이 독립 행 대신 셀 내용에 남음. HTML `tr` 수는 VL 72·Structure 62. [JSON][rag6-st] · [PNG][rag6-img] | 글자 누락과 행 구조 손상을 구분해야 함. 행 개수 차이만으로 정확도 판정 불가 |
| 헤더 병합·기호 오인식 | DETR 13페이지 Table 4의 헤더가 합쳐지고 일부 체크를 `J`로 인식. [JSON][detr13-st] · [PNG][detr13-img] | 표 구조와 문자 인식 양쪽의 오류 가능성. 모듈별 원인 분리는 미수행 |
| 표 헤더 누락 | 3M 인명·직책 표에서 `Age` 헤더 누락 | 기존 정성 관찰. 검출·인식·표 조립 중 최초 원인은 미확인 |
| 그림 속 문장의 영역 혼합 | RAG 3페이지에서 서로 다른 상자의 문장을 섞음. NUS 9페이지에서도 조합별 내용 혼합. [RAG JSON][rag3-st] · [PNG][rag3-img] | 영역별 내용 대응 손상. VL의 병합 로직과 같은 원인이라고 확인한 것은 아님 |
| 아이콘 셀의 시각 정보 누락 | UHD 설명서 8페이지 표의 아이콘 셀이 비어 있음 | 문자가 없는 셀일 수 있으므로 단순 OCR 실패와 구분. 아이콘과 동작의 연결이 불완전함 |

Structure의 과거 oneDNN 실행 예외는 인식 품질 문제와 별개다.
현재 어댑터는 그 예외를 피하려고 `enable_mkldnn=False`를 지정하고, 저장된 비교 실행은 27페이지 `ok`다.
실행 완료는 표·문자·수식의 정확성 보장이 아니다.

### 3.3 두 파서에서 공통으로 관찰된 인식 문제

| 공통 문제 | 근거 | 확인 범위와 한계 |
|---|---|---|
| 수식의 의미 구조 손상 | DETR 5페이지 식 (2)의 `\hat{b}_{\hat{\sigma}(i)}`를 두 파서 모두 `\hat{b}_{\hat{\sigma}}(i)` 형태로 출력. [VL][detr5-vl] · [Structure][detr5-st] · [PNG][detr5-img] | LaTeX 존재와 첨자 의미 보존은 다름. 두 파서의 내부 원인이 같다는 뜻은 아님 |
| 복잡한 표의 구조 손상 | DETR 13페이지에서 VL은 Table 3, Structure는 Table 4에 서로 다른 손상. [VL][detr13-vl] · [Structure][detr13-st] | 같은 표에서 똑같이 실패했다는 분류가 아니라, 두 경로 모두 표 구조 오류가 있다는 뜻 |
| 도식 누락·영역 혼합 | Toyota 21페이지에서 양쪽 파서에 관찰 | 문자 인식뿐 아니라 영역 구분·내용 배정도 검토 필요 |
| 그림 내부 정보의 불완전한 복원 | RAG 3페이지에서 VL은 단어 오인식, Structure는 상자별 문장 혼합. [VL][rag3-vl] · [Structure][rag3-st] · [PNG][rag3-img] | 같은 입력에서도 실패 방식이 다름. 어느 한 출력을 정답으로 사용할 수 없음 |

작은 글자·회전 문자·장식 글꼴·축·범례·화살표·조직도 관계는 추가 검토 대상이다.
문서별 관찰만 있고 파서별 증거를 나누지 않은 항목은 양쪽에서 같은 오류가 확인됐다고 확대하지 않는다.
캡션 평가는 원문에 인쇄된 캡션의 추출이며, 그림의 새 설명문 생성 품질을 평가한 것이 아니다.

### 3.4 PDF 텍스트층 직접 추출의 문제와 제약

이 절의 텍스트 파싱은 PyMuPDF의 `native` 경로다. 이미지 기반 모델의 `parsing` 결과와 구분한다.
현재 `pdf_parser.py`는 PDF의 블록·줄·span을 읽어 `native.raw`에 보존하고,
줄별 문자열을 연결해 `native.text`를 만든다.

| 문제·제약 | 저장 결과 또는 코드에서 확인한 내용 | 해석 |
|---|---|---|
| 이미지 속 문자를 텍스트층에서 얻지 못함 | 현재 선정 27페이지의 `native` 상태는 `ok` 20·`empty` 7·`failed` 0 | `empty`는 공백 이외 문자를 얻지 못한 상태다. 추출 예외나 페이지에 글자가 없다는 판정이 아님 |
| 수식의 공간 구조가 평문에서 풀림 | DETR 5페이지의 합 기호·위아래 첨자가 여러 줄 문자로 나타남. [페이지 JSON][detr5-vl]의 `native.text` | 문자열 연결만으로 수학적 구조를 보존하기 어려움. span 좌표는 별도 raw에 남음 |
| 읽기 순서를 보장하지 않음 | `get_text("dict", sort=False)`로 추출 목록과 좌표를 보존 | 다단 문서의 자연스러운 읽기 순서를 검증했다는 뜻이 아님. 모든 페이지의 순서가 틀렸다는 관찰도 아님 |
| 평문에 표의 논리 구조가 없음 | 현재 `native.text` 생성은 줄·span 문자열 연결이며 행·열·병합 헤더를 구성하지 않음 | 문자와 좌표가 남아 있어도 표의 논리 구조 복원은 별도 작업 |

텍스트층이 빈 페이지는 슬라이드 `0e94…`의 4·6·10·15페이지와
`competitiveoutcomes…`의 16·29·40페이지다. 과거 24페이지 실행에서 기록한 빈 페이지 6개와 구분한다.
선택·복사가 가능하다는 사전 관찰은 현재 추출의 완전성·순서·수식 정확성을 검증한 결과가 아니다.

현재 두 모델에는 `native.text`가 아니라 페이지 전체 이미지가 들어간다.
따라서 native 추출의 손상을 VL·Structure 인식 오류의 직접 원인으로 연결하지 않는다.

### 3.5 프로젝트 공통 변환·저장의 문제와 제약

| 항목 | 확인한 내용 | 분류 |
|---|---|---|
| 라이브러리 JSON → 공통 블록 변환 | 54페이지·829개 블록에서 content·이미지 bbox의 변경 0개 | 위 인식 손상은 현재 공통 블록 변환 이전 결과에 이미 존재 |
| 이미지 파일 참조의 불완전성 | UHD 설명서 VL 표 HTML의 이미지 경로 4개에 대응 파일이 없음 | 결과 이미지 저장 누락. OCR 문자 정확도와 별개. 2026-11-08부터 `markdown_images`를 페이지별 `imgs/`에 저장하도록 수정했으나 재실행 전이라 기존 결과는 그대로다 |
| 결과 객체의 시각화·crop 미보존 | 2026-11-08 이전 `model_result.py`는 `first.json`만 읽었다. 이후 `markdown_images`(그림 블록·표 안 그림 crop)는 저장하지만 `img` 시각화와 병합된 VLM 입력 crop은 여전히 저장하지 않음 | 전체 페이지 PNG는 별도 보존하지만 결과 객체의 이미지와 구분해야 함 |
| 관계 표현 부족 | 공통 블록에 캡션↔그림·수식↔번호·페이지 간 표의 명시적 연결 없음 | 프로젝트 표현 설계의 한계. 라이브러리의 모든 결과에도 관계가 없다고 단정하지 않음 |
| 상태와 품질의 혼동 가능성 | 문자열이 있으면 파싱 `ok`. 원문 사실성·문장 위치·셀 의미 대응은 검사하지 않음 | 처리 성공과 품질 판정을 분리해야 함 |

VL Toyota 21페이지의 약 0.11pt 경계 초과는 좌표 경고다.
문장 전체가 다른 bbox에 붙는 내용 귀속 문제와 같은 원인·심각도로 묶지 않는다.

### 문서별 관찰 요약

| 문서·PDF 페이지 | 확인한 내용 |
|---|---|
| 뉴스 `0b854…` 1·3·11 | 제목·본문·사진 설명은 상당 부분 읽힘. VL은 단을 넘는 문장을 한 블록에 합치고 다른 블록을 비움. 생물학 도식의 글자는 일부 복구하지만 화살표 관계는 구조화하지 않음 |
| 슬라이드 `0e94…` 4·6·8·10·15 | 4페이지 작은 연도·임무명 오인식, 6페이지 우주개발 도표의 비정상 수치표, 8페이지 VL 문장–bbox 불일치. 10페이지 지도 범례는 일부 복구. 15페이지 VL의 수치표는 원본 막대 변동과 맞지 않아 신뢰 불가 |
| 3M 8·9·15 | 8·9페이지 인명·직책 표는 상당 부분 읽힘. Structure의 Age 헤더 누락, VL 9페이지 Elected→Executed 오인식. 15페이지는 VL이 수치·행 대응을 더 잘 유지하나 여분 빈 열과 헤더 문제 존재 |
| DETR `2005.12872v3` 5·13 | 5페이지 수식 첨자 결합 오류. 13페이지 Table 3은 Structure가 더 잘 보존하고 Table 4는 VL이 더 잘 보존. 그림 내부 zebra 라벨 복구는 불완전 |
| RAG `2312.10997v5` 3·4·6 | 3·4페이지 캡션은 대체로 추출. 그림 내부의 단어·읽기 순서·반복 오류 존재. 6페이지 Structure는 하단 행들을 셀에 뭉쳐 넣음 |
| competitiveoutcomes 16·29·40 | 이미지뿐인 페이지에서도 OCR 결과가 생김. 16페이지 그래프 수치 복원은 신뢰 불가. 29페이지 주요 라벨·캡션은 일부 복구하나 축·곡선 관계는 보존하지 못함. 40페이지 잡지 표지의 장식 글자·숫자 오류 |
| UHD 설명서 8 | 본문과 표의 상태·응답 문구는 상당 부분 읽힘. 아이콘과 동작의 연결 불완전. Structure 표의 아이콘 셀은 비어 있고 VL의 이미지 파일 참조는 연결되지 않음 |
| Toyota 2·7·21·32·34 | 2페이지 미리보기와 7페이지 원뿔 도식에서 VL 비정상 반복. 21페이지 양쪽 파서의 도식 누락·영역 혼합. 32페이지 조직도·연표 관계 손상, 34페이지 일부 표의 병합셀과 각주 확인 필요 |
| NUS 9·22 | 9페이지 VL은 전공 조합 일부를 누락, Structure는 글자를 더 남기지만 영역별 조합이 섞임. 22페이지 VL은 상단 두 단을 한 bbox에 합침. 사진 설명은 일부 추출되지만 연결 라벨·순서 불안정 |

이 표는 페이지별 모든 문자·셀을 전수 채점했다는 뜻이 아니다. 확인한 대표 실패와 성공을 요약한다.

### 대표 증거

| 사례 | 원본 대조와 JSON 관찰 | 근거 |
|---|---|---|
| Toyota 2페이지 반복 | VL의 블록 20에서 무관한 국제학회 문장과 `China` 2,029회 반복. 페이지 상태는 `ok` | [VL JSON][toyota2-vl] · [PNG][toyota2-img] |
| 게슈탈트 8페이지 위치 불일치 | VL 블록 1의 bbox는 왼쪽인데 Closure·Continuity가 함께 들어감. 오른쪽 블록 2는 빈 문자열이며 두 블록의 raw group_id는 1 | [VL JSON][gestalt-vl] · [Structure JSON][gestalt-st] · [PNG][gestalt-img] |
| DETR 13페이지 Table 3 | VL은 헤더 대응과 마지막 Δ열을 잃음. Structure는 다단 헤더와 마지막 열을 더 잘 보존 | [VL JSON][detr13-vl] · [Structure JSON][detr13-st] · [PNG][detr13-img] |
| DETR 13페이지 Table 4 | Structure는 헤더를 합치고 일부 체크를 J로 읽음. VL은 표 대응을 더 잘 유지 | 위 DETR 13페이지 근거와 동일 |
| 3M 15페이지 수치 이동 | 원문 Same period last year의 0.85를 Structure는 다음 행으로 이동. VL은 대응을 유지 | [VL JSON][3m15-vl] · [Structure JSON][3m15-st] · [PNG][3m15-img] |
| DETR 5페이지 식 (2) | 두 파서 모두 원문의 `\hat{b}_{\hat{\sigma}(i)}`를 `\hat{b}_{\hat{\sigma}}(i)` 형태로 출력. LaTeX 생성과 수학적 의미 보존은 다름 | [VL JSON][detr5-vl] · [Structure JSON][detr5-st] · [PNG][detr5-img] |
| RAG 3페이지 그림 내부 | 원문 dismissal을 VL은 原始로 출력. Structure는 여러 상자의 문장을 섞음. 캡션 추출은 상대적으로 양호 | [VL JSON][rag3-vl] · [Structure JSON][rag3-st] · [PNG][rag3-img] |
| RAG 6페이지 표 하단 | HTML tr 수는 VL 72·Structure 62. Structure에는 SANTA 등 하단 이름이 독립 행이 아닌 셀 내용에 남음. 글자 전체 누락과 행 대응 손상을 구분 | [VL JSON][rag6-vl] · [Structure JSON][rag6-st] · [PNG][rag6-img] |

## 4. 프로젝트의 파싱 과정과 평가 과정은 다르다

### 프로젝트 실행 흐름

```text
selection.json + CLI
 → run.py: 문서·페이지 선택, 선택한 파서 초기화
 → pdf_source.py: 원본 PDF bytes 읽기
 → pdf_parser.py: native 텍스트·이미지 배치 추출 + 페이지 렌더링
 → BGR 이미지 → vl_parser.py 또는 structure_parser.py
 → model_result.py: parser.predict() → 라이브러리 JSON → 공통 blocks
 → pdf_parser.py: 이미지 bbox를 유지하고 PDF 좌표 추가
 → review_pdf.py / result_store.py: 검토 PDF·PNG·JSON·요약 저장
```

native 텍스트가 있어도 모델에는 전체 페이지 이미지를 입력한다. native는 별도로 보존하는 대조 자료다.
`model_result.py`는 JSON 타입·좌표 형식·결과 개수를 검사한다. 문자열이 있으면 `ok`로 기록하지만
원문 사실성·표 의미·내용과 bbox의 대응은 검사하지 않는다.
`parsing.raw`는 라이브러리가 조립한 최종 JSON이며 VLM의 후처리 전 토큰·로짓·생성 기록은 아니다.
현재 공통 정리 단계는 원본 content와 bbox를 그대로 복사한다.

### 사용한 모델과 라이브러리 내부 흐름

| 경로 | 실행 기록으로 확인한 설정 | 공식 버전 설정의 구성 |
|---|---|---|
| VL | PaddleOCRVL, pipeline_version v1.6, native backend, 레이아웃·차트·이미지 OCR 켜짐 | PaddleX 3.7.2 기본 YAML: PP-DocLayoutV3 + PaddleOCR-VL-1.6-0.9B |
| Structure | PPStructureV3, 문자 검출 PP-OCRv6_medium_det·문자 인식 PP-OCRv6_medium_rec·수식 PP-FormulaNet_plus-L 지정 | 기본 YAML: 레이아웃 PP-DocLayout_plus-L, 차트 PP-Chart2Table, 표 분류·구조·셀 검출 모듈 |

Structure의 기본 YAML에는 표 구조 SLANeXt_wired·SLANet_plus, 셀 검출 RT-DETR-L wired/wireless 모델이 있다.
이는 배포 설정에 정의된 후보이며 각 표에서 어떤 분기가 실행됐는지까지 확인한 기록은 아니다.
프로젝트의 명시적 OCR 모델 지정은 기본 YAML의 PP-OCRv5 이름보다 우선한다.
실제 다운로드된 모든 가중치의 파일 해시·revision·실행 중 설치 소스는 확보하지 못했다.

VL은 레이아웃 영역을 자르고 필요시 합친 뒤, 영역 유형에 따라 OCR·표·수식·차트 작업을 VLM에 요청하고 결과를 조립한다.
Structure는 레이아웃, 문자 검출·인식, 표 구조·셀 배치, 수식·차트 인식을 모듈로 연결한다.
둘 다 라이브러리 안에 추가 처리가 있다. 이 처리와 이번 대화에서 수행한 결과 평가는 별개다.
모델의 학습 가중치 내부가 왜 특정 문장을 생성했는지까지 추적한 것은 아니다.

공식 자료는 설치 기록에 맞는 버전으로 확인했다.

- [PaddleX v3.7.2 VL 기본 설정](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/configs/pipelines/PaddleOCR-VL-1.6.yaml)
- [PaddleX v3.7.2 VL 실행 흐름](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/inference/pipelines/paddleocr_vl/pipeline.py)
- [PaddleX v3.7.2 영역 병합 구현](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/inference/pipelines/paddleocr_vl/uilts.py)
- [PaddleX v3.7.2 Structure 기본 설정](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/configs/pipelines/PP-StructureV3.yaml)
- [PaddleOCR v3.7.0 VL 래퍼와 병합 인자](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/paddleocr/_pipelines/paddleocr_vl.py)
- [PaddleOCR v3.7.0 Structure 래퍼](https://github.com/PaddlePaddle/PaddleOCR/blob/v3.7.0/paddleocr/_pipelines/pp_structurev3.py)

### 문장–bbox 불일치의 유력한 발생 경로

게슈탈트 VL raw에는 `merge_layout_blocks=True`와 두 텍스트 블록의 동일한 `group_id=1`이 있다.
PaddleX 3.7.2의 `merge_blocks()`는 조건에 맞는 영역의 이미지를 연결해 첫 블록의 입력으로 넣고,
나머지 그룹 블록의 이미지를 None으로 둔다. 개별 box는 그대로 남긴다.
조립 단계는 첫 블록에 인식 문자열을 넣고, 추론 입력이 없는 후속 블록에는 빈 문자열을 남길 수 있다.
이 동작은 관찰한 두 문장 병합·빈 블록·개별 bbox 유지와 일치한다.

따라서 모든 빈 블록을 OCR 누락으로 세거나 PDF 좌표 변환을 고치는 접근은 적절하지 않다.
우선 라이브러리의 병합 그룹을 보존하고 같은 이미지에서 병합을 끈 결과와 비교해야 한다.
실행 당시 설치 소스의 해시와 중간 crop은 없으므로 공개 버전 코드와 저장 결과에 근거한 발생 경로 판단이다.
병합 비활성화가 품질을 개선했다는 실측 결론은 아직 없다.

2026-11-11 병합을 끄고 같은 27페이지를 다시 파싱했다. VL의 병합 그룹 22개와 빈 text 블록 23개가 모두 사라졌고,
bbox·라벨은 그대로, 텍스트는 같은 블록 사이에서 다시 나뉘었다. 블록별 텍스트를 정답과 대조하지는 않았다.
결과와 한계는 [병합 해제 재파싱 결과](2026-11-11_vl-merge-off-reparse.md)에 정리했다.

2026-11-02 새로 설치한 PaddleOCR 3.7.0·PaddleX 3.7.2에서도 함수 동작을 확인했다.
게슈탈트 두 텍스트 bbox와 같은 크기의 모의 이미지를 `merge_blocks()`에 넣으면
첫 블록에 높이 299·너비 649의 합친 이미지가 들어가고 두 번째 블록 이미지는 `None`이 된다.
개별 bbox는 유지된다. 두 블록만 넣은 검사라 그룹 번호는 0이며, 원래 전체 페이지의 `group_id=1`과 구분한다.
이는 병합 분기의 재현이며 원본 픽셀을 사용한 모델 재추론이나 품질 개선 검증이 아니다.

### 설치된 결과 객체에서 이미지를 읽는 API

확인 환경은 macOS·Python 3.12.10의 `.venv`, PaddleOCR 3.7.0·PaddleX 3.7.2다.
두 패키지 버전을 구분한다. 결과 클래스의 이미지 구현은 PaddleX에 있다.
실제 결과 클래스에 샘플 데이터를 넣어 이미지 접근과 임시 폴더 저장을 실행했고,
모델 가중치 로딩이나 새 OCR 추론은 하지 않았다.

| 목적 | 접근 API | 반환값·조건 |
|---|---|---|
| 레이아웃 검출 시각화 | `res.img["layout_det_res"]` | 해당 결과가 제공될 때 PIL 이미지 |
| Structure의 OCR 시각화 | `res.img["overall_ocr_res"]` | PIL 이미지 |
| Structure의 읽기 순서 시각화 | `res.img["layout_order_res"]` | PIL 이미지 |
| Structure의 표 셀 표시 | `res.img["table_cell_img"]` | 표 인식이 켜져 있고 표 결과가 있을 때 PIL 이미지 |
| 문서 그림 crop 묶음 | `res.markdown["markdown_images"]` | 상대 경로 → PIL 이미지 딕셔너리 |
| 개별 블록 이미지 | `res["parsing_res_list"][i].image["img"]` | `.image`가 `None`이 아닌 블록. 경로는 `.image["path"]` |
| 시각화 파일 저장 | `res.save_to_img(save_path=...)` | 파일 생성. 여러 시각화가 있으므로 폴더 경로 사용 |
| Markdown·그림 저장 | `res.save_to_markdown(save_path=...)` | Markdown과 참조 이미지 파일 생성 |

`img`·`markdown`·`json`은 메서드가 아닌 속성이다. 기본 `res.json`은 블록 이미지 객체를 보존하지 않는다.
그림 crop 묶음과 검출 시각화는 서로 다르며, 병합된 VLM 입력 crop 전부를 반환하는 API도 아니다.
최종 결과에 남지 않는 중간 이미지는 처리 단계에서 별도로 확보해야 한다.
위 API는 라이브러리 기능 확인이며 프로젝트에 저장 기능을 추가했다는 뜻이 아니다.

근거: [VL 결과 클래스](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/inference/pipelines/paddleocr_vl/result.py),
[Structure 결과 클래스](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/inference/pipelines/layout_parsing/result_v2.py),
[이미지·Markdown 속성과 저장 메서드](https://github.com/PaddlePaddle/PaddleX/blob/v3.7.2/paddlex/inference/common/result/mixin.py).

### 2026-11-02 확인 당시의 변경 기록과 효과 측정

아래 미구현 항목의 구현 방향과 청킹 이후 확장은
[단계별 실행 기록과 전후 비교 계획](../../planning/EXPERIMENT_COMPARISON_PLAN.md)에 정리했다.
이는 2026-11-04 설계 문서이며 아래 구현 상태나 기존 실행 결과를 변경한 것은 아니다.

2026-11-05에는 공통 실행 기록·소스 diff·모델 파일·메모리·파싱 전후 평가 코드를 추가했다.
현재 사용법과 실제 지원 범위는 [실행 비교 사용법](../../planning/EXPERIMENT_COMPARISON_USAGE.md)을 참고한다.
아래 표는 추가 구현 전의 확인 기록으로 보존한다. 과거 결과에 새 메모리·revision·품질 점수를 채워 넣지 않았다.

| 항목 | 구현 상태 | 한계 |
|---|---|---|
| 실행 조건·입력 추적 | `run.py`의 환경·패키지 버전·선정 목록·코드 해시, 페이지의 PDF·PNG 해시·ID·좌표 | 실행 조건을 남기지만 Git commit·diff와 변경 이유를 자동 기록하지 않음 |
| 명시적 predict 설정 기록 | `dcd6d29`부터 두 어댑터의 `predict_options()`를 실제 호출과 `run.json` 기록에 함께 사용 | 생략한 라이브러리 기본값 전체나 모든 디코딩 적용값을 기록하는 것은 아님 |
| 시간·오류 기록 | 페이지 추론 시간, 실행·모델 로딩 시간, 실패 단계·오류 | 메모리·모델 가중치 revision·가중치 해시는 미확보 |
| 규칙 검사 | 입력 해시·ID·좌표·반복·병합 그룹·HTML 격자 검사와 별도 보고서 | 원문 의미 정확도는 미측정. `quality_accuracy`는 `null` |
| 실행 간 변경 비교 | 미구현 | 설정·소스·입력의 전후 차이를 연결하는 자동 보고서가 없음 |
| 수정 효과의 품질 비교 | 미구현 | 복구한 문장·새 누락·문장 위치·표 셀 대응의 전후 점수가 없음 |

2026-11-02 현재 선정 목록으로 규칙 검사를 다시 실행한 결과는 VL 검토 대상 13/27페이지,
Structure 4/27페이지다. VL 신호는 병합 그룹 22·긴 번호 나열 2·단어 집중 반복 1·
3단어 묶음 반복 1·출력 길이 1, Structure는 긴 번호 나열 1·표 격자 3이다.
같은 페이지에 여러 신호가 생길 수 있고, 검토 신호 수는 정확도나 파서 우열이 아니다.
기존 테스트 20개와 설치 패키지 의존성 검사를 통과했다. 모델 품질 테스트를 통과했다는 뜻은 아니다.

## 5. 개선 방법: 식별, 원인 구분, 복구를 나눠 진행

아래 절차 중 반복·병합 그룹·좌표·표 격자의 첫 규칙 검사는 구현했다.
원문 근거 정렬, 선택적 재인식·복구, 설정 변경의 전후 품질 비교는 아직 구현·재실행하지 않았다.
원본 JSON을 덮어쓰지 않고 별도 평가·파생 결과를 남긴다.
규칙의 초기 임계값은 개발용 표본에서 정하고, 최종 평가 표본에 맞춰 반복 조정하지 않는다.

### 5.1 반복·환각 식별

1. 블록별로 정규화한 단어·문자 n-gram의 반복 비율, 최장 연속 반복, 같은 줄의 중복을 센다.
2. 긴 연속 번호·빈 수치 행, 영역 크기에 비해 비정상적으로 긴 출력도 별도 신호로 남긴다.
3. 숫자·고유명사·언어가 native 텍스트 또는 독립 OCR과 크게 다르면 원본 대조 대상으로 표시한다.
4. 신호가 있어도 바로 지우지 않는다. 문서의 정상적인 반복 헤더·표 값과 구분해 원본 영역을 확인한다.
5. 확인된 손상 출력은 검색 표현에서 보류하고 원본 이미지·문서 연결을 남긴다. 단순 반복 제거로 나머지 문장까지 정확해지지는 않는다.

반복 규칙은 이상 탐지다. 반복이 없는 환각을 놓칠 수 있고 다른 OCR도 같은 오류를 낼 수 있다.
OCR confidence 역시 문장의 사실성 확률이 아니다. 명확한 원문 대조 없이 환각 확정 라벨을 붙이지 않는다.
재인식한다면 같은 영역의 해상도·모델·디코딩 설정 중 하나만 바꾸고 원문 복구량·잘림·비용을 비교한다.
길이 제한만 줄이면 정상적인 큰 표도 잘릴 수 있으므로 해결책으로 단정하지 않는다.

### 5.2 문장–bbox 대응 복구

우선 두 문제를 구분한다.

- **좌표 계산 문제:** 원점·배율·회전·CropBox 변환 오류. 좌표 변환과 원문 overlay로 확인한다.
- **내용 귀속 문제:** 여러 영역의 텍스트가 한 블록에 들어감. 현재 VL의 대표 사례는 이 유형이다.

첫 비교 실험은 동일한 게슈탈트 8페이지에서 `merge_layout_blocks=True/False`만 바꾸는 것이다.
공식 v3.7.0 래퍼는 생성자와 predict에서 이 인자를 지원한다. 2026-11-08부터 VL 어댑터의 생성자 설정은
`merge_layout_blocks=False`이고, `True` 실행은 기록 설정의 `pipeline_options`로 이 값만 덮어쓴다.
두 경우 모두 `run.json`의 `parser_options`와 실제 초기화에 같은 값이 쓰인다. raw model_settings의 실제 반영값도 검사한다.
기존 결과는 Windows에서 만들었으므로 두 조건을 같은 환경에서 다시 실행해 비교한다.
나머지 PDF·이미지·DPI·모델·디코딩 설정을 고정한다. 아직 이 실행을 수행하지 않았다.

복구 표현은 다음 순서로 설계한다.

1. raw의 `group_id`와 소속 `model_block_id`를 파생 표현에 보존한다.
2. 그룹 전체 문자열에는 구성 bbox 목록을 연결한다. 큰 사각형 하나로 합치는 것은 정밀 문장 위치 복구가 아니다.
3. 문장별 위치가 필요하면 native span 또는 독립 OCR의 줄·단어 좌표와 문자열을 정렬한다.
4. OCR 줄 사각형 Q를 블록 B에 연결할 때 `area(Q ∩ B) / area(Q)`를 후보 점수로 사용할 수 있다.
   임계값·동점·여러 블록에 걸친 줄은 개발 표본에서 검증한다. bbox만으로 문장 의미 대응을 확정하지 않는다.
5. 대응을 찾은 문장만 해당 줄들의 좌표에 연결한다. 실패·중복 매칭은 미해결로 남기고 재인식 또는 검토한다.

문자열 유사도와 공간 겹침을 함께 확인하고 이미지 crop의 원점도 기록한다.
문장 원문 복구, 위치 정확성, 새 누락, 블록 수·지연 변화를 각각 비교한다.
페이지 전체 좌표나 그룹의 union bbox로 바꾼 것만으로 문장 위치를 검증했다고 표시하지 않는다.

### 5.3 표의 행·열 대응 검증

1. 표 HTML을 파싱해 rowspan·colspan을 반영한 논리 격자로 펼친다. 태그 구문 오류·셀 겹침·예상하지 않은 빈칸을 기록한다.
2. 각 값에 행 키와 다단 헤더 경로를 연결한다. 예: `Same period last year × Three months ended / December 31, 2018 → 0.85`.
3. 원본의 셀 위치·OCR 문자열과 연결해 숫자가 다른 행·열로 이동했는지 검사한다.
   Structure raw의 `table_res_list`에 있는 cell_box_list·table_ocr_pred도 대조하되 목록 순서가 HTML 셀과 같다고 가정하지 않는다.
4. 음수 괄호·소수점·퍼센트·통화·단위·대시·빈칸·체크·이미지 셀을 각각 검증한다. 빈칸과 대시를 0으로 통일하지 않는다.
5. 반복 헤더와 이어지는 표는 문서·페이지·원본 헤더를 유지한 채 연결한다. 원본 행과 재구성 행을 구분한다.
6. 합계가 명시된 경우 보조 검산을 한다. 반올림·소계·중복 집계 규칙이 없으면 합계만으로 셀 배치를 확정하지 않는다.

직사각형 격자와 HTML 구문이 정상이어도 의미상 틀린 표일 수 있다.
현재 표본의 DETR Table 3·4, 3M 15페이지, RAG 6페이지, 아이콘 설명서 표를 서로 다른 실패 검증에 사용한다.
원본으로 확인한 작은 셀 대응 정답을 만든 뒤 셀 내용 정확도와 행·열·헤더 대응 정확도를 따로 측정한다.
복구되지 않은 표는 이미지와 캡션을 보존하고 구조화 수치 검색에 넣을지는 보류한다.

### 5.4 다음 실행의 입력과 출력

| 구분 | 필요한 정보 |
|---|---|
| 입력 | source_sha256, page_id, PNG 해시·DPI, 원본 raw·blocks, 실행 설정·모델 revision |
| 평가 기록 제안 | element_id, 검사 규칙·버전, 관찰값·임계값, 원본 근거, 의심/확인/미해결 판정 |
| 복구 결과 제안 | 원본 element_id 목록, 파생 content·위치 목록, 복구 방법·버전, 실패 이유 |
| 비교 | 복구한 정보뿐 아니라 새 누락·잘못 바꾼 내용·위치 오류·지연·메모리도 기록 |
| 청킹에 전달 | 원문과 연결된 검증 상태·사용할 표현·해결되지 않은 제한 |

위 필드는 제안 스키마다. 현재 JSON에 이미 존재하거나 구현됐다는 뜻이 아니다.
이번 문서 작성으로 기존 run.json의 `quality_evaluation: not_run`을 변경하지 않았다.
이는 당시 자동 실행 기록이며, 이번 정성 검토를 자동 평가 실행으로 바꾸어 기록하지 않는다.

[toyota2-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/2eaa8902b6ea6415e13186080fa7b9f310e6c49a358438097677c2f91deb4298/page_00002.json
[toyota2-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/2eaa8902b6ea6415e13186080fa7b9f310e6c49a358438097677c2f91deb4298/page_00002.png
[gestalt-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/670aceefba12d5fde77212a555eb25fd3dc06a489a21bbe2b559421a2a363f82/page_00008.json
[gestalt-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/670aceefba12d5fde77212a555eb25fd3dc06a489a21bbe2b559421a2a363f82/page_00008.json
[gestalt-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/670aceefba12d5fde77212a555eb25fd3dc06a489a21bbe2b559421a2a363f82/page_00008.png
[detr13-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00013.json
[detr13-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00013.json
[detr13-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00013.png
[3m15-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/772c0ad228f8f35bd4492ae6e5a5d7022a6db46553f91a0cf021037b5c5d1d36/page_00015.json
[3m15-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/772c0ad228f8f35bd4492ae6e5a5d7022a6db46553f91a0cf021037b5c5d1d36/page_00015.json
[3m15-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/772c0ad228f8f35bd4492ae6e5a5d7022a6db46553f91a0cf021037b5c5d1d36/page_00015.png
[detr5-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00005.json
[detr5-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00005.json
[detr5-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/86582f690906603c36f8bc08266640f92e879b79b7bcdaaadb16bcc23823f87f/page_00005.png
[rag3-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00003.json
[rag3-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00003.json
[rag3-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00003.png
[rag6-vl]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00006.json
[rag6-st]: ../../../outputs/parsing_structure_20261028T232222203813Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00006.json
[rag6-img]: ../../../outputs/parsing_vl_20261027T015138481163Z/documents/de7c2798d40a0c5c32b38e3ba65c0e70f93ab1ddad234a554ed74afa49ccf9b1/page_00006.png
