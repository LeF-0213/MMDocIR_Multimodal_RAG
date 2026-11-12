"""문서의 Mermaid 그림이 렌더링되는지, 선이 엇갈리거나 상자·라벨이 겹치지 않는지 검사한다.

전체 위치: PR → GitHub Actions(.github/workflows/docs-check.yml) → tools/docs_check.py(문서 갱신·일관성)와 이 스크립트(그림).
docs/AGENTS.md "그림" 절의 규칙 중 기계로 판정할 수 있는 것을 막는다.

처리 순서
1. 추적 중인 마크다운에서 ```mermaid 블록과 시작 줄 번호를 꺼낸다.
2. 사전 검사(렌더링 없이): 노드·선 라벨의 꺾쇠(<br/> 제외), 줄당 길이 한도를 넘는 선 라벨.
3. mmdc(mermaid-cli)로 SVG를 만든다. 실패하면 문법 오류로 보고한다.
4. tools/diagram/measure.mjs가 브라우저에서 상자·묶음 상자·선·선 라벨의 좌표를 잰다.
5. 좌표로 판정한다: 선끼리 교차, 선이 다른 상자·다른 선의 라벨을 지남, 상자끼리 겹침,
   묶음 상자끼리 겹침(포함 관계 제외), 선 라벨이 상자나 다른 라벨을 덮음.

입력: 설정 파일(.github/docs-check.json의 diagrams 절), 도구 폴더(tools/diagram, npm으로 의존성 설치).
출력: 표준 출력에 그림마다 결과. 문제가 있으면 종료 코드 1, 없으면 0, 실행·설정 오류는 2.
부작용: 임시 폴더에 .mmd·.svg를 만들었다가 끝나면 지운다. 저장소 파일은 바꾸지 않는다. 헤드리스 브라우저를 띄운다.
한계: 선이 "보기 좋게" 놓였는지(지나치게 긴 우회 등)는 판정하지 않는다. 판정 기준은 교차와 겹침뿐이다.
"""

# 표준 라이브러리: argparse는 실행 인자, json은 설정과 측정 결과, os는 GitHub Actions 환경 변수 확인,
# re는 mermaid 블록·라벨 추출, subprocess는 git 파일 목록·mmdc·node 실행, sys는 종료 코드,
# tempfile은 지워지는 임시 폴더, pathlib은 경로 계산.
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# 내부 모듈: docs_check.py의 경로 패턴 판정과 설정 읽기를 함께 쓴다. 같은 설정 파일·같은 glob 규칙을 쓰기 위해서다.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from docs_check import load_config, matches_any  # noqa: E402

# 측정 스크립트(measure.mjs)·브라우저 실행 옵션(puppeteer.json)·npm 의존성 정의가 있는 폴더다. 이 파일 위치 기준이다.
TOOL_DIR = Path(__file__).resolve().parent / "diagram"
BLOCK = re.compile(r"^```mermaid\n(.*?)^```", re.M | re.S)
# 선 라벨: -->|"글자"|, -.->|글자| 형식. 큰따옴표가 있으면 안쪽만 쓴다.
EDGE_LABEL = re.compile(r"-[-.=]*>\|\"?(.*?)\"?\|")
QUOTED = re.compile(r"\"([^\"]*)\"")
BREAK = re.compile(r"<br\s*/?>", re.I)


def git_files() -> list[str]:
    return subprocess.run(["git", "ls-files"], check=True, capture_output=True, text=True).stdout.splitlines()


def extract_blocks(root: Path, settings: dict) -> list[dict]:
    """추적 중인 마크다운에서 mermaid 블록을 꺼낸다. 위치는 "파일:블록 시작 줄"로 남긴다."""
    blocks = []
    for name in git_files():
        if not matches_any(name, settings["include"]) or matches_any(name, settings["exclude"]):
            continue
        text = (root / name).read_text(encoding="utf-8")
        for match in BLOCK.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            blocks.append({"where": f"{name}:{line}", "source": match.group(1)})
    return blocks


def lint(source: str, settings: dict) -> list[str]:
    """2. 렌더링 없이 원문으로 잡는 규칙 위반이다."""
    problems = []
    for text in QUOTED.findall(source):
        if re.search(r"[<>]", BREAK.sub("", text)):
            problems.append(f"[꺾쇠] 노드·선 글자에 <br/> 말고 꺾쇠가 있다: {text!r}")
    for label in EDGE_LABEL.findall(source):
        for part in BREAK.split(label):
            if len(part.strip()) > settings["max_edge_label_chars_per_line"]:
                problems.append(f"[선 라벨 길이] 한 줄 {len(part.strip())}자(한도 {settings['max_edge_label_chars_per_line']}): "
                                f"{part.strip()!r}. 설명은 그림 아래 표로 옮긴다")
    return problems


