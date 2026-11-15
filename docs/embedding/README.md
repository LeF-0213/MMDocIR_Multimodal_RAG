# 임베딩

작성일: 2026-11-14.

```text
PDF → 파싱 → 청킹 → 임베딩〔현재〕 → 인덱싱 → 검색·평가
```

- 구현 코드: [embedding/run.py](../../embedding/run.py)(진입점·입력 확인·저장), [encoders.py](../../embedding/encoders.py)(모델 어댑터)
- 설정 파일: [configs/embedding.json](../../configs/embedding.json)
- 실행 기록: [임베딩 기록 타임라인](log/README.md)

청크 텍스트와 페이지 이미지를 벡터로 바꿔 실행 폴더에 저장한다. 벡터 행마다 원래 청크·페이지 ID와 입력 해시를
함께 남겨, 인덱싱과 검색 결과에서 원문 위치로 돌아갈 수 있게 한다. Qdrant에 넣는 일은 [인덱싱](../indexing/README.md)이 맡는다.

## 1. 결정 사항과 이유

| 항목 | 결정 | 이유·한계 |
|---|---|---|
| 채널 | 텍스트(청크)와 이미지(페이지) 두 갈래, 채널마다 실행 폴더 하나 | 두 모델의 벡터는 표현 공간이 달라 서로 비교하지 않는다. 채널마다 따로 저장·검색하고 순위만 결합한다([검색](../retrieval/README.md)) |
| 텍스트 모델 | `qwen/qwen3-embedding-8b`를 OpenRouter API로 호출 | 2026-11-14 결정. 청킹이 이미 이 모델의 토크나이저로 길이를 쟀다. 컨테이너 메모리 한도(cgroup 16GiB)에서 8B를 로컬로 올리기 어렵다. 공급자가 바뀌면 같은 모델 ID라도 결과가 다를 수 있어, 응답 모델·공급자를 호출마다 기록한다 |
| 이미지 모델 | `Qwen/Qwen3-VL-Embedding-2B`, 로컬 CPU float32 | 2026-11-14 결정. 계획의 8B 선택([프로젝트 계획](../planning/PROJECT_PLAN.md))에서 바꿨다. 2B float32 실행의 RSS가 약 13GB였고, 8B는 같은 형식으로 16GiB 한도에 들어가지 않는다. 모델 카드의 MMEB-V2 수치는 8B가 2B보다 높지만 이 프로젝트에서 직접 비교하지 않았다 |
| 이미지 입력 | 파싱 실행이 저장한 144 DPI 페이지 PNG(828×1231 등) | 파싱과 같은 이미지를 써서 페이지 ID·해시로 연결한다. 이미지 채널은 파싱 품질과 관계없지만, 지금 코드는 파싱 실행 폴더에서 이미지를 찾는다 |
| 지시문 | 텍스트 질의만 `Instruct: …\nQuery:…` 형식, 문서는 지시문 없음. 이미지 모델은 문서·질의 지시문을 따로 둠 | 두 모델 카드의 사용법이다. 지시문 문구는 설정 파일에 있다 |
| 정규화 | 행마다 L2 정규화, 정규화 전 노름 통계를 기록 | 정규화 뒤 내적이 코사인 유사도가 된다. 노름 0인 행은 실패로 처리한다 |
| 이미지 처리 단위 | 한 장씩 | 최대 메모리를 낮추려는 선택이다. 묶음 처리의 속도 이점은 측정하지 않았다 |
| API 키 | 설정에는 환경 변수 이름만, 기록에는 사용 여부만 | 키 값을 코드·설정·실행 기록에 남기지 않는다. 루트 `.env`는 git이 추적하지 않는다 |

## 2. 데이터 흐름

