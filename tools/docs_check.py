"""PR이 코드·결과를 바꿀 때 짝이 되는 문서도 갱신했는지, 문서끼리 서로 맞는지 검사한다.

전체 위치: 코드·실행 결과 변경 → PR → GitHub Actions(.github/workflows/docs-check.yml) → 이 스크립트 → 머지 허용·차단.
파이프라인 데이터를 읽거나 바꾸지 않는다. 저장소의 파일 목록, git diff, 마크다운 문서만 읽는다.

검사 A (짝 문서 갱신): PR에서 바뀐 파일이 규칙의 when 패턴에 맞으면, require_any 패턴의 문서도 하나 이상 바뀌어야 한다.
  PR에 예외 라벨이 있고 PR 설명에 "문서 갱신 불필요 이유:" 뒤 내용이 있으면 검사 A를 통과시킨다.
검사 B (문서 일관성):
  B1 상대 링크와 제목 앵커가 실제 파일·제목을 가리키는지
  B2 같은 이슈 ID의 상태가 단계 이슈 목록, 기록의 이슈 요약, 루트 README의 남은 이슈에서 같은지
  B3 해결되지 않은 이슈가 모두 남은 이슈 표에 있고, 해결된 이슈는 빠져 있는지
  B4 표 위에 적은 상태별 수가 실제 행 수와 같은지

입력: 설정 파일(.github/docs-check.json), 비교할 두 커밋(--base, --head), 선택으로 PR 이벤트 JSON(--event).
출력: 표준 출력에 검사 결과. 실패가 하나라도 있으면 종료 코드 1, 모두 통과하면 0, 실행 인자·설정 오류는 2.
부작용: 파일을 만들거나 바꾸지 않는다. git 명령은 읽기만 한다(diff, ls-files).
"""

# 표준 라이브러리: argparse는 실행 인자, json은 설정·이벤트 파일, os는 GitHub Actions 환경 변수 확인,
# re는 경로 패턴·표·링크 해석, subprocess는 git 읽기 명령 실행, sys는 종료 코드,
# unicodedata는 GitHub 제목 앵커 규칙(문자 종류 판별), pathlib은 경로 계산에 쓴다.
# 외부 패키지를 쓰지 않아 CI에서 설치 없이 실행된다.
import argparse
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

# 설정 파일 형식의 정의다. 다른 형식의 설정을 잘못 읽지 않도록 확인한다.
CONFIG_SCHEMA = "project.docs_check.v1"
# 마크다운 상대 링크: [글](경로) 형식과 [이름]: 경로 형식(참조 링크)을 모두 찾는다.
LINK = re.compile(r"\]\(([^)\s]+)\)|^\[[^\]]+\]:\s*(\S+)", re.M)
# 외부 주소는 검사하지 않는다. 네트워크 요청 없이 저장소 안의 연결만 본다.
EXTERNAL = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.M)
# 상태별 수 줄의 예: "현재 상태별 수(19개): 해결 2, 부분 해결 1." 이름과 숫자 쌍을 읽는다.
COUNT_LINE = re.compile(r"^현재 [^:\n]*:\s*(.+)$", re.M)
COUNT_ITEM = re.compile(r"([^,\d]+?)\s+(\d+)")


def load_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("schema_version") != CONFIG_SCHEMA:
        raise ValueError(f"설정 형식이 다릅니다: {config.get('schema_version')!r}")
    return config