# 기하 계산 -------------------------------------------------------------------

def shrink(box: dict, margin: float) -> tuple[float, float, float, float]:
    return box["x1"] + margin, box["y1"] + margin, box["x2"] - margin, box["y2"] - margin


def boxes_overlap(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def contains(outer, inner) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segment_intersection(p1, p2, q1, q2):
    """두 선분이 서로 엇갈려 만나면 교점을, 아니면 None을 돌려준다. 끝점이 닿기만 하는 경우는 세지 않는다."""
    d1, d2 = cross(q1, q2, p1), cross(q1, q2, p2)
    d3, d4 = cross(p1, p2, q1), cross(p1, p2, q2)
    if (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0) and d1 * d2 < 0 and d3 * d4 < 0:
        t = d1 / (d1 - d2)
        return p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1])
    return None


def segment_hits_box(p, q, box) -> bool:
    """선분이 상자 안을 지나는지 본다. 끝점이 안에 있거나, 상자의 네 변 중 하나와 엇갈리면 지난다."""
    x1, y1, x2, y2 = box
    if x1 >= x2 or y1 >= y2:
        return False
    inside = lambda pt: x1 < pt[0] < x2 and y1 < pt[1] < y2  # noqa: E731
    if inside(p) or inside(q):
        return True
    corners = [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]
    return any(segment_intersection(p, q, corners[i], corners[(i + 1) % 4]) for i in range(4))


def endpoints(edge_id: str, names: set[str]) -> tuple[str | None, str | None]:
    """선 id("L_<시작>_<끝>_<번호>")에서 시작·끝 노드 이름을 찾는다. 노드 이름에 밑줄이 있어도 나눌 수 있게 모든 자리를 시도한다."""
    body = re.sub(r"^L_|_\d+$", "", edge_id)
    parts = body.split("_")
    for i in range(1, len(parts)):
        a, b = "_".join(parts[:i]), "_".join(parts[i:])
        if a in names and b in names:
            return a, b
    return None, None


def far_from(point, anchors, margin) -> bool:
    return all((point[0] - x) ** 2 + (point[1] - y) ** 2 > margin ** 2 for x, y in anchors)


def judge(geometry: dict, settings: dict) -> list[str]:
    """5. 측정한 좌표로 교차·겹침을 판정한다."""
    problems = []
    nodes = {n["name"]: shrink(n["box"], settings["node_margin_px"]) for n in geometry["nodes"]}
    names = set(nodes)
    edges = []
    for edge in geometry["edges"]:
        src, dst = endpoints(edge["id"], names)
        edges.append({"id": edge["id"], "label": f"{src or '?'}→{dst or '?'}", "ends": {src, dst},
                      "points": [tuple(p) for p in edge["points"]]})

    for i, a in enumerate(edges):
        anchors_a = [a["points"][0], a["points"][-1]]
        for b in edges[i + 1:]:
            anchors = anchors_a + [b["points"][0], b["points"][-1]]
            hit = next((pt for p1, p2 in zip(a["points"], a["points"][1:])
                        for q1, q2 in zip(b["points"], b["points"][1:])
                        if (pt := segment_intersection(p1, p2, q1, q2)) and far_from(pt, anchors, settings["endpoint_margin_px"])),
                       None)
            if hit:
                problems.append(f"[선 교차] {a['label']} 와 {b['label']} 가 ({hit[0]:.0f}, {hit[1]:.0f})에서 엇갈린다")

    for edge in edges:
        for name, box in nodes.items():
            if name in edge["ends"]:
                continue
            if any(segment_hits_box(p, q, box) for p, q in zip(edge["points"], edge["points"][1:])):
                problems.append(f"[상자 관통] 선 {edge['label']} 가 상자 {name} 를 지난다")

    items = sorted(nodes.items())
    for i, (na, ba) in enumerate(items):
        for nb, bb in items[i + 1:]:
            if boxes_overlap(ba, bb):
                problems.append(f"[상자 겹침] {na} 와 {nb}")

    clusters = [(c["name"], shrink(c["box"], 0)) for c in geometry["clusters"]]
    for i, (na, ba) in enumerate(clusters):
        for nb, bb in clusters[i + 1:]:
            if boxes_overlap(ba, bb) and not contains(ba, bb) and not contains(bb, ba):
                problems.append(f"[묶음 상자 겹침] {na} 와 {nb}")

    labels = [(l["text"], l["edge"], shrink(l["box"], settings["label_margin_px"])) for l in geometry["labels"]]
    for text, _, box in labels:
        for name, node_box in nodes.items():
            if boxes_overlap(box, node_box):
                problems.append(f"[라벨 겹침] 선 라벨 {text!r} 가 상자 {name} 를 덮는다")
    for i, (ta, _, ba) in enumerate(labels):
        for tb, _, bb in labels[i + 1:]:
            if boxes_overlap(ba, bb):
                problems.append(f"[라벨 겹침] 선 라벨 {ta!r} 와 {tb!r}")
    for edge in edges:
        for text, owner, box in labels:
            if owner == edge["id"]:
                continue
            if any(segment_hits_box(p, q, box) for p, q in zip(edge["points"], edge["points"][1:])):
                problems.append(f"[라벨 관통] 선 {edge['label']} 가 다른 선의 라벨 {text!r} 를 지난다")
    return problems


