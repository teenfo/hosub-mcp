// 트레이딩 그룹 페이지(매매 데스크·발굴감시·성과백테스트) 공용 헬퍼.
import { badge, el, fetchJSON } from "../app.js";
import { createProChart, MA_DEFS } from "../chart.js";

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
  if (!res.ok) {
    // 본문을 오류에 붙여 둔다. 편입 게이트(409)처럼 **거절 사유와 측정값을
    // 함께 주는** 응답이 있는데, 메시지 문자열만 남기면 화면이 '그래도 추가'
    // 같은 다음 행동을 만들 수 없다.
    const err = new Error(data.error || "HTTP " + res.status);
    err.status = res.status;
    err.data = data;
    throw err;
  }
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

// 모달은 탭 4개(기본정보·1분봉·뉴스공시·엔진판단)로 나뉜다 — 매매 데스크에
// 상시 노출되던 1분봉 카드를 여기로 이관(사용자 결정 2026-08-04). 상시 폴링이
// 사라지고 모달이 열려 있는 동안만 갱신하므로 API 부담은 오히려 준다.
// loaded 는 코드 전환 시 비우는 지연 로드 플래그 — 탭을 열 때만 조회한다.
const _STOCK_MODAL = { el: null, modal: null, title: null, tabs: null, tierBox: null,
                       code: "", name: "", loaded: {}, chart: null, timer: null };

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
  // 종목명 옆 tier 뱃지 + 매매/수집전용 전환 버튼 자리(사용자 결정 2026-08-04).
  const tierBox = el("span", { class: "d-flex align-items-center gap-2" });
  const body = el("div", { class: "modal-body pt-2 small" });
  // 차트 탭 때문에 modal-xl — lg 폭에서는 1분봉이 읽히지 않는다.
  const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
    el("div", { class: "modal-dialog modal-dialog-centered modal-xl modal-dialog-scrollable" },
      el("div", { class: "modal-content" }, [
        el("div", { class: "modal-header py-2" }, [
          el("div", { class: "d-flex align-items-center flex-wrap gap-2" }, [title, tierBox]),
          el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
        ]),
        body,
        el("div", { class: "modal-footer py-2" },
          el("button", { class: "btn btn-sm btn-secondary", type: "button",
                         "data-bs-dismiss": "modal" }, "닫기")),
      ])));
  document.body.appendChild(modalEl);
  const tabs = makeTabs([
    { id: "info", label: "기본정보", icon: "info-circle" },
    { id: "chart", label: "1분봉 차트", icon: "graph-up" },
    { id: "news", label: "뉴스·공시", icon: "newspaper" },
    { id: "engine", label: "엔진 판단", icon: "cpu" },
  ]);
  tabs.mount(body);
  // makeTabs 의 show 는 클로저라 밖에서 감쌀 수 없다 — nav 버블링으로 탭 전환을
  // 잡아 지연 로드를 건다(버튼 자체 onclick 이 먼저 돌아 pane 은 이미 보인다).
  tabs.nav.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-tab]");
    if (b) _onStockTab(b.dataset.tab);
  });
  modalEl.addEventListener("hidden.bs.modal", _chartTimerStop);
  Object.assign(_STOCK_MODAL, { el: modalEl, title, tabs, tierBox,
                                modal: new bootstrap.Modal(modalEl) });
  return _STOCK_MODAL;
}

// ---- 매매/수집전용 전환 — 종목명 옆 뱃지+버튼 (감시목록 밖 종목은 표시만) ----

