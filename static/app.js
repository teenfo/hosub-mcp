// 대시보드 오케스트레이터.
//
// 패널(위젯) 단위로 구성된다. 새 패널을 추가하려면:
//   1) static/panels/<이름>.js 를 만들어 default 로 패널 객체를 export
//   2) static/panels/index.js 의 PANELS 배열에 import 한 줄 추가
// 각 패널: { id, title, wide?, refreshMs, render(bodyEl) }  (render 는 async 가능)

import { PANELS } from "./panels/index.js";

// --- 공용 유틸 (패널에서 import 해서 사용) ---
export async function fetchJSON(path) {
  const res = await fetch(path, { headers: { Accept: "application/json" } });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  if (!res.ok) throw new Error("HTTP " + res.status);
  return res.json();
}

export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "html") node.innerHTML = v;
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

export function bar(percent, warnAt = 75, errAt = 90) {
  const cls = percent >= errAt ? "bar err" : percent >= warnAt ? "bar warn" : "bar";
  return el("div", { class: cls }, [
    el("span", { style: `width:${Math.max(0, Math.min(100, percent))}%` }),
  ]);
}

// --- 렌더링 ---
const grid = document.getElementById("grid");

function mountPanel(panel) {
  const card = el("div", { class: "card" + (panel.wide ? " wide" : "") });
  if (panel.wide) card.classList.add("wide");
  const body = el("div", { class: "card-body" });
  card.appendChild(el("h2", {}, panel.title));
  card.appendChild(body);
  grid.appendChild(card);

  async function tick() {
    try {
      await panel.render(body);
    } catch (e) {
      if (e && e.message === "unauthorized") return;
      body.innerHTML = "";
      body.appendChild(el("div", { class: "empty" }, "불러오기 실패: " + e.message));
    }
  }
  tick();
  if (panel.refreshMs) setInterval(tick, panel.refreshMs);
}

for (const panel of PANELS) mountPanel(panel);

// --- 상단 시계 ---
function updateClock() {
  document.getElementById("clock").textContent = new Date().toLocaleString("ko-KR");
}
updateClock();
setInterval(updateClock, 1000);

// --- 이스터에그 훅 (서버 변경 없이 프론트엔드만으로 확장) ---
// 코나미 코드로 파티 모드 토글. 추후 재미 요소를 여기에 연결.
(function easterEgg() {
  const seq = [
    "ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown",
    "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a",
  ];
  let i = 0;
  window.addEventListener("keydown", (e) => {
    i = e.key === seq[i] ? i + 1 : 0;
    if (i === seq.length) {
      document.body.classList.toggle("hosub-party");
      i = 0;
    }
  });
})();
