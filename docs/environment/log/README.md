# 실행 환경 기록 타임라인

컨테이너·시스템 패키지·줄바꿈처럼 실행 환경에서 생긴 문제의 기록이다. 작성 규칙은 [docs/AGENTS.md](../../AGENTS.md)를 따른다.

| ID | 날짜 | 기록 | 상태 | 핵심 |
|---|---|---|---|---|
| [E-01](2026-11-11_container-termination.md) | 2026-11-11 | [VL 재파싱 중 컨테이너 종료](2026-11-11_container-termination.md) | 미해결 | Docker 호스트 쪽 작업으로 컨테이너가 종료·삭제된 것으로 본다. 주체는 권한이 없어 미확인. 실행은 재개로 끝냈다 |
| [E-02](2026-11-11_windows-crlf-hash-mismatch.md) | 2026-11-11 | [Windows CRLF 변환으로 해시 불일치](2026-11-11_windows-crlf-hash-mismatch.md) | 해결 | Windows Git이 받을 때 LF를 CRLF로 바꿔 코드·결과 파일 해시가 기록과 달라졌다. `.gitattributes`로 LF 고정(#20). 이미 받은 사본은 한 번 다시 받아야 한다 |
| [E-03](2026-11-11_system-packages-lost.md) | 2026-11-11 | [재시작 때 시스템 라이브러리 소실](2026-11-11_system-packages-lost.md) | 미해결 | 시작할 때마다 이미지에서 새 컨테이너를 만들어 apt 설치분이 사라진다. 이미지 변경에는 템플릿 관리자 권한이 필요하다 |
