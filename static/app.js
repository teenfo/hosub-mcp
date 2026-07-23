// 대시보드 오케스트레이터 (Bootstrap 5 admin 테마).
//
// 패널(위젯) 단위로 구성된다. 새 패널을 추가하려면:
//   1) static/panels/<이름>.js 를 만들어 default 로 패널 객체를 export
//   2) static/panels/index.js 의 PANELS 배열에 import 한 줄 추가
// 패널 객체: { id, title, icon?, wide?, refreshMs, render(bodyEl) }

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
    if (v == null) continue;
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

// Bootstrap progress 바. tone: null(자동)/success/warning/danger
export function bar(percent, warnAt = 75, errAt = 90) {
  const p = Math.max(0, Math.min(100, percent));
  const tone = p >= errAt ? "danger" : p >= warnAt ? "warning" : "success";
  const wrap = el("div", { class: "progress", role: "progressbar", style: "height:8px" });
  wrap.appendChild(el("div", { class: `progress-bar text-bg-${tone}`, style: `width:${p}%` }));
  return wrap;
}

export function badge(text, tone) {
  return el("span", { class: `badge text-bg-${tone}` }, text);
}

// --- 렌더링: 패널을 Bootstrap 카드+컬럼으로 마운트 ---
const grid = document.getElementById("grid");

function mountPanel(panel) {
  const col = el("div", {
    class: panel.wide ? "col-12" : "col-12 col-xl-6",
    id: "panel-" + panel.id,
  });
  const card = el("div", { class: "card shadow-sm h-100" });
  const header = el("div", { class: "card-header d-flex align-items-center" }, [
    el("span", { html: (panel.icon ? `<i class="bi ${panel.icon}"></i>` : "") + panel.title }),
  ]);
  const body = el("div", { class: "card-body" });
  card.appendChild(header);
  card.appendChild(body);
  col.appendChild(card);
  grid.appendChild(col);

  async function tick() {
    try {
      await panel.render(body);
    } catch (e) {
      if (e && e.message === "unauthorized") return;
      body.innerHTML = "";
      body.appendChild(
        el("div", { class: "text-danger small" }, "불러오기 실패: " + e.message)
      );
    }
  }
  tick();
  if (panel.refreshMs) setInterval(tick, panel.refreshMs);
}

for (const panel of PANELS) mountPanel(panel);

// --- 사이드바 네비 활성화 표시 ---
document.querySelectorAll("#sidebar-nav .nav-link").forEach((link) => {
  link.addEventListener("click", () => {
    document.querySelectorAll("#sidebar-nav .nav-link").forEach((l) => l.classList.remove("active"));
    link.classList.add("active");
  });
});

// --- 테마 토글 ---
const themeBtn = document.getElementById("theme-toggle");
function applyThemeIcon() {
  const cur = document.documentElement.getAttribute("data-bs-theme");
  themeBtn.innerHTML = cur === "dark"
    ? '<i class="bi bi-sun"></i>'
    : '<i class="bi bi-moon-stars"></i>';
}
applyThemeIcon();
themeBtn.addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-bs-theme");
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-bs-theme", next);
  localStorage.setItem("hosub-theme", next);
  applyThemeIcon();
});

// --- 상단 시계 ---
function updateClock() {
  const c = document.getElementById("clock");
  if (c) c.textContent = new Date().toLocaleString("ko-KR");
}
updateClock();
setInterval(updateClock, 1000);

// --- 이스터에그 훅 (코나미 코드 → 파티 모드) ---
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