```text
텍스트: outputs/chunking_<파서>_*/chunks.jsonl
        → text_inputs(): 청크 메타데이터 + 청크 글자
        → OpenRouterTextEncoder.encode_documents(): batch_size개씩 POST /embeddings, 429·5xx 재시도
        → normalize_rows()
        → outputs/embedding_text_<파서>_*/ (records.jsonl · vectors.npy · run.json · summary.json)

이미지: configs/selection.json + outputs/parsing_vl_*/documents/<문서 ID>/page_N.json·png
        → page_image_inputs(): 선정 페이지의 PNG 경로와 sha256 확인
        → QwenVLEncoder.encode_images(): 한 장씩 문서 지시문과 함께 인코딩
        → normalize_rows()
        → outputs/embedding_image_page_*/ (같은 네 파일)
```

## 3. 설정값

| 필드 | 뜻 |
|---|---|
| `channels` | 실행할 채널 목록. `--channel`로 하나만 고를 수 있다 |
| `text.chunking_runs` | 텍스트 채널 입력 청킹 실행. 실행마다 임베딩 실행 폴더 하나가 생긴다 |
| `text.model.batch_size` | 요청 하나에 넣는 청크 수 |
| `text.model.timeout_seconds`·`max_retries`·`retry_backoff_seconds` | 요청 제한 시간(초), 재시도 횟수, 재시도 대기(초, 시도마다 배수로 늘림) |
| `text.model.query_instruction` | 질의를 인코딩할 때 붙이는 과제 설명 |
| `image.page_image_run` | 페이지 이미지를 가져올 파싱 실행 |
| `image.model.repo_id`·`revision` | Hugging Face 모델과 고정 커밋 |
| `image.model.device`·`dtype`·`cpu_threads` | 실행 장치, 가중치 형식, PyTorch CPU 스레드 수 |
| `image.model.document_prompt`·`query_prompt` | 페이지와 질의에 붙이는 지시문 |

## 4. 실행

```bash
python -m embedding.run --check            # 설정과 입력 파일만 확인. 모델·API 호출 없음
set -a; source .env; set +a                # 텍스트 채널: OPENROUTER_API_KEY를 셸에 불러온다
python -m embedding.run --channel text     # 원격 호출과 과금이 생긴다
python -m embedding.run --channel image    # 처음 실행 때 가중치를 HF 캐시에 내려받는다
```

새 실행 폴더를 만들고 기존 폴더는 덮어쓰지 않는다. 의존성은 [requirements.txt](../../requirements.txt)의
PyTorch CPU·transformers·sentence-transformers 묶음이다.

## 5. 출력 형식

`outputs/embedding_<채널>_*/`. 인덱싱([indexing/run.py](../../indexing/run.py))이 읽는다.

| 파일 | 내용 |
|---|---|
| `vectors.npy` | float32 `[레코드 수, 차원]`. 행 번호가 `records.jsonl`의 `vector_index`다 |
| `records.jsonl` | 행마다 `vector_index`(프로젝트 생성)와 입력 메타데이터. 텍스트는 `chunk_id`·`kind`·`document_id`·`document_name`·`page_id`·`page_number`·`token_count`·`heading`·`flags`(청킹이 만든 필드), 이미지는 `page_id`·`document_id`·`document_name`·`page_number`·`page_image`·`page_image_sha256`·`dpi`·`width`·`height`·`encode_seconds` |
| `run.json` | 설정, 입력 실행·해시, 코드 해시, 패키지 버전 |
| `summary.json` | 상태, 벡터 수·차원, `vectors.npy`·`records.jsonl` 해시, 노름 통계, 모델 기록(호출 수·토큰·비용·시간) |

## 6. 알려진 한계

- 이미지 채널은 파싱 실행의 페이지 PNG에 묶여 있다. 파싱하지 않은 페이지는 임베딩할 수 없다.
- 텍스트 채널은 파싱·청킹의 미해결 이슈를 그대로 물려받는다([남은 이슈](../../README.md#남은-이슈)).
- 원격 모델은 공급자 쪽 변경을 막을 수 없다. 같은 설정으로 다시 실행해도 벡터가 같다는 보장은 없다.

## 7. 현재 상태

이미지 채널 27페이지를 임베딩했다([2026-11-14 이미지](log/2026-11-14_image.md)). 텍스트 채널은 API 키가 준비되지 않아 실행하지 않았다.
