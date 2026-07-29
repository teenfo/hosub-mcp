// 트레이딩 그룹 페이지(매매 데스크·발굴감시·성과백테스트) 공용 헬퍼.
import { badge, el, fetchJSON } from "../app.js";

export async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

export const fmt = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });
export const won = (n) => (n > 0 ? "+" : "") + fmt(n) + "원";
export const pct = (n) => (n > 0 ? "+" : "") + n + "%";

// 가격 셀 렌더: 현재가 + (진입가 있으면) 괴리%. 초기 렌더·부분 갱신 공용.
export function priceCellHTML(cell, price, entry) {
  if (price == null || price === "" || isNaN(Number(price))) { cell.textContent = "—"; return; }
  entry = Number(entry) || 0;
  if (entry) {
    const gap = (price - entry) / entry * 100;
    cell.innerHTML = `<span class="fw-semibold">${fmt(price)}</span>` +
      `<span class="small ms-1 ${gap >= 0 ? "text-danger" : "text-primary"}">${gap >= 0 ? "+" : ""}${gap.toFixed(2)}%</span>`;
  } else {
    cell.textContent = fmt(price);
  }
}

// 생성 시각 → "N분 전" / 만료 시각 → "만료까지 M분" (staleness 표시)
export const agoStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return m + "분 전";
  return Math.floor(m / 60) + "시간 " + (m % 60) + "분 전";
};
export const leftStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  return m <= 0 ? "만료됨" : "만료까지 " + m + "분";
};

export const sideBadge = (side) =>
  badge(side === "short" ? "숏" : "롱", side === "short" ? "danger" : "success");

// 카드 안 탭 그리드. 성격이 다른 표를 한 카드에 쌓지 않고 갈라, 지금 무엇을
// 보고 있는지가 항상 분명하게 한다(발굴·감시 페이지와 같은 방식).
//   defs: [{ id, label, icon, note }]
//   반환: { nav, panes, mount(parent), set(id, node, count), show(id) }
export function makeTabs(defs) {
  const nav = el("ul", { class: "nav nav-tabs nav-fill small mb-2" });
  const panes = {};
  const counts = {};
  let active = defs[0].id;
  const show = (id) => {
    active = id;
    defs.forEach((t) => {
      panes[t.id].classList.toggle("d-none", t.id !== id);
      nav.querySelector(`[data-tab="${t.id}"]`).classList.toggle("active", t.id === id);
    });
  };
  defs.forEach((t) => {
    const cnt = el("span", { class: "badge text-bg-secondary ms-1 d-none" }, "0");
    counts[t.id] = cnt;
    const link = el("button", {
      class: "nav-link" + (t.id === active ? " active" : ""),
      type: "button", "data-tab": t.id,
    }, [el("i", { class: `bi bi-${t.icon} me-1` }), t.label, cnt]);
    link.onclick = () => show(t.id);
    nav.appendChild(el("li", { class: "nav-item" }, link));
    panes[t.id] = el("div", { class: t.id === active ? "" : "d-none" }, [
      t.note ? el("div", { class: "small text-secondary mb-2", html: t.note }) : null,
      el("div", { class: "pane-body" }),
    ]);
  });
  return {
    nav, panes, show,
    mount(parent) { parent.append(nav, ...defs.map((t) => panes[t.id])); },
    /** 탭 본문 교체. count 를 주면 탭 제목에 개수 뱃지를 단다. */
    set(id, node, count) {
      const body = panes[id].querySelector(".pane-body");
      body.innerHTML = "";
      body.appendChild(node);
      const cnt = counts[id];
      cnt.classList.toggle("d-none", count == null);
      if (count == null) return;
      cnt.textContent = String(count);
      cnt.className = "badge ms-1 " + (count ? "text-bg-primary" : "text-bg-secondary");
    },
  };
}

