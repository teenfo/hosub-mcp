// 대시보드 셸 + 해시 라우터 (Bootstrap 5 admin, 멀티 페이지).
//
// 새 페이지 추가:
//   1) static/pages/<이름>.js 를 만들어 default 로 { id, title, icon, render(container, ctx) } export
//   2) static/pages/index.js 의 PAGES 배열에 import 한 줄 추가
// 사이드바 링크와 라우팅은 PAGES 로부터 자동 생성된다.
//
// 페이지 render(container, ctx): container 를 채운다. 주기 갱신이 필요하면
// ctx.addTimer(setInterval(...)) 로 등록하면 페이지 이동 시 자동 정리된다.

import { PAGES } from "./pages/index.js";

// --- 공용 유틸 (페이지/패널에서 import) ---
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

// 카드 컬럼 헬퍼: 페이지에서 카드 그리드를 쉽게 구성
export function card(titleHtml, bodyNode, { wide = false, icon = "" } = {}) {
  const col = el("div", { class: wide ? "col-12" : "col-12 col-xl-6" });
  const c = el("div", { class: "card shadow-sm h-100" });
  c.appendChild(
    el("div", { class: "card-header" }, el("span", { html: (icon ? `<i class="bi ${icon}"></i>` : "") + titleHtml }))
  );
  const body = el("div", { class: "card-body" });
  if (bodyNode) body.appendChild(bodyNode);
  c.appendChild(body);
  col.appendChild(c);
  return { col, body };
}

// 패널 배열(대시보드 페이지)을 카드로 마운트. ctx.addTimer 로 폴링 정리.
export function mountPanels(container, panels, ctx) {
  const row = el("div", { class: "row g-3" });
  container.appendChild(row);
  for (const panel of panels) {
    const { col, body } = card(panel.title, null, { wide: panel.wide, icon: panel.icon });
    col.id = "panel-" + panel.id;
    row.appendChild(col);
    const tick = async () => {
      try {
        await panel.render(body);
      } catch (e) {
        if (e && e.message === "unauthorized") return;
        body.innerHTML = "";
        body.appendChild(el("div", { class: "text-danger small" }, "불러오기 실패: " + e.message));
      }
    };
    tick();
    if (panel.refreshMs) ctx.addTimer(setInterval(tick, panel.refreshMs));
  }
}

// --- 사이드바 생성 ---
const nav = document.getElementById("sidebar-nav");
let lastGroup = null;
for (const p of PAGES) {
  // 그룹이 바뀌면 그룹 헤더를 한 번 넣고, 그룹 소속 페이지는 들여쓴다.
  if (p.group && p.group !== lastGroup) {
    nav.appendChild(
      el("li", { class: "nav-item mt-2" },
        el("span", { class: "nav-group-header d-block small text-secondary text-uppercase px-3 mb-1",
          html: `<i class="bi bi-collection"></i> ${p.group}` }))
    );
  }
  lastGroup = p.group || null;
  nav.appendChild(
    el("li", { class: "nav-item" }, [
      el("a", { class: "nav-link" + (p.group ? " ps-4" : ""), href: "#/" + p.id, "data-route": p.id },
        el("span", { html: `<i class="bi ${p.icon}"></i> ${p.title}` })),
    ])
  );
}

// --- 라우터 ---
const content = document.getElementById("content");
const pageTitle = document.getElementById("page-title");
let timers = [];
function clearTimers() {
  timers.forEach(clearInterval);
  timers = [];
}
function routeId() {
  return location.hash.replace(/^#\/?/, "") || PAGES[0].id;
}
async function renderRoute() {
  clearTimers();
  const id = routeId();
  const page = PAGES.find((p) => p.id === id) || PAGES[0];
  content.innerHTML = "";
  // 사이드바 활성화
  document.querySelectorAll("#sidebar-nav .nav-link").forEach((l) =>
    l.classList.toggle("active", l.getAttribute("data-route") === page.id)
  );
  pageTitle.innerHTML = `<i class="bi ${page.icon}"></i> ${page.title}`;
  // 모바일에서 페이지 선택 시 사이드바 닫기
  const oc = window.bootstrap && bootstrap.Offcanvas.getInstance(document.getElementById("sidebar"));
  if (oc) oc.hide();
  const ctx = { addTimer: (t) => timers.push(t) };
  try {
    await page.render(content, ctx);
  } catch (e) {
    if (e && e.message === "unauthorized") return;
    content.appendChild(el("div", { class: "alert alert-danger" }, "페이지 오류: " + e.message));
  }
}
window.addEventListener("hashchange", renderRoute);
renderRoute();

// --- 테마 토글 ---
const themeBtn = document.getElementById("theme-toggle");
function applyThemeIcon() {
  const cur = document.documentElement.getAttribute("data-bs-theme");
  themeBtn.innerHTML = cur === "dark" ? '<i class="bi bi-sun"></i>' : '<i class="bi bi-moon-stars"></i>';
}
applyThemeIcon();
themeBtn.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-bs-theme") === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-bs-theme", next);
  localStorage.setItem("hosub-theme", next);
  applyThemeIcon();
});

// --- 시계 ---
function updateClock() {
  const c = document.getElementById("clock");
  if (c) c.textContent = new Date().toLocaleString("ko-KR");
}
updateClock();
setInterval(updateClock, 1000);

// --- 이스터에그 (코나미 → 파티) ---
(function easterEgg() {
  const seq = ["ArrowUp", "ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowLeft", "ArrowRight", "b", "a"];
  let i = 0;
  window.addEventListener("keydown", (e) => {
    i = e.key === seq[i] ? i + 1 : 0;
    if (i === seq.length) {
      document.body.classList.toggle("hosub-party");
      i = 0;
    }
  });
})();