def render_and_measure(blocks: list[dict], node_modules: Path, settings: dict, workdir: Path) -> None:
    """3·4. 블록마다 SVG를 만들고, 성공한 SVG를 한 번의 브라우저 실행으로 모두 잰다."""
    mmdc = node_modules / ".bin" / "mmdc"
    config = TOOL_DIR / "puppeteer.json"
    svgs = {}
    for index, block in enumerate(blocks):
        source, svg = workdir / f"{index}.mmd", workdir / f"{index}.svg"
        source.write_text(block["source"], encoding="utf-8")
        run = subprocess.run([str(mmdc), "-p", str(config), "-i", str(source), "-o", str(svg), "-q"],
                             capture_output=True, text=True, timeout=settings["render_timeout_s"])
        if run.returncode != 0 or not svg.exists():
            # mmdc 오류 출력에서 "Error"가 든 첫 줄이 원인(문법 오류 위치, 브라우저 실행 실패 등)이다.
            lines = (run.stderr or run.stdout).strip().splitlines()
            cause = next((line for line in lines if "Error" in line), lines[-1] if lines else f"mmdc 종료 코드 {run.returncode}")
            block["problems"].append(f"[렌더링 실패] {cause.strip()}")
        else:
            svgs[str(svg)] = block
    if svgs:
        measured = subprocess.run(["node", str(TOOL_DIR / "measure.mjs"), "--node-modules", str(node_modules),
                                   "--puppeteer-config", str(config), *svgs],
                                  check=True, capture_output=True, text=True, timeout=settings["render_timeout_s"] * 2)
        for path, geometry in json.loads(measured.stdout).items():
            svgs[path]["problems"] += judge(geometry, settings)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mermaid 그림 렌더링·겹침·교차 검사")
    parser.add_argument("--config", type=Path, required=True, help="검사 설정 JSON(diagrams 절)")
    parser.add_argument("--node-modules", type=Path, default=TOOL_DIR / "node_modules",
                        help="tools/diagram/package.json으로 설치한 node_modules. 기본은 tools/diagram/node_modules")
    args = parser.parse_args(argv)
    try:
        settings = load_config(args.config)["diagrams"]
        root = Path(subprocess.run(["git", "rev-parse", "--show-toplevel"], check=True,
                                   capture_output=True, text=True).stdout.strip())
        if not (args.node_modules / ".bin" / "mmdc").exists():
            raise FileNotFoundError(f"mmdc가 없습니다: {args.node_modules}. tools/diagram에서 npm ci를 실행하세요.")
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"실행 오류: {exc}", file=sys.stderr)
        return 2

    blocks = extract_blocks(root, settings)
    for block in blocks:
        block["problems"] = lint(block["source"], settings)
    try:
        with tempfile.TemporaryDirectory() as workdir:
            render_and_measure(blocks, args.node_modules.resolve(), settings, Path(workdir))
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"실행 오류(측정): {getattr(exc, 'stderr', '') or exc}", file=sys.stderr)
        return 2

    failed = [b for b in blocks if b["problems"]]
    print(f"검사 D 그림: {len(blocks)}개 중 {'모두 통과' if not failed else f'{len(failed)}개 실패'}")
    github = os.environ.get("GITHUB_ACTIONS") == "true"
    for block in failed:
        for problem in block["problems"]:
            print(f"::error::[D] {block['where']} {problem}" if github else f"  {block['where']} {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