// 변경 감지 메모 팩토리: 폴링 데이터가 실제로 바뀔 때만 DOM 재렌더(깜빡임 제거).
export function makeChanged() {
  const memo = {};
  const changed = (key, data) => {
    const s = JSON.stringify(data);
    if (memo[key] === s) return false;
    memo[key] = s;
    return true;
  };
  changed.invalidate = (key) => { memo[key] = undefined; };
  return changed;
}

/** 기어 모달 — 설정을 카드에서 빼내 필요할 때만 연다.
 *
 * 설정 카드를 페이지에 상시 노출하면 자주 보는 정보(관심종목·상태)가 밀린다.
 * `trading.js` 가 쓰던 형태를 여기로 올려 두 페이지가 같은 것을 쓰게 한다 —
 * 파편화가 실제 손실로 이어진 사례를 하루에 여러 번 봤다.
 *
 * 반환: { button, show, hide, body } — button 을 카드 헤더나 툴바에 붙인다.
 */
export function makeGearModal(container, title, { icon = "gear-fill", size = "modal-lg",
                                                  label = "설정" } = {}) {
  const body = el("div", { class: "modal-body pt-2" });
  const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
    el("div", { class: `modal-dialog modal-dialog-centered ${size} modal-dialog-scrollable` },
      el("div", { class: "modal-content" }, [
        el("div", { class: "modal-header py-2" }, [
          el("h5", { class: "modal-title", html: `<i class="bi bi-${icon}"></i> ${title}` }),
          el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
        ]),
        body,
        el("div", { class: "modal-footer py-2" },
          el("button", { class: "btn btn-sm btn-secondary", type: "button",
                         "data-bs-dismiss": "modal" }, "닫기")),
      ])));
  container.appendChild(modalEl);
  const modal = new bootstrap.Modal(modalEl);
  const button = el("button", {
    class: "btn btn-sm btn-outline-secondary", type: "button", title,
  }, [el("i", { class: `bi bi-${icon} me-1` }), label]);
  button.onclick = () => modal.show();
  return { button, body, show: () => modal.show(), hide: () => modal.hide() };
}


// ===================================================================
// 종목 표기 — 이름과 코드를 병기하고, 누르면 기본정보 모달을 띄운다
// ===================================================================
//
// 화면마다 종목을 다르게 그리고 있었다. 어떤 표는 이름만, 어떤 표는 코드만
// 보여줘 같은 종목인지 알아보기 어려웠다. 표기를 여기로 모은다.
//
// 클릭 처리는 **document 위임**이다. 페이지들이 행을 innerHTML 문자열로 만드는
// 곳과 el() 로 만드는 곳이 섞여 있어, 노드마다 onclick 을 다는 방식이면 절반은
// 적용되지 않는다. 위임이면 `data-stock` 속성만 있으면 어느 쪽이든 동작한다.

const _STOCK_MODAL = { el: null, modal: null, body: null, title: null };

/** "종목명 (코드)" 클릭 링크 — innerHTML 문자열용. */
export function stockHTML(code, name, { plain = false } = {}) {
  const c = String(code || "").trim();
  const n = String(name || "").trim();
  if (!/^\d{6}$/.test(c)) return n || c || "-";     // 코드가 없으면 링크가 아니다
  const label = n ? `${n} <span class="text-secondary">${c}</span>` : c;
  if (plain) return label;
  return `<a href="#" class="stock-link text-decoration-none" data-stock="${c}"`
       + ` data-stock-name="${n.replace(/"/g, "&quot;")}"`
       + ` title="기본정보 보기">${label}</a>`;
}

/** 같은 것의 노드 버전 — el() 로 표를 만드는 화면용. */
export function stockCell(code, name, opts) {
  return el("span", { html: stockHTML(code, name, opts) });
}