async function _loadTier(code) {
  const m = _STOCK_MODAL;
  m.tierBox.innerHTML = "";
  let entry = null;
  try {
    const d = await fetchJSON("/api/trading/watchlist");
    entry = (d.entries || []).find((e) => e.code === code) || null;
  } catch (e) { return; }   // 조회 실패면 전환 UI 를 그리지 않는다 — 오조작 방지
  if (m.code !== code) return;
  if (!entry) {
    m.tierBox.appendChild(el("span", { class: "badge text-bg-light text-dark",
      title: "감시목록에 없는 종목 — 편입은 발굴·감시 페이지에서" }, "감시목록에 없음"));
    return;
  }
  const cur = !!entry.collect_only;
  m.tierBox.appendChild(el("span",
    { class: "badge text-bg-" + (cur ? "secondary" : "success") },
    cur ? "수집전용" : "매매"));
  const btn = el("button", {
    class: "btn btn-sm btn-outline-" + (cur ? "success" : "secondary"), type: "button",
    title: cur ? "매매 대상으로 전환(신호·주문 활성)" : "수집전용으로 전환(매매 제외)",
  }, cur ? "매매로" : "수집전용으로");
  btn.onclick = async () => {
    // 매매 방향 전환만 확인을 받는다 — 신호·자동주문 대상이 되는 실거래
    // 방향이라 보수적으로(수집전용 전환은 매매를 좁히는 쪽이라 즉시).
    if (cur && !confirm(`${m.name || code} 을(를) 매매 대상으로 전환합니다.\n`
        + "신호 평가·주문 제안(자동승인 켜져 있으면 자동발주) 대상이 됩니다.")) return;
    btn.disabled = true;
    try {
      await postJSON("/api/trading/watchlist/mode", { code, collect_only: !cur });
      _loadTier(code);        // 서버 확정값으로 다시 그린다
    } catch (e) {
      alert("전환 실패: " + e.message);
      btn.disabled = false;
    }
  };
  m.tierBox.appendChild(btn);
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

async function _renderInfo(code, name) {
  const m = _STOCK_MODAL;
  m.tabs.set("info", el("div", { class: "text-secondary" }, "불러오는 중…"));
  let d;
  try {
    d = await fetchJSON(`/api/trading/stock/${code}`);
  } catch (e) {
    m.loaded.info = false;      // 다음에 탭을 다시 열면 재시도
    m.tabs.set("info", el("div", {
      html: `<div class="text-danger">기본정보 조회 실패: ${e.message}</div>`
        + '<div class="text-secondary mt-1">키움 API 연결·레이트리밋을 확인하세요.</div>' }));
    return;
  }
  if (m.code !== code) return;  // 로드 중 다른 종목으로 전환됨 — 늦은 응답은 버린다
  const b = d.basic || {};
  m.title.innerHTML = `<i class="bi bi-graph-up me-1"></i>${b.stk_nm || name || ""} `
                    + `<span class="text-secondary">${code}</span>`;
  const box = el("div");

  // 맨 위 한 줄 — 지금 갖고 있는가, 얼마인가. 나머지는 그 다음이다.
  const head = [];
  if (d.held) head.push(`<span class="badge text-bg-danger">보유 ${fmt(d.held)}주</span>`);
  else if (d.held === 0) head.push('<span class="badge text-bg-light text-dark">미보유</span>');
  if (d.price != null) head.push(`<span class="fs-5 fw-semibold">${fmt(d.price)}원</span>`);
  if (b.flu_rt != null) head.push(_rate(b.flu_rt));
  box.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-3", html: head.join(" ") }));

  for (const [group, rows] of BASIC_GROUPS) {
    const cells = rows
      .filter(([, key]) => String(b[key] ?? "").trim() !== "")
      .map(([label, key, fn]) => `<div class="col-6 col-md-3 mb-1">`
        + `<div class="text-secondary" style="font-size:.72rem">${label}</div>`
        + `<div>${fn(b[key], b)}</div></div>`);
    if (!cells.length) continue;
    box.appendChild(el("div", { class: "mb-2" }, [
      el("div", { class: "fw-semibold border-bottom pb-1 mb-1" }, group),
      el("div", { class: "row g-0", html: cells.join("") }),
    ]));
  }
  box.appendChild(el("div", { class: "text-secondary mt-2", style: "font-size:.72rem" },
    `키움 ka10001 · ${d.cached ? "캐시(최대 5분)" : "방금 조회"}`
    + " — 가격은 실시간 스냅샷, 나머지는 기본정보"));
  m.tabs.set("info", box);
}

// ---- 1분봉 탭 — 매매 데스크 카드에서 이관(동작 동일: 날짜·기간·BB·체결 마커) ----

function _chartTimerStop() {
  if (_STOCK_MODAL.timer) { clearInterval(_STOCK_MODAL.timer); _STOCK_MODAL.timer = null; }
}

function _ensureChartPane() {
  const m = _STOCK_MODAL;
  if (m.chart) return m.chart;
  const host = el("div", { style: "width:100%;height:52vh;min-height:300px" });
  const pro = createProChart(host, { up: "#d64545", down: "#3a6fd8", axis: "time" });
  const dateInp = el("input", { type: "date", class: "form-control form-control-sm w-auto",
                                title: "데이터가 있는 날짜만 선택됩니다" });
  const dateList = el("datalist", { id: "stock-modal-bar-dates" });
  dateInp.setAttribute("list", "stock-modal-bar-dates");
  const today = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "오늘");
  const periodGroup = el("div", { class: "btn-group btn-group-sm" });
  const periodBtns = [["30분", 30], ["1시간", 60], ["2시간", 120], ["전체", "all"]]
    .map(([lbl, n]) => {
      const btn = el("button", { class: "btn btn-outline-secondary", type: "button" }, lbl);
      btn.onclick = () => {
        pro.setVisibleCount(n);
        periodBtns.forEach((x) => x.classList.toggle("active", x === btn));
      };
      periodGroup.appendChild(btn);
      return btn;
    });
  const bb = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "볼린저밴드");
  let bbOn = false;
  bb.onclick = () => { bbOn = !bbOn; bb.classList.toggle("active", bbOn); pro.setIndicator("bb", bbOn); };
  const legend = el("div", { class: "small d-flex gap-2 flex-wrap align-items-center ms-auto" },
    MA_DEFS.map((d) => el("span", { style: `color:${d.color};font-weight:600` }, `━ MA${d.p}`))
      .concat([
        el("span", { style: "color:#7a5cff;font-weight:600",
          title: "실제 매수 체결가 — 삼각형 꼭짓점이 그 가격입니다" }, "▲ 매수"),
        el("span", { class: "text-secondary", style: "font-weight:600",
          title: "실제 매도 체결가(이익=빨강 / 손실=파랑). ~ 표시는 실체결 미확인(이론가)" },
          "▼ 매도"),
      ]));
  const st = { host, pro, dateInp, dateList, periodBtns, dates: [], date: "", key: "" };
  dateInp.onchange = () => {
    const v = dateInp.value;
    if (v && st.dates.length && !st.dates.includes(v)) {
      alert("해당 날짜의 분봉 데이터가 없습니다 (보유: " + st.dates.slice(0, 5).join(", ") + " …)");
      dateInp.value = st.date || st.dates[0] || "";
      return;
    }
    st.date = (v && v === st.dates[0]) ? "" : v;      // 최신일이면 실시간 모드
    _chartLoad(true);
  };
  today.onclick = () => { st.date = ""; dateInp.value = st.dates[0] || ""; _chartLoad(true); };
  m.tabs.set("chart", el("div", {}, [
    el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
      [dateInp, today, dateList, periodGroup, bb,
       el("span", { class: "small text-secondary" }, "휠 확대·드래그 이동·더블클릭 리셋"), legend]),
    host,
  ]));
  m.chart = st;
  return st;
}

