# 검색·리랭킹·검색 평가

작성일: 2026-11-15.

```text
PDF → 파싱 → 청킹 → 임베딩 → 인덱싱 → 검색·평가〔현재〕
```

- 구현 코드: [retrieval/run.py](../../retrieval/run.py)(검색 진입점), [fusion.py](../../retrieval/fusion.py)(페이지 집계·RRF),
  [rerank.py](../../retrieval/rerank.py)(리랭커), [evaluation/retrieval.py](../../evaluation/retrieval.py)(검색 점수)
- 설정 파일: `configs/retrieval.json`, `configs/evaluation_retrieval.json`(질문 파일이 준비되면 만든다. 아직 없음)

질문마다 채널별로 Qdrant를 검색하고, 페이지 단위로 모아 순위를 결합한 뒤 리랭커로 다시 정렬한다.
점수는 별도 평가 단계가 정답 파일과 비교해 낸다. 검색 단계는 정답 파일을 읽지 않는다.

## 1. 결정 사항과 이유

| 항목 | 결정 | 이유·한계 |
|---|---|---|
| 평가 단위 | 페이지 | 이미지 채널은 페이지 하나가 벡터 하나라 페이지보다 작은 단위를 낼 수 없다. 세 채널을 같은 단위로 비교하려고 페이지로 맞췄다. 레이아웃(영역) 단위 평가는 텍스트 채널 보조 평가로 검토 중이다 |
| 검색 범위 | 질문이 걸린 문서 안 | 2026-11-15 결정. 질문 파일에 `document_name`을 두고 Qdrant payload 필터로 그 문서의 레코드만 검색한다. 필터는 유사도 계산 전에 적용돼 상위 k가 모두 그 문서에서 나온다 |
| 채널 검색 | 채널(컬렉션)마다 그 컬렉션을 만든 임베딩 설정으로 질의를 인코딩 | 문서와 질의를 같은 모델·지시문으로 인코딩해야 같은 공간에서 비교된다 |
| 청크 → 페이지 | 페이지별 최고 점수 청크(max) | 근거가 청크 하나에만 있어도 페이지가 올라간다. 같은 페이지 청크가 상위를 독점하지 않는다 |
| 결합 | RRF, 페이지 점수 = Σ 1/(k + 채널 순위) | 채널마다 모델과 점수 분포가 달라 점수를 직접 더하지 않는다. `k`는 설정값이다 |
| 리랭킹 | Qwen3-VL-Reranker-2B, 결합 상위 `k_rerank` 페이지 이미지와 질문을 한 쌍씩 | 질문과 페이지를 함께 읽어 더 정밀하게 판정한다. 점수는 yes/no 로짓 차이이며 확률이 아니다. CPU에서 쌍당 약 20초(4스레드 실측)라 후보 수를 제한한다. 창 밖 페이지는 결합 순서대로 뒤에 붙인다 |
| 모델 적재 | 한 번에 하나씩 올리고 내림 | 컨테이너 메모리 한도가 16GiB이고 2B float32 모델 하나가 약 13GB를 쓴다 |
| 질의와 라벨 분리 | 질문 파일은 `query_id`·`document_name`·`question`만 허용, 정답은 `qrels.jsonl`로 따로 | 정답이 검색 입력에 섞이지 않게 한다. 다른 필드가 있으면 실행을 거부한다 |

## 2. 데이터 흐름

```text
queries.jsonl {query_id, document_name, question}  +  indexing 실행(컬렉션 → 임베딩 실행)
  → search_channel(): 채널마다 질의 인코딩 → query_points(문서 필터, 상위 k_retrieve) → pages_by_max()
  → reciprocal_rank_fusion(): 채널별 페이지 순위 결합
  → QwenVLReranker.score(): 결합 상위 k_rerank 페이지를 다시 정렬
  → outputs/retrieval_*/ run.json · rankings.jsonl · summary.json

rankings.jsonl + qrels.jsonl {query_id, page_id, relevance}
  → evaluation.retrieval: 시스템별(채널별·fused_rrf·fused_rrf+rerank) 지표와 질의별 실패 단계
  → outputs/evaluation_retrieval_*/ run.json · per_query.jsonl · summary.json
```

## 3. 지표와 실패 단계

질의 하나, 관련 페이지 집합 R, 순위 목록 L에 대해 계산하고 질의 평균을 낸다. 관련성은 이진이다.

| 지표 | 정의 |
|---|---|
| Recall@k | (R ∩ L[:k]의 원소 수) ÷ (R의 원소 수) |
| Hit@k | `R ∩ L[:k]`가 비어 있지 않으면 1 |
| MRR@k | 첫 관련 페이지 순위 r ≤ k면 1/r, 아니면 0 |
| nDCG@k | 이진 이득의 DCG를 이상 순위(최대 k개)의 DCG로 나눈 값 |

관련 페이지가 없는 질의는 평균에서 빼고 따로 센다. 실패 단계는 첫 관련 페이지 하나로 판정한다.

| 단계 | 뜻 | 살펴볼 곳 |
|---|---|---|
| `not_in_candidates` | 어느 채널도 k_retrieve 안에서 찾지 못함 | 파싱·청킹·임베딩·질의 표현 |
| `outside_rerank` | 결합 순위가 k_rerank 밖 | 후보 수·결합 |
| `ranked_below_final` | 리랭킹 창 안이었지만 최종 순위가 k_final 밖 | 리랭킹 |
| `hit` | 최종 순위가 k_final 이내 | — |

MMDocIR 공식 평가의 K 값과 계산 방식은 아직 확인하지 않았다. 이 지표는 프로젝트 자체 지표다.

## 4. 실행

```bash
python -m retrieval.run --check          # 설정·질문 파일만 확인. 문서별 질문 수를 보여 준다
set -a; source .env; set +a              # 텍스트 채널이 있으면 질의 인코딩에 API 키가 필요하다
python -m retrieval.run
python -m evaluation.retrieval           # 정답 파일은 이 단계만 읽는다
```

## 5. 출력 형식

- `rankings.jsonl`: 질의마다 `query_id`·`document_name`·`question`, `channels`(컬렉션별 `[{page_id, score, best_record_id}]`),
  `fused`(`[{page_id, score}]`), `reranked`(`[{page_id, rerank_score, fused_rank}]` 또는 리랭킹을 끄면 `null`).
  평가([evaluation/retrieval.py](../../evaluation/retrieval.py))가 읽는다.
- `summary.json`(검색): 문서별 질의 수, 검색 범위, `rankings.jsonl` 해시, 채널별 인코딩·검색 시간, 리랭커 기록.
- `per_query.jsonl`·`summary.json`(평가): 질의별 관련 페이지·실패 단계·시스템별 지표, 전체 평균과 실패 단계 수.

## 6. 알려진 한계

- 지금 인덱스는 선정 27페이지뿐이라 문서마다 후보가 1~5장이다. 이 상태의 Recall@3 이상은 자명하게 높고,
  정답이 선정 페이지 밖에 있으면 찾을 수 없다. 전체 페이지 인덱싱은 파싱 개선을 확인한 뒤 진행한다.
- 리랭커가 CPU에서 느려 후보 수와 질의 수가 실행 시간을 정한다.

## 7. 현재 상태

코드와 단위 테스트까지 작성했다. 질문 파일·정답 파일과 설정 파일이 없어 실제 검색과 평가는 실행하지 않았다.
