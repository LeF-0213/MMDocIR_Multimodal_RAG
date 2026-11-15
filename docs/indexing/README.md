# 인덱싱

작성일: 2026-11-14.

```text
PDF → 파싱 → 청킹 → 임베딩 → 인덱싱〔현재〕 → 검색·평가
```

- 구현 코드: [indexing/run.py](../../indexing/run.py)(진입점·적재·확인), [store.py](../../indexing/store.py)(연결·컬렉션 이름·포인트 ID)
- 설정 파일: [configs/indexing.json](../../configs/indexing.json)
- 실행 기록: [인덱싱 기록 타임라인](log/README.md)

임베딩 실행의 벡터를 Qdrant 컬렉션에 넣고, 정답 라벨 없이 저장이 맞는지 확인한다.

## 1. 결정 사항과 이유

| 항목 | 결정 | 이유·한계 |
|---|---|---|
| 벡터 DB | Qdrant, 지금은 로컬 모드(`QdrantClient(path=...)`) | 2026-11-14 결정. payload 필터(문서 범위 제한)와 HNSW 설정을 쓸 수 있고, 같은 클라이언트 코드로 서버 모드로 옮길 수 있다. 로컬 모드는 서버 없이 실행되지만 HNSW를 만들지 않고 항상 전수 비교한다 |
| 컬렉션 | 임베딩 실행 하나 → 컬렉션 하나, 이름은 임베딩 실행 폴더 이름 | 채널마다 표현 공간이 달라 한 컬렉션에 섞지 않는다. 이름만 보고 어느 벡터 파일에서 왔는지 안다. 이미 있으면 덮어쓰지 않고 실패한다 |
| 포인트 ID | `uuid5(NAMESPACE_URL, "mmdocir-project:" + 청크 ID 또는 페이지 ID)` | Qdrant는 정수나 UUID만 받는다. 같은 레코드는 언제나 같은 ID를 받는다 |
| payload | 임베딩 레코드의 메타데이터 + `vector_index` + `embedding_run` | 검색 결과에서 페이지·청크·원문 위치로 돌아가는 연결이다. 정답 라벨은 넣지 않는다 |
| 거리 | COSINE | 임베딩이 L2 정규화돼 있어 내적과 순위가 같다 |
| 확인 | 개수, 다시 읽은 벡터 차이, 자기 검색 1위, 정확 검색과 ANN의 상위 k 일치율 | 라벨 없이 저장과 ID 연결이 맞는지 본다. ANN 일치율은 HNSW가 실제로 쓰였을 때만 의미가 있다(`ann_index_used`) |

## 2. 데이터 흐름

```text
outputs/embedding_*/ vectors.npy · records.jsonl · summary.json
  → load_embedding_run(): 요약의 sha256과 실제 파일 해시, 행 번호와 레코드 순서 확인
  → create_collection(): 차원·거리·HNSW 설정
  → upsert(): upsert_batch_size개씩 포인트 적재
  → count · retrieve · query_points: 확인 지표 계산
  → Qdrant 저장 위치(로컬 모드면 data/qdrant/, git 제외) + outputs/indexing_*/ run.json · summary.json
```

## 3. 설정값

| 필드 | 뜻 |
|---|---|
| `embedding_runs` | 적재할 임베딩 실행 목록. `--embedding-run`으로 바꿀 수 있다 |
| `qdrant.path`·`url`·`api_key_env` | 로컬 모드 저장 폴더 또는 서버 주소 중 하나. 서버 키는 환경 변수 이름만 적는다 |
| `distance` | Qdrant 거리 함수 이름 |
| `hnsw.m`·`ef_construct`·`full_scan_threshold` | HNSW 그래프 이웃 수, 구축 탐색 폭, 이 크기(KB)보다 작은 세그먼트는 전수 비교 |
| `upsert_batch_size` | 한 번에 적재하는 포인트 수 |
| `check.top_k`·`hnsw_ef` | 확인 검색의 상위 개수와 ANN 탐색 폭 |

## 4. 실행

```bash
python -m indexing.run --check   # 설정과 입력 실행만 확인. Qdrant에 쓰지 않음
python -m indexing.run           # 컬렉션 생성·적재·확인
```

## 5. 출력 형식

- Qdrant 컬렉션: 검색([retrieval/run.py](../../retrieval/run.py))이 이름으로 찾는다. payload의 `page_id`·`chunk_id`·`document_name`을 읽는다.
- `outputs/indexing_*/run.json`: 설정, 저장 위치, 코드 해시, 패키지 버전.
- `outputs/indexing_*/summary.json`: 컬렉션마다 이름, 임베딩 실행과 그 요약 해시, 채널, 포인트 수, 차원, 거리, 확인 지표.
  검색은 임베딩 요약 해시가 인덱싱 뒤에 바뀌지 않았는지 확인한다.

## 6. 알려진 한계

- 로컬 모드는 HNSW를 만들지 않는다. ANN과 정확 검색의 비교는 서버 모드와 충분한 벡터 수에서만 의미가 있다.
- 서버 모드에서 문서 필터를 쓰면 payload 인덱스를 만드는 편이 빠르다. 지금은 만들지 않는다.

## 7. 현재 상태

이미지 채널 컬렉션 하나를 적재했다([2026-11-14 인덱싱](log/2026-11-14_image.md)). 텍스트 채널은 임베딩이 끝난 뒤 새 인덱싱 실행으로 넣는다.