async function _chartDates() {
  const m = _STOCK_MODAL, st = m.chart;
  try {
    st.dates = (await fetchJSON(`/api/trading/bars/${m.code}/dates`)).dates || [];
  } catch (e) { st.dates = []; }
  st.dateList.innerHTML = "";
  for (const day of st.dates) st.dateList.appendChild(el("option", { value: day }));
  if (st.dates.length) {
    st.dateInp.min = st.dates[st.dates.length - 1];
    st.dateInp.max = st.dates[0];
  }
  st.dateInp.value = st.date || (st.dates[0] || "");
  st.dateInp.title = st.dates.length
    ? `데이터 보유 ${st.dates.length}일 (${st.dates[st.dates.length - 1]} ~ ${st.dates[0]})`
    : "저장된 분봉 없음";
}

// 체결 오버레이 — 그날 이 종목을 실제로 얼마에 사고팔았는지 차트에 겹쳐 본다.
async function _chartMarks() {
  const m = _STOCK_MODAL, st = m.chart;
  const day = st.date || (st.dates[0] || new Date().toLocaleDateString("sv-SE"));
  try {
    st.pro.setMarkers(await fetchJSON(
      `/api/trading/trades/${m.code}?date=${encodeURIComponent(day)}`));
  } catch (e) { st.pro.setMarkers(null); }   // 조회 실패는 차트 자체를 막지 않는다
}