function _ensureModal() {
  if (_STOCK_MODAL.modal) return _STOCK_MODAL;
  const title = el("h5", { class: "modal-title" }, "종목 정보");
  const body = el("div", { class: "modal-body pt-2 small" });
  const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
    el("div", { class: "modal-dialog modal-dialog-centered modal-lg modal-dialog-scrollable" },
      el("div", { class: "modal-content" }, [
        el("div", { class: "modal-header py-2" }, [
          title,
          el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
        ]),
        body,
        el("div", { class: "modal-footer py-2" },
          el("button", { class: "btn btn-sm btn-secondary", type: "button",
                         "data-bs-dismiss": "modal" }, "닫기")),
      ])));
  document.body.appendChild(modalEl);
  Object.assign(_STOCK_MODAL, { el: modalEl, body, title,
                                modal: new bootstrap.Modal(modalEl) });
  return _STOCK_MODAL;
}

// 키움은 가격에 전일대비 방향 부호를 붙여 보낸다(현재가 '-208500' 은 마이너스
// 가격이 아니라 '하락'이라는 뜻이다). 가격은 절대값으로 읽고, 등락률·전일대비
// 처럼 **진짜 부호가 있는 값**만 그대로 쓴다. 이걸 헷갈리면 상한가가 음수로
// 보인다.
const _abs = (v) => {
  const n = Number(String(v ?? "").replace(/[+\-,\s]/g, ""));
  return Number.isFinite(n) ? n : null;
};
const _signed = (v) => {
  const n = Number(String(v ?? "").replace(/[,\s]/g, ""));
  return Number.isFinite(n) ? n : null;
};
const _money = (v) => { const n = _abs(v); return n == null ? "-" : fmt(n) + "원"; };
const _num = (v) => { const n = _abs(v); return n == null ? "-" : fmt(n); };
const _rate = (v) => {
  const n = _signed(v);
  return n == null ? "-" : `<span class="${n > 0 ? "text-danger" : n < 0 ? "text-primary" : ""}">`
       + `${n > 0 ? "+" : ""}${n}%</span>`;
};
const _dt = (v) => {
  const s = String(v ?? "");
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}.${s.slice(4, 6)}.${s.slice(6)}` : "-";
};
// 억 단위로 오는 값(시가총액·자본금·매출액 등) — 조 단위로 접어 읽기 쉽게.
const _eok = (v) => {
  const n = _abs(v);
  if (n == null) return "-";
  return n >= 10000 ? `${(n / 10000).toLocaleString("ko-KR", { maximumFractionDigits: 1 })}조원`
                    : `${fmt(n)}억원`;
};
const _cheonju = (v) => { const n = _abs(v); return n == null ? "-" : fmt(n) + "천주"; };

// [라벨, 필드, 포맷] — 실호출(2026-07-29 ka10001)로 확인한 필드만 쓴다.
const BASIC_GROUPS = [
  ["시세", [
    ["현재가", "cur_prc", _money], ["전일대비", "pred_pre", (v) => {
      const n = _signed(v);
      return n == null ? "-" : `<span class="${n > 0 ? "text-danger" : n < 0 ? "text-primary" : ""}">`
           + `${n > 0 ? "+" : ""}${fmt(n)}원</span>`;
    }],
    ["등락률", "flu_rt", _rate], ["거래량", "trde_qty", _num],
    ["시가", "open_pric", _money], ["고가", "high_pric", _money],
    ["저가", "low_pric", _money], ["기준가", "base_pric", _money],
    ["상한가", "upl_pric", _money], ["하한가", "lst_pric", _money],
  ]],
  ["규모", [
    ["시가총액", "mac", _eok], ["자본금", "cap", _eok],
    ["상장주식수", "flo_stk", _cheonju], ["유통주식", "dstr_stk", _cheonju],
    ["유통비율", "dstr_rt", (v) => (_abs(v) == null ? "-" : _abs(v) + "%")],
    ["외국인소진율", "for_exh_rt", (v) => (_abs(v) == null ? "-" : _abs(v) + "%")],
    ["신용비율", "crd_rt", (v) => (_abs(v) == null ? "-" : _abs(v) + "%")],
    ["액면가", "fav", (v, b) => (_abs(v) == null ? "-" : fmt(_abs(v)) + (b.fav_unit || "원"))],
  ]],
  ["가치지표", [
    ["PER", "per", _num], ["PBR", "pbr", _num], ["ROE", "roe", (v) => (_abs(v) ?? "-") + "%"],
    ["EPS", "eps", _money], ["BPS", "bps", _money], ["EV/EBITDA", "ev", _num],
    ["매출액", "sale_amt", _eok], ["영업이익", "bus_pro", _eok],
    ["당기순이익", "cup_nga", _eok], ["결산월", "setl_mm", (v) => (v ? v + "월" : "-")],
  ]],
  ["52주(250일)", [
    ["최고", "250hgst", _money], ["최고일", "250hgst_pric_dt", _dt],
    ["최고 대비", "250hgst_pric_pre_rt", _rate],
    ["최저", "250lwst", _money], ["최저일", "250lwst_pric_dt", _dt],
    ["최저 대비", "250lwst_pric_pre_rt", _rate],
  ]],
];

async function _render(code, name) {
  const m = _ensureModal();
  m.title.innerHTML = `<i class="bi bi-graph-up me-1"></i>${name || ""} `
                    + `<span class="text-secondary">${code}</span>`;
  m.body.innerHTML = '<div class="text-secondary">불러오는 중…</div>';
  m.modal.show();
  let d;
  try {
    d = await fetchJSON(`/api/trading/stock/${code}`);
  } catch (e) {
    m.body.innerHTML = `<div class="text-danger">기본정보 조회 실패: ${e.message}</div>`
      + '<div class="text-secondary mt-1">키움 API 연결·레이트리밋을 확인하세요.</div>';
    return;
  }
  const b = d.basic || {};
  m.title.innerHTML = `<i class="bi bi-graph-up me-1"></i>${b.stk_nm || name || ""} `
                    + `<span class="text-secondary">${code}</span>`;
  m.body.innerHTML = "";

  // 맨 위 한 줄 — 지금 갖고 있는가, 얼마인가. 나머지는 그 다음이다.
  const head = [];
  if (d.held) head.push(`<span class="badge text-bg-danger">보유 ${fmt(d.held)}주</span>`);
  else if (d.held === 0) head.push('<span class="badge text-bg-light text-dark">미보유</span>');
  if (d.price != null) head.push(`<span class="fs-5 fw-semibold">${fmt(d.price)}원</span>`);
  if (b.flu_rt != null) head.push(_rate(b.flu_rt));
  m.body.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-3", html: head.join(" ") }));

  for (const [group, rows] of BASIC_GROUPS) {
    const cells = rows
      .filter(([, key]) => String(b[key] ?? "").trim() !== "")
      .map(([label, key, fn]) => `<div class="col-6 col-md-3 mb-1">`
        + `<div class="text-secondary" style="font-size:.72rem">${label}</div>`
        + `<div>${fn(b[key], b)}</div></div>`);
    if (!cells.length) continue;
    m.body.appendChild(el("div", { class: "mb-2" }, [
      el("div", { class: "fw-semibold border-bottom pb-1 mb-1" }, group),
      el("div", { class: "row g-0", html: cells.join("") }),
    ]));
  }
  m.body.appendChild(el("div", { class: "text-secondary mt-2", style: "font-size:.72rem" },
    `키움 ka10001 · ${d.cached ? "캐시(최대 5분)" : "방금 조회"}`
    + " — 가격은 실시간 스냅샷, 나머지는 기본정보"));
}

/** 종목 기본정보 모달 열기. 페이지가 직접 부를 수도 있다. */
export function openStockModal(code, name) {
  if (/^\d{6}$/.test(String(code || ""))) _render(String(code), name || "");
}

// document 위임 — 어느 페이지에서 만든 링크든 한 번의 등록으로 동작한다.
if (!window.__stockLinkBound) {
  window.__stockLinkBound = true;
  document.addEventListener("click", (ev) => {
    const a = ev.target.closest?.("[data-stock]");
    if (!a) return;
    ev.preventDefault();
    ev.stopPropagation();      // 행 클릭(상세 열기)과 겹치지 않게
    openStockModal(a.dataset.stock, a.dataset.stockName || "");
  });
}
