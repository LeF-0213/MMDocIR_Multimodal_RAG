# 프로젝트 문서 안내

현재 흐름은 PDF → 파싱·검증 → 청킹 → 임베딩 → 저장·인덱싱 → 검색·평가다.
현재 실행한 파싱 결과와 앞으로의 연구 계획을 구분해 읽는다.

## 폴더 구성

```text
docs/
├─ README.md
├─ planning/
│  ├─ RAG_LEARNING_PLAN.md
│  └─ PROJECT_PLAN.md
├─ data/
│  └─ DATA_SPEC.md
└─ parsing/
   ├─ PARSING_REVIEW.md
   └─ PARSING_VALIDATION_PLAN.md
```

## 목적별로 찾기

| 찾는 내용 | 문서 | 구분 |
|---|---|---|
| 전체 학습 순서 | [기본 학습 계획](planning/RAG_LEARNING_PLAN.md) | 전달받은 기본 커리큘럼 |
| 프로젝트의 설계·선정 근거·실험 방향 | [프로젝트 계획](planning/PROJECT_PLAN.md) | 프로젝트별 계획과 확인한 구현 상태 |
| 선정 PDF·페이지·문서 특성 | [데이터 명세](data/DATA_SPEC.md) | 전달받은 관찰과 확인 범위를 구분한 기록 |
| 모델별 문제·공통 문제·PDF 텍스트층 추출 문제 | [파싱 문제 정리](parsing/PARSING_REVIEW.md#3-문제-분류) | 기존 정성 관찰과 저장 결과·함수 검사 |
| 이미지 결과 API·변경 기록·효과 측정의 구현 상태 | [파싱 과정과 확인 범위](parsing/PARSING_REVIEW.md#4-프로젝트의-파싱-과정과-평가-과정은-다르다) | 설치 패키지 확인과 구현·미구현 항목 |
| 파싱 품질 검사·재인식·복구의 다음 구현 | [파싱 검증 계획](parsing/PARSING_VALIDATION_PLAN.md) | 첫 규칙 검사 구현 현황과 후속 설계 |
| 실제 실행 명령·출력 구조 | [프로젝트 README](../README.md) | 현재 코드의 실행 안내 |

파싱 문제 정리는 PaddleOCR-VL과 PP-StructureV3의 출력 기준으로 나눈다.
파이프라인 내부의 특정 모델이 최초 원인인지 미확인인 항목은 그 상태를 유지한다.
`ok`·검토 신호 수·테스트 통과를 원문 정확도나 개선 효과로 해석하지 않는다.
