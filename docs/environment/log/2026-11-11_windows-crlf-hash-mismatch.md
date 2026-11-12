# Windows 작업 사본의 CRLF 변환으로 해시가 기록과 달라짐

- ID: E-02
- 상태: 해결. `.gitattributes`를 main에 합쳤다(#20, `e54b3d7`). 확인은 [4절 후속](#4-결과)에 있다.
- 관련 실행: [rerecognition_vl_20261111T101144801007Z](../../../outputs/rerecognition_vl_20261111T101144801007Z),
  [chunking_vl_20261111T105933020828Z](../../../outputs/chunking_vl_20261111T105933020828Z)
- 코드: `1655543`(#19), 해결 `e54b3d7`(#20)
- 출처: PR #19 커밋 메시지, `docs/chunking/CHUNKING.md` 6절, `docs/parsing_log/PARSING_ROOT_CAUSES.md` 9.1절(커밋 `1655543`).
  2절의 재현 테스트는 2026-11-12에 새로 했다.

## 1. 문제가 어디서 보였나

- PR #19 작업 중 Windows 작업 사본에서 청킹을 실행하자 재인식 결과가 적용되지 않았다. 청킹은 재인식 기록의
  `input_run_json_sha256`이 입력 파싱 실행 `run.json`의 해시와 같을 때만 재인식 결과를 쓰는데, 두 해시가 달랐다.
- 재인식 `run.json`에 기록된 `parsing/quality_checks.py` 해시도 저장소 파일과 달랐다. 당시 컨테이너에 복사한 파일이
  Windows 작업 사본이라 CRLF 줄바꿈이었다.
- 그래서 재인식과 청킹은 결과 JSON이 LF로 남아 있는 Linux 컨테이너에서 실행했다.

## 2. 원인을 어떻게 찾았나

- 사실: 저장소의 텍스트 파일은 모두 LF다(2026-11-12 `git ls-files --eol`에서 텍스트 353개 모두 `i/lf`).
- 사실: Windows용 Git을 `core.autocrlf=true`로 쓰면 파일을 받을 때 작업 폴더의 텍스트 파일을 CRLF로 바꾼다.
  저장소 내용은 LF 그대로다.
- 사실: 프로젝트 코드는 작업 폴더 파일의 바이트로 SHA-256을 계산한다(`common/files.file_hash`).
  화면에서는 같아 보여도 바이트가 달라 해시가 달라진다.

2026-11-12 재현 테스트(Linux에서 `core.autocrlf=true`로 Windows 동작을 흉내 냄, scratchpad 클론, 저장소 변경 없음):

```bash
git -c core.autocrlf=true clone -q -b main "$REPO" repo && cd repo && git config core.autocrlf true
tr -cd '\r' < "$RUN_JSON" | wc -c; sha256sum "$RUN_JSON"            # [1]
git fetch -q "$REPO" chore/gitattributes-lf && git checkout -q FETCH_HEAD   # [2]
git rm -rq --cached . && git reset -q --hard                         # [3]
git -c core.autocrlf=true clone -q -b chore/gitattributes-lf "$REPO" repo2  # [4]
```

`RUN_JSON`은 `outputs/chunking_vl_20261111T105933020828Z/run.json`, 저장소에 든 바이트의 SHA-256은 `dd09e74be71f…`다.

| 단계 | 상황 | CR 개수 | SHA-256 앞자리 |
|---|---|---:|---|
| [1] | `.gitattributes` 없는 main을 받음 | 127 | `c0619bed47b3` (기록과 다름) |
| [2] | `.gitattributes` 커밋으로 이동만 함(pull과 같음) | 127 | `c0619bed47b3`. 변경 파일 93개로 표시 |
| [3] | 인덱스를 비우고 hard reset | 0 | `dd09e74be71f` (기록과 같음) |
| [4] | `.gitattributes` 있는 브랜치를 새로 클론 | 0 | `dd09e74be71f` (기록과 같음) |

루트 `AGENTS.md`도 [1]에서 CR 294개로 바뀌었다가 [3]·[4]에서 저장소와 같은 바이트가 됐다.

## 3. 왜 이 해결책을 골랐나

| 후보 | 판단 |
|---|---|
| 문서 규칙: 줄바꿈이 바뀐 사본에서는 해시를 확인하는 실행을 하지 않는다 | 사람이 기억해야 하고, 바뀌었는지 눈으로 알 수 없다. 이미 받은 사본을 위한 보조 규칙으로 [outputs/AGENTS.md](../../../outputs/AGENTS.md)에 넣었다 |
| 해시를 계산할 때 줄바꿈을 정규화 | 기록된 해시가 실제 파일 바이트와 달라진다. 텍스트와 PNG 같은 바이너리를 나눠 처리해야 하고, 이미 기록된 해시와 맞지 않는다(이번 정리 판단) |
| `.gitattributes`로 받을 때부터 LF 고정 | 고른 방법. 저장소에 커밋하는 설정이라 누가 어느 PC에서 받든 적용되고, PC별 `core.autocrlf`보다 우선한다 |

`.gitattributes` 내용: 텍스트 파일은 `text=auto eol=lf`, `outputs/**`는 `-text`로 줄바꿈 변환을 아예 하지 않는다.
실행 결과는 코드가 쓴 바이트를 그대로 보존해야 하기 때문이다.

## 4. 결과

- 재현 테스트에서 `.gitattributes`가 있으면 Windows 설정으로 받아도 저장소와 같은 바이트가 됐다([4]).
- 저장소의 텍스트 파일이 이미 LF라 `git add --renormalize .`으로 바뀌는 파일은 없었다.
- main에 합치기 전이라 상태는 미해결로 둔다.

후속(2026-11-12): `.gitattributes`를 main에 합쳤다(#20, `e54b3d7`). main `6a85cc7`을 scratchpad에
`core.autocrlf=true`로 새로 클론하자 `RUN_JSON`의 CR 0개, SHA-256 `dd09e74be71f`(기록과 같음), 루트 `AGENTS.md` CR 0개,
`git ls-files --eol`에서 작업 폴더가 CRLF인 파일 0개, 변경 표시 0개였다. 그래서 상태를 해결로 바꿨다.

## 5. 남은 문제와 다음 단계

- `chore/gitattributes-lf`를 main에 합친 뒤 상태를 고친다.
- 이미 CRLF로 받은 Windows 작업 사본은 pull만으로 바뀌지 않는다([2]). 커밋하지 않은 변경을 먼저 커밋하거나 stash한 뒤
  `git rm -r --cached .`와 `git reset --hard`로 다시 받아야 한다([3]). 이 명령은 커밋하지 않은 변경을 지운다.
- 실제 Windows에서는 확인하지 않았다. 재현은 Linux에서 `core.autocrlf=true`로 흉내 낸 것이다.

후속(2026-11-12): 첫 항목은 끝났다([4절 후속](#4-결과)). 나머지 두 항목은 그대로 남는다.