async function _chartLoad(force = false) {
  const m = _STOCK_MODAL, st = m.chart;
  if (!m.code || !st) return;
  try {
    const q = st.date ? `?tf=1m&date=${st.date}` : "?tf=1m&live=1";
    const bars = await fetchJSON(`/api/trading/bars/${m.code}${q}`);
    const key = m.code + "|" + st.date;
    if (force || key !== st.key) {
      st.key = key;
      st.pro.setData(bars);                       // 종목·날짜 전환 → 새로 그림
      st.periodBtns.forEach((x) => x.classList.remove("active"));
      _chartMarks();                              // 전환 시에만 체결 재조회
    } else {
      st.pro.update(bars);                        // 실시간 갱신 → 확대/이동 보존
    }
  } catch (e) { /* 조회 실패 — 다음 주기에 재시도 */ }
}

async function _showChartTab() {
  const m = _STOCK_MODAL;
  const st = _ensureChartPane();
  if (st.key.split("|")[0] !== m.code) {          // 종목 전환 — 날짜부터 다시
    st.date = "";
    await _chartDates();
  }
  await _chartLoad(true);
  // 실시간 갱신은 모달이 열려 있고 이 탭이 보이는 동안만(hidden/탭 전환 시 해제)
  // — 데스크 상시 카드 시절보다 API 부담이 줄어든 이유가 이 조건이다.
  _chartTimerStop();
  m.timer = setInterval(() => { if (!m.chart.date) _chartLoad(false); }, 4000);
}

// ---- 뉴스·공시 탭 — TNM 수집분(이 종목으로 매칭된 항목) ----

async function _loadNews(code) {
  const m = _STOCK_MODAL;
  m.tabs.set("news", el("div", { class: "text-secondary" }, "불러오는 중…"));
  let items;
  try {
    items = (await fetchJSON(`/api/tnm/items?ticker=${code}&limit=30`)).items || [];
  } catch (e) {
    m.loaded.news = false;
    m.tabs.set("news", el("div", { class: "text-danger" }, "TNM 조회 실패: " + e.message));
    return;
  }
  if (m.code !== code) return;
  if (!items.length) {
    m.tabs.set("news", el("div", { class: "text-secondary" }, "수집된 뉴스·공시 없음"), 0);
    return;
  }
  const tbl = el("table", { class: "table table-sm table-hover small mb-0" });
  tbl.appendChild(el("thead", {}, el("tr", {
    html: "<th>시각</th><th>출처</th><th>점수</th><th>분류</th><th>제목</th>" })));
  const tb = el("tbody");
  for (const it of items) {
    tb.appendChild(el("tr", {
      html: `<td class="text-secondary text-nowrap">${(it.published_at || "").slice(5, 16).replace("T", " ")}</td>`
        + `<td class="text-nowrap">${it.source || "-"}</td>`
        + `<td>${it.score ?? "-"}</td>`
        + `<td class="text-nowrap">${it.status === "ok" ? (it.category || "-") : it.status}</td>`
        + `<td>${it.url ? `<a href="${it.url}" target="_blank" rel="noopener">${it.title}</a>` : it.title}</td>` }));
  }
  tbl.appendChild(tb);
  m.tabs.set("news", el("div", { class: "table-responsive" }, tbl), items.length);
}

