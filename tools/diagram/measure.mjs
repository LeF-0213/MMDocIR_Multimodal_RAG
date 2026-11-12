// Mermaid가 그린 SVG를 헤드리스 브라우저에 띄워 상자·묶음 상자·선·선 라벨의 화면 좌표를 재는 스크립트다.
//
// 전체 위치: 문서의 mermaid 블록 → mmdc로 SVG 렌더링 → 이 스크립트(좌표 측정) → tools/diagram_check.py(겹침·교차 판정)
// 입력: --node-modules <폴더>(mermaid-cli·puppeteer가 설치된 node_modules), --puppeteer-config <JSON>(브라우저 실행 옵션),
//       뒤에 SVG 파일 경로들.
// 출력: 표준 출력에 JSON 한 개. {"<svg 경로>": {nodes, clusters, edges, labels}}. 좌표 단위는 화면 픽셀이다.
// 부작용: 파일을 만들거나 바꾸지 않는다. 헤드리스 브라우저를 띄웠다가 닫는다.
//
// 좌표를 SVG 문자열에서 직접 계산하지 않고 브라우저에서 재는 이유: 원통·마름모·평행사변형처럼 도형마다 경로 명령이 다르고
// 묶음 상자마다 이동 변환이 겹친다. 브라우저의 getBoundingClientRect·getPointAtLength는 이 변환을 모두 반영한 좌표를 준다.

// Node 표준 모듈: createRequire는 지정한 node_modules에서 puppeteer를 불러오고, readFileSync는 SVG 파일을 읽는다.
// 이 스크립트를 node_modules와 다른 폴더에 두기 위해 경로를 인자로 받는다.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// 선 하나를 이 간격(px)으로 잘라 점 목록으로 만든다. 작을수록 곡선을 정확히 따라가지만 판정이 느려진다.
// 그림 크기가 수백~천여 px라 4px면 선 하나에 수백 점이고, 교차 판정에 충분하다.
const SAMPLE_STEP = 4;

function parseArgs(argv) {
  const args = { files: [] };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--node-modules") args.nodeModules = argv[++i];
    else if (argv[i] === "--puppeteer-config") args.puppeteerConfig = argv[++i];
    else args.files.push(argv[i]);
  }
  if (!args.nodeModules || !args.puppeteerConfig || args.files.length === 0) {
    throw new Error("사용법: measure.mjs --node-modules DIR --puppeteer-config FILE SVG...");
  }
  return args;
}

// 브라우저 안에서 실행되는 함수다. 바깥 변수를 쓸 수 없으므로 표본 간격을 인자로 받는다.
function measurePage(step) {
  const box = (el) => {
    const r = el.getBoundingClientRect();
    return { x1: r.left, y1: r.top, x2: r.right, y2: r.bottom };
  };
  // 상자 id는 "<svg id>-flowchart-<노드 이름>-<번호>" 형식이다. 노드 이름만 남겨 선의 양 끝과 맞춘다.
  const nodes = [...document.querySelectorAll("g.node")].map((g) => ({
    name: g.id.replace(/^.*?-flowchart-/, "").replace(/-\d+$/, ""),
    box: box(g),
  }));
  const clusters = [...document.querySelectorAll("g.cluster")].map((g) => ({
    name: g.id || g.getAttribute("data-id") || "",
    box: box(g.querySelector("rect") || g),
  }));
  const edges = [...document.querySelectorAll("path[data-edge='true']")].map((path) => {
    const total = path.getTotalLength();
    const ctm = path.getScreenCTM();
    const points = [];
    for (let t = 0; t <= total; t += step) {
      const p = path.getPointAtLength(t).matrixTransform(ctm);
      points.push([p.x, p.y]);
    }
    const end = path.getPointAtLength(total).matrixTransform(ctm);
    points.push([end.x, end.y]);
    return { id: path.getAttribute("data-id") || path.id, points };
  });
  // 글자가 있는 선 라벨만 잰다. 라벨이 없는 선도 빈 라벨 요소가 생기기 때문이다.
  const labels = [...document.querySelectorAll("g.edgeLabel")]
    .filter((g) => g.textContent.trim() !== "")
    .map((g) => ({
      edge: (g.querySelector("[data-id]") || g).getAttribute("data-id") || "",
      text: g.textContent.trim(),
      box: box(g),
    }));
  return { nodes, clusters, edges, labels };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const require = createRequire(resolve(args.nodeModules) + "/");
  const puppeteer = require("puppeteer");
  const launch = JSON.parse(readFileSync(args.puppeteerConfig, "utf-8"));
  const browser = await puppeteer.launch(launch);
  const result = {};
  try {
    const page = await browser.newPage();
    // 그림이 잘리지 않도록 넉넉한 화면 크기를 준다. 좌표는 화면 왼쪽 위가 원점이다.
    await page.setViewport({ width: 4000, height: 4000 });
    for (const file of args.files) {
      const svg = readFileSync(file, "utf-8");
      await page.setContent(`<!doctype html><html><body style="margin:0">${svg}</body></html>`, { waitUntil: "load" });
      result[file] = await page.evaluate(measurePage, SAMPLE_STEP);
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(`측정 실패: ${error.message}\n`);
  process.exit(2);
});
