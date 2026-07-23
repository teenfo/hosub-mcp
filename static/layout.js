// 카드 그리드 레이아웃 편집기 — 외부 의존성 없음.
// '레이아웃 편집' 모드에서 카드를 끌어 배치하고, 우측/하단/모서리 핸들을
// 끌어 가로(12칼럼 단위)·세로(px) 크기를 바꾼다. 결과는 브라우저별
// localStorage 에 저장되어 새로고침 후에도 유지된다.
//
// 사용:
//   makeLayoutEditable(row, { key: "trading", toolbar });
//   - row: 카드 .col 들을 자식으로 가진 .row (각 .col 에 dataset.cardId 필요)
//   - key: 저장 키 네임스페이스
//   - toolbar: 편집/초기화 버튼을 넣을 컨테이너 (없으면 row 앞에 생성)

import { el } from "./app.js";

const MIN_W = 3;   // 최소 3칼럼(너무 좁은 카드 방지)
const MAX_W = 12;
const MIN_H = 140; // 최소 카드 높이(px)

function loadState(key) {
  try {
    return JSON.parse(localStorage.getItem("hosub:layout:" + key + ":v1")) || {};
  } catch {
    return {};
  }
}
function saveState(key, state) {
  try {
    localStorage.setItem("hosub:layout:" + key + ":v1", JSON.stringify(state));
  } catch {
    /* 용량 초과 등은 조용히 무시 */
  }
}

function applyWidth(col, n) {
  col.className = col.className
    .replace(/\bcol-xl-\d+\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!/\bcol-12\b/.test(col.className)) col.classList.add("col-12");
  col.classList.add("col-xl-" + Math.max(MIN_W, Math.min(MAX_W, n)));
}

function applyHeight(col, px) {
  const c = col.querySelector(".card");
  if (!c) return;
  if (px) {
    c.classList.remove("h-100");
    c.classList.add("card-fixed-h");
    c.style.height = Math.max(MIN_H, px) + "px";
  } else {
    c.classList.remove("card-fixed-h");
    c.style.height = "";
    c.classList.add("h-100");
  }
}