// ---- 엔진 판단 탭 — 발굴 엔진(tier·점수·소스·결정) + 매매 신호(차단 사유 포함) ----

async function _loadEngine(code) {
  const m = _STOCK_MODAL;
  m.tabs.set("engine", el("div", { class: "text-secondary" }, "불러오는 중…"));
  // 조회 둘은 서로 독립 — 한쪽 실패가 다른 쪽 표시를 막지 않는다.
  let sc = null, sigs = null;
  try { sc = await fetchJSON("/api/trading/scout"); } catch (e) { /* 아래에서 표기 */ }
  try { sigs = await fetchJSON("/api/trading/signals"); } catch (e) { /* 아래에서 표기 */ }
  if (m.code !== code) return;
  const box = el("div");
  const section = (label, node) => box.appendChild(el("div", { class: "mb-3" }, [
    el("div", { class: "fw-semibold border-bottom pb-1 mb-1" }, label), node]));

  if (!sc) {
    section("발굴 엔진", el("div", { class: "text-danger" }, "발굴 엔진 조회 실패"));
  } else {
    const wl = (sc.watchlist || []).find((w) => w.code === code);
    const cand = (sc.candidates || []).find((c) => c.code === code);
    const stateBits = [];
    if (wl) {
      stateBits.push(`<span class="badge text-bg-${wl.tier === "trade" ? "danger" : "secondary"}">`
        + `${wl.tier === "trade" ? "매매 감시" : "수집전용"}</span>`);
      if (wl.protected) stateBits.push('<span class="badge text-bg-info">보호(보유·수동)</span>');
    } else {
      stateBits.push('<span class="badge text-bg-light text-dark">감시목록에 없음</span>');
    }
    if (cand) {
      stateBits.push(`후보 점수 <b>${cand.score}</b> · 소스 ${cand.group_count}그룹`
        + ` (${(cand.sources || []).join(", ") || "-"})`);
    }
    section("발굴 엔진", el("div", { html: stateBits.join(" ") }));
    if (cand && cand.evidence && cand.evidence.length) {
      section("근거", el("div", { class: "text-secondary",
        html: [].concat(cand.evidence).map((s) => String(s)).join("<br>") }));
    }
    // 소스별 살아있는 신호 — 취합 전 원시 관점(어느 소스가 언제 봤는가)
    const srcRows = [];
    for (const [src, rows] of Object.entries(sc.by_source || {})) {
      for (const r of rows) if (r.code === code) srcRows.push({ src, ...r });
    }
    if (srcRows.length) {
      const tbl = el("table", { class: "table table-sm small mb-0" });
      tbl.appendChild(el("thead", {}, el("tr", {
        html: "<th>소스</th><th>종류</th><th>세기</th><th>유효세기</th><th>관측</th>" })));
      const tb = el("tbody");
      for (const r of srcRows.sort((a, b) => b.effective - a.effective)) {
        tb.appendChild(el("tr", {
          html: `<td>${r.src}</td><td>${r.kind || "-"}</td><td>${r.strength}</td>`
            + `<td>${r.effective}</td>`
            + `<td class="text-secondary">${(r.observed_at || "").slice(5, 16).replace("T", " ")}</td>` }));
      }
      tbl.appendChild(tb);
      section(`소스 신호 (${srcRows.length})`, el("div", { class: "table-responsive" }, tbl));
    }
    const decs = (sc.decisions || []).filter((d) => d.code === code).slice(0, 10);
    if (decs.length) {
      const tbl = el("table", { class: "table table-sm small mb-0" });
      const tb = el("tbody");
      for (const d of decs) {
        tb.appendChild(el("tr", {
          html: `<td class="text-secondary text-nowrap">${(d.ts || "").slice(5, 16).replace("T", " ")}</td>`
            + `<td>${d.action || "-"}</td><td>${d.reason || ""}</td>` }));
      }
      tbl.appendChild(tb);
      section("최근 승격·강등 결정", el("div", { class: "table-responsive" }, tbl));
    }
  }

  if (!sigs) {
    section("매매 신호", el("div", { class: "text-danger" }, "신호 조회 실패"));
  } else {
    const mine = (Array.isArray(sigs) ? sigs : []).filter((s) => s.symbol === code);
    if (!mine.length) {
      section("매매 신호(최근)", el("div", { class: "text-secondary" }, "최근 신호 없음"));
    } else {
      const tbl = el("table", { class: "table table-sm small mb-0" });
      tbl.appendChild(el("thead", {}, el("tr", {
        html: "<th>시각</th><th>규칙</th><th>수량</th><th>상태</th><th>비고</th>" })));
      const tb = el("tbody");
      for (const s of mine) {
        tb.appendChild(el("tr", {
          html: `<td class="text-secondary text-nowrap">${(s.ts || "").slice(11, 16)}</td>`
            + `<td>${s.rule || "-"}</td><td>${s.qty ?? "-"}</td>`
            + `<td>${s.actionable ? '<span class="badge text-bg-success">발주 대상</span>'
                                  : '<span class="badge text-bg-secondary">미발주</span>'}</td>`
            + `<td class="text-secondary">${s.note || s.guard_warn || ""}</td>` }));
      }
      tbl.appendChild(tb);
      section(`매매 신호(최근 ${mine.length}건)`, el("div", { class: "table-responsive" }, tbl));
    }
  }
  m.tabs.set("engine", box);
}