def glob_regex(pattern: str) -> re.Pattern:
    """저장소 상대 경로용 glob을 정규식으로 바꾼다.

    `**`는 폴더 경계를 넘어 0개 이상의 경로, `*`·`?`는 폴더 하나 안의 글자만 맞춘다.
    fnmatch는 `*`가 `/`까지 넘어가므로 폴더 단위 규칙을 쓰려고 직접 바꾼다.
    """
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?"); i += 3
        elif pattern.startswith("**", i):
            out.append(".*"); i += 2
        elif pattern[i] == "*":
            out.append("[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append("[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_regex(p).match(path) for p in patterns)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def changed_files(base: str, head: str) -> list[str]:
    """두 커밋의 분기점부터 head까지 바뀐 파일이다.

    `base...head`는 PR 브랜치가 분기한 뒤 바꾼 파일만 낸다. main에 다른 PR이 먼저 들어가도 섞이지 않는다.
    --no-renames로 이름 변경을 삭제+추가로 나눠, 옛 경로와 새 경로를 모두 변경으로 센다.
    """
    return [line for line in git("diff", "--name-only", "--no-renames", f"{base}...{head}").splitlines() if line]


def exemption_reason(event_path: Path | None, exemption: dict) -> str | None:
    """예외 라벨이 있고 PR 설명에 이유가 적혀 있으면 그 이유를, 아니면 None을 돌려준다."""
    if event_path is None:
        return None
    pr = json.loads(event_path.read_text(encoding="utf-8")).get("pull_request") or {}
    labels = {label.get("name") for label in pr.get("labels") or []}
    if exemption["label"] not in labels:
        return None
    for line in (pr.get("body") or "").splitlines():
        if exemption["reason_marker"] in line:
            reason = line.split(exemption["reason_marker"], 1)[1].strip()
            if reason:
                return reason
    return None


def check_sync(changed: list[str], rules: list[dict]) -> list[str]:
    """검사 A. 규칙마다 when에 맞는 변경이 있는데 require_any 문서 변경이 없으면 실패로 돌려준다."""
    failures = []
    for rule in rules:
        triggers = [path for path in changed if matches_any(path, rule["when"])]
        if triggers and not any(matches_any(path, rule["require_any"]) for path in changed):
            shown = ", ".join(triggers[:5]) + (f" 외 {len(triggers) - 5}개" if len(triggers) > 5 else "")
            failures.append(f"[A] {rule['name']}: {shown} 이(가) 바뀌었지만 "
                            f"{' 또는 '.join(rule['require_any'])} 문서 변경이 없습니다.")
    return failures


def github_slug(text: str) -> str:
    """GitHub가 제목에서 앵커를 만드는 규칙이다. 소문자로 바꾸고 글자·숫자·밑줄·하이픈·공백만 남긴 뒤 공백을 하이픈으로 바꾼다."""
    text = re.sub(r"[`*]", "", text.strip().lower())
    kept = (c for c in text if c in " -_" or unicodedata.category(c)[0] in "LMN")
    return "".join(kept).replace(" ", "-")


def anchors_of(path: Path, cache: dict) -> set[str]:
    if path not in cache:
        text = path.read_text(encoding="utf-8")
        # 코드 블록 안의 # 줄은 제목이 아니므로 빼고 센다.
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        cache[path] = {github_slug(h) for h in HEADING.findall(text)}
    return cache[path]


def check_links(root: Path, settings: dict) -> list[str]:
    """B1. 추적 중인 마크다운의 상대 링크가 실제 파일과 제목을 가리키는지 본다."""
    files = [f for f in git("ls-files").splitlines()
             if matches_any(f, settings["include"]) and not matches_any(f, settings["exclude"])]
    failures, cache = [], {}
    for name in files:
        source = root / name
        text = re.sub(r"```.*?```", "", source.read_text(encoding="utf-8"), flags=re.S)
        for match in LINK.finditer(text):
            target = match.group(1) or match.group(2)
            if EXTERNAL.match(target):
                continue
            file_part, _, fragment = target.partition("#")
            dest = (source.parent / file_part).resolve() if file_part else source
            if not dest.exists():
                failures.append(f"[B1] {name}: 없는 경로 {target}")
            elif fragment and dest.suffix == ".md" and fragment not in anchors_of(dest, cache):
                failures.append(f"[B1] {name}: 없는 제목 앵커 {target}")
    return failures


def table_statuses(text: str, id_regex: re.Pattern, column: int) -> dict[str, list[str]]:
    """표 행의 첫 칸에서 이슈 ID를, column 번째 칸에서 상태를 읽는다. 같은 ID가 여러 번 나오면 모두 모은다.

    상태 칸 뒤에 붙인 "([상세](...))" 링크는 상태가 아니므로 떼어 낸다.
    """
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        head = id_regex.search(cells[0]) if cells else None
        if head and len(cells) > column and re.fullmatch(rf"\[?{id_regex.pattern}\]?(\([^)]*\))?", cells[0]):
            found.setdefault(head.group(0), []).append(re.sub(r"\(\[상세\].*", "", cells[column]).strip())
    return found


def section(text: str, heading: str) -> str | None:
    """heading으로 시작하는 절의 본문이다. 같은 수준의 다음 제목에서 끝난다."""
    if heading not in text:
        return None
    level = heading.split(" ")[0]
    body = text.split(heading, 1)[1]
    end = re.search(rf"^{re.escape(level)} ", body, re.M)
    return body[:end.start()] if end else body


def check_counts(name: str, text: str, statuses: dict[str, str], valid: list[str]) -> list[str]:
    """B4. "현재 …: 상태 n, 상태 n" 줄의 수가 표의 실제 행 수와 같은지 본다. 줄이 없으면 검사하지 않는다."""
    line = COUNT_LINE.search(text)
    if not line:
        return []
    written = {k.strip(): int(v) for k, v in COUNT_ITEM.findall(line.group(1)) if k.strip() in valid}
    actual = {s: list(statuses.values()).count(s) for s in valid if s in statuses.values()}
    if written != actual:
        return [f"[B4] {name}: 적힌 상태별 수 {written} 와 표의 실제 수 {actual} 가 다릅니다."]
    return []


def check_issues(root: Path, settings: dict) -> list[str]:
    """B2·B3·B4. 단계 이슈 목록을 기준으로 기록 요약과 루트 README 남은 이슈의 상태를 대조한다."""
    id_regex = re.compile(settings["id_pattern"])
    valid, closed = settings["statuses"], set(settings["closed_statuses"])
    failures: list[str] = []
    truth: dict[str, str] = {}
    for name in settings["stage_lists"]:
        path = root / name
        if not path.exists():
            continue
        rows = table_statuses(path.read_text(encoding="utf-8"), id_regex, settings["stage_list_status_column"][name])
        stage = {}
        for issue, values in rows.items():
            if len(values) > 1 or issue in truth:
                failures.append(f"[B2] {name}: 이슈 ID {issue} 가 두 번 이상 나옵니다.")
            if values[0] not in valid:
                failures.append(f"[B2] {name}: {issue} 의 상태 {values[0]!r} 는 정의된 상태가 아닙니다.")
            stage[issue] = truth[issue] = values[0]
        failures += check_counts(name, path.read_text(encoding="utf-8"), stage, valid)
    if not truth:
        return failures  # 이슈 목록이 아직 없는 저장소 상태에서는 B2~B4를 건너뛴다.

    for pattern in settings["records"]:
        for path in sorted(root.glob(pattern)):
            body = section(path.read_text(encoding="utf-8"), settings["record_summary_heading"])
            if body is None:
                continue
            for issue, values in table_statuses(body, id_regex, settings["record_status_column"]).items():
                if issue not in truth:
                    failures.append(f"[B2] {path.relative_to(root)}: {issue} 가 단계 이슈 목록에 없습니다.")
                elif values[0] != truth[issue]:
                    failures.append(f"[B2] {path.relative_to(root)}: {issue} 상태 {values[0]!r} ≠ 이슈 목록 {truth[issue]!r}")

    remaining_text = (root / settings["remaining_file"]).read_text(encoding="utf-8")
    body = section(remaining_text, settings["remaining_heading"])
    if body is None:
        return failures + [f"[B3] {settings['remaining_file']}: {settings['remaining_heading']} 절이 없습니다."]
    remaining = {k: v[0] for k, v in table_statuses(body, id_regex, settings["remaining_status_column"]).items()}
    should = {k for k, v in truth.items() if v not in closed}
    for issue in sorted(should - set(remaining)):
        failures.append(f"[B3] 남은 이슈 표에 {issue}({truth[issue]}) 가 없습니다.")
    for issue in sorted(set(remaining) - should):
        failures.append(f"[B3] 남은 이슈 표에 끝난 이슈 {issue}({truth.get(issue, '목록에 없음')}) 가 남아 있습니다.")
    for issue in sorted(set(remaining) & should):
        if remaining[issue] != truth[issue]:
            failures.append(f"[B2] 남은 이슈 표의 {issue} 상태 {remaining[issue]!r} ≠ 이슈 목록 {truth[issue]!r}")
    failures += check_counts(settings["remaining_file"], body, remaining, valid)
    return failures


def report(title: str, failures: list[str]) -> None:
    print(f"{title}: {'통과' if not failures else f'실패 {len(failures)}건'}")
    for failure in failures:
        # GitHub Actions에서는 ::error:: 형식이 PR 화면의 검사 결과에 오류로 표시된다.
        print(f"::error::{failure}" if os.environ.get("GITHUB_ACTIONS") == "true" else f"  {failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="문서 갱신·일관성 검사")
    parser.add_argument("--config", type=Path, required=True, help="검사 설정 JSON")
    parser.add_argument("--base", help="비교 기준 커밋(PR 대상 브랜치). 없으면 검사 A를 건너뛴다")
    parser.add_argument("--head", default="HEAD", help="비교할 커밋. 기본 HEAD")
    parser.add_argument("--event", type=Path, help="GitHub PR 이벤트 JSON. 예외 라벨·이유를 읽는다")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        root = Path(git("rev-parse", "--show-toplevel").strip())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"실행 오류: {exc}", file=sys.stderr)
        return 2

    failures: list[str] = []
    if args.base:
        sync = check_sync(changed_files(args.base, args.head), config["sync_rules"])
        reason = exemption_reason(args.event, config["exemption"]) if sync else None
        if reason:
            print(f"검사 A: 예외 적용({config['exemption']['label']}). 이유: {reason}")
            for failure in sync:
                print(f"  예외로 넘긴 항목 {failure}")
        else:
            report("검사 A 짝 문서 갱신", sync)
            failures += sync
    else:
        print("검사 A: --base가 없어 건너뜀")

    links = check_links(root, config["links"])
    report("검사 B1 링크", links)
    issues = check_issues(root, config["issues"])
    report("검사 B2~B4 이슈 상태", issues)
    failures += links + issues
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