export function makeLayoutEditable(row, { key, toolbar }) {
  const cols = () => [...row.querySelectorAll(".col[data-card-id]")];
  const defaults = new Map(               // 초기화용 기본 폭(현재 클래스 기준)
    cols().map((col) => {
      const m = col.className.match(/\bcol-xl-(\d+)\b/);
      return [col.dataset.cardId, m ? +m[1] : 12];
    })
  );
  const state = loadState(key);
  state.order ||= [];
  state.width ||= {};
  state.height ||= {};

  const byId = () => Object.fromEntries(cols().map((c) => [c.dataset.cardId, c]));

  const applyAll = () => {
    const map = byId();
    // 순서 복원
    for (const id of state.order) if (map[id]) row.appendChild(map[id]);
    // 폭·높이 복원
    for (const col of cols()) {
      const id = col.dataset.cardId;
      applyWidth(col, state.width[id] ?? defaults.get(id) ?? 12);
      applyHeight(col, state.height[id] || 0);
    }
  };

  const persistOrder = () => {
    state.order = cols().map((c) => c.dataset.cardId);
    saveState(key, state);
  };

  // --- 크기 조절 핸들 (편집 모드에서만 보임) ---
  const addHandles = (col) => {
    if (col.querySelector(".layout-handle")) return;
    const mkHandle = (cls, mode) => {
      const h = el("div", { class: "layout-handle " + cls });
      h.addEventListener("pointerdown", (ev) => startResize(ev, col, mode));
      col.querySelector(".card").appendChild(h);
    };
    mkHandle("layout-handle-r", "w");   // 우측 → 폭
    mkHandle("layout-handle-b", "h");   // 하단 → 높이
    mkHandle("layout-handle-c", "wh");  // 모서리 → 둘 다
  };

  let resizing = null;
  function startResize(ev, col, mode) {
    ev.preventDefault();
    ev.stopPropagation();
    const card = col.querySelector(".card");
    resizing = {
      col, mode,
      startX: ev.clientX, startY: ev.clientY,
      startH: card.getBoundingClientRect().height,
      rowRect: row.getBoundingClientRect(),
      startW: state.width[col.dataset.cardId] ?? defaults.get(col.dataset.cardId) ?? 12,
    };
    ev.target.setPointerCapture?.(ev.pointerId);
    window.addEventListener("pointermove", onResize);
    window.addEventListener("pointerup", endResize, { once: true });
  }
  function onResize(ev) {
    if (!resizing) return;
    const { col, mode, startX, startY, startH, rowRect, startW } = resizing;
    if (mode.includes("w")) {
      const colPx = rowRect.width / 12;
      const n = Math.round(startW + (ev.clientX - startX) / colPx);
      applyWidth(col, n);
    }
    if (mode.includes("h")) {
      applyHeight(col, Math.round(startH + (ev.clientY - startY)));
    }
  }
  function endResize() {
    if (!resizing) return;
    const { col } = resizing;
    const id = col.dataset.cardId;
    const m = col.className.match(/\bcol-xl-(\d+)\b/);
    if (m) state.width[id] = +m[1];
    const card = col.querySelector(".card");
    if (card.classList.contains("card-fixed-h")) {
      state.height[id] = Math.round(card.getBoundingClientRect().height);
    }
    resizing = null;
    window.removeEventListener("pointermove", onResize);
    saveState(key, state);
  }

  // --- 드래그 재배치 (HTML5 DnD) ---
  let dragEl = null;
  const onDragStart = (e) => {
    const col = e.target.closest(".col[data-card-id]");
    if (!col) return;
    dragEl = col;
    col.classList.add("layout-dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", col.dataset.cardId); } catch {}
  };
  const onDragEnd = () => {
    if (dragEl) dragEl.classList.remove("layout-dragging");
    dragEl = null;
    persistOrder();
  };
  const onDragOver = (e) => {
    if (!dragEl) return;
    e.preventDefault();
    const target = e.target.closest?.(".col[data-card-id]");
    if (!target || target === dragEl) return;
    const box = target.getBoundingClientRect();
    const after = e.clientX > box.left + box.width / 2;
    row.insertBefore(dragEl, after ? target.nextSibling : target);
  };

  let editing = false;
  const setEditing = (on) => {
    editing = on;
    row.classList.toggle("layout-editing", on);
    for (const col of cols()) {
      col.draggable = on;
      if (on) addHandles(col);
    }
    editBtn.textContent = on ? "편집 종료" : "레이아웃 편집";
    editBtn.className = "btn btn-sm " + (on ? "btn-primary" : "btn-outline-secondary");
    resetBtn.classList.toggle("d-none", !on);
    hint.classList.toggle("d-none", !on);
  };

  // --- 툴바 ---
  const editBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "레이아웃 편집");
  const resetBtn = el("button", { class: "btn btn-sm btn-outline-danger d-none", type: "button" }, "기본 배치로 초기화");
  const hint = el("span", { class: "small text-secondary d-none" },
    "카드를 끌어 배치 · 오른쪽/아래/모서리 핸들을 끌어 크기 조절");
  editBtn.addEventListener("click", () => setEditing(!editing));
  resetBtn.addEventListener("click", () => {
    state.order = [];
    state.width = {};
    state.height = {};
    saveState(key, state);
    for (const col of cols()) {
      applyWidth(col, defaults.get(col.dataset.cardId) ?? 12);
      applyHeight(col, 0);
    }
    // 원래 DOM 순서로 되돌리기 위해 dataset.cardIndex 기준 정렬
    cols()
      .sort((a, b) => (+a.dataset.cardIndex || 0) - (+b.dataset.cardIndex || 0))
      .forEach((c) => row.appendChild(c));
  });
  const bar = toolbar || el("div");
  bar.classList.add("d-flex", "align-items-center", "gap-2", "mb-2", "flex-wrap");
  bar.append(editBtn, resetBtn, hint);
  if (!toolbar) row.parentNode.insertBefore(bar, row);

  row.addEventListener("dragstart", onDragStart);
  row.addEventListener("dragend", onDragEnd);
  row.addEventListener("dragover", onDragOver);

  applyAll();
  return { setEditing };
}