function _onStockTab(id) {
  const m = _STOCK_MODAL;
  if (!m.code) return;
  _chartTimerStop();               // 차트 탭을 떠나면 실시간 갱신도 멈춘다
  if (id === "info") {
    if (!m.loaded.info) { m.loaded.info = true; _renderInfo(m.code, m.name); }
  } else if (id === "chart") {
    _showChartTab();
  } else if (id === "news") {
    if (!m.loaded.news) { m.loaded.news = true; _loadNews(m.code); }
  } else if (id === "engine") {
    if (!m.loaded.engine) { m.loaded.engine = true; _loadEngine(m.code); }
  }
}

/** 종목 모달 열기. opts.tab 으로 시작 탭 지정(info|chart|news|engine). */
export function openStockModal(code, name, opts = {}) {
  const c = String(code || "").trim();
  if (!/^\d{6}$/.test(c)) return;
  const m = _ensureModal();
  if (m.code !== c) {              // 종목 전환 — 차트 키·날짜 선택 초기화
    m.code = c;
    m.name = name || "";
    if (m.chart) { m.chart.key = ""; m.chart.date = ""; m.chart.dates = []; }
    m.title.innerHTML = `<i class="bi bi-graph-up me-1"></i>${m.name} `
                      + `<span class="text-secondary">${c}</span>`;
  }
  // 열 때마다 지연 로드 플래그를 비운다 — 같은 종목을 다시 열어도 엔진 판단·
  // 신호는 그 사이 변했을 수 있다(기본정보는 서버 5분 캐시라 부담 없음).
  // 모달이 떠 있는 동안의 탭 전환은 재조회하지 않는다.
  m.loaded = {};
  m.modal.show();
  _loadTier(c);                    // 열 때마다 tier 최신값(전환 버튼 포함)
  const tab = opts.tab || "info";
  m.tabs.show(tab);
  _onStockTab(tab);
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
