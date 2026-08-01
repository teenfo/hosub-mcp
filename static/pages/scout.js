import { fetchJSON, el, card, badge } from "../app.js";
import { makeLayoutEditable } from "../layout.js";
import { postJSON, fmt, makeChanged, makeTabs, priceCellHTML, stockHTML } from "./tradelib.js";
import { mdToHtml, renderIframe } from "./briefing.js";
import { createProChart, MA_DEFS } from "../chart.js";

// 발굴 엔진 (트레이딩 그룹) — 여섯 갈래로 흩어진 종목 발굴을 한 통로로 모은다.
//
//   [엔진 상태]     모드 · 소스별 수집 상태 · 임계값
//        ↓
//   [후보 큐]       점수 순. 어느 정보원이 몇 개나 가리켰나
//        ↓
//   [판단 대조]     엔진이라면 이렇게 했을 것  ↔  실제 감시목록   ← shadow 의 핵심
//        ↓
//   [결정 이력]     승격·강등 기록. 소스별 기여도 측정의 입력
//
// 2026-08-01 화면 통합: 옛 '발굴·감시' 페이지가 이 페이지의 상단 섹션으로
// 흡수됐다(완전 통합 — 편입이 엔진 단일 통로가 되면서 두 화면을 가를 이유가
// 사라졌다). 위쪽이 실무(국면·후보·감시목록·리포트), 아래쪽이 엔진의 판단이다.

const MODE_META = {
  shadow: { label: "관찰(shadow)", tone: "secondary",
            desc: "신호를 모으고 결정을 <b>기록만</b> 한다. 감시목록에는 손대지 않는다." },
  collect: { label: "수집전용(collect)", tone: "warning",
             desc: "수집전용 tier 만 엔진이 관리한다. 매매 tier 는 기존 경로 소유." },
  full: { label: "전체(full)", tone: "danger",
          desc: "매매 tier 까지 엔진이 관리한다." },
};
const SRC_META = {
  volume: { label: "거래대금", tone: "info" },
  gainers: { label: "등락률", tone: "danger" },
  presurge: { label: "급등조짐", tone: "warning" },
  nightly: { label: "야간발굴", tone: "primary" },
  news: { label: "뉴스·공시", tone: "success" },
  manual: { label: "수동", tone: "dark" },
};
const GROUP_KO = { intraday: "장중", daily: "일간", news: "뉴스", human: "사람" };
const TIER_KO = { trade: "매매", collect: "수집전용", none: "미편입" };
const TIER_TONE = { trade: "danger", collect: "secondary", none: "light" };
const ACTION_KO = {
  promote_collect: "수집전용 편입", promote_trade: "매매 승격",
  demote: "수집전용 강등", drop: "감시 해제", hold: "유지",
};
const ACTION_TONE = {
  promote_collect: "secondary", promote_trade: "danger",
  demote: "warning", drop: "light", hold: "light",
};
const srcMeta = (s) => SRC_META[s] || { label: s || "—", tone: "secondary" };

/** 소스마다 evidence 키가 다르다 — 있는 것만 짧게 이어 붙인다. */
function evidenceText(ev) {
  if (!ev || typeof ev !== "object") return "";
  const out = [];
  if (ev.rank != null) out.push(`순위 ${ev.rank}/${ev.of}`);
  if (ev.change_pct != null) out.push(`등락 ${ev.change_pct}%`);
  if (ev.surge_pct != null) out.push(`거래량 급증 ${ev.surge_pct}%`);
  if (ev.trade_value != null) out.push(`거래대금 ${ev.trade_value}`);
  if (Array.isArray(ev.reasons) && ev.reasons.length) out.push(ev.reasons.join(" · "));
  if (ev.category) out.push(ev.category);
  if (ev.title) out.push(String(ev.title).slice(0, 40));
  if (ev.date) out.push(ev.date);
  if (ev.added) out.push(`추가 ${String(ev.added).slice(0, 10)}`);
  return out.join(" · ");
}

// ============================================================================
// 발굴·감시 섹션 — 옛 discover.js(별도 페이지)를 2026-08-01 화면 통합으로 흡수.
// 시장 국면 · 후보 발굴(실시간 3 + 야간 2) · 감시목록 관리 · AI 리포트.
// 레이아웃 저장 키("discover")와 동작은 그대로다 — 옮긴 것은 자리뿐이다.
// ============================================================================
// 발굴·감시 페이지 (트레이딩 그룹) — 종목이 '어디서 와서 어디로 가는가' 를 따라간다.
//
//   [후보 발굴]  실시간(거래대금·급등률·조짐) + 야간 배치(scout/nightly)
//        ↓ 발굴 엔진(단일 통로) / 수동 추가
//   [감시목록]  매매 tier / 수집전용 tier
//        ↓
//   (매매 데스크 — 신호·주문은 그쪽 담당)
//
// 성격이 다른 표를 한 카드에 쌓지 않고 탭으로 갈라, 지금 무엇을 보고 있는지가
// 항상 분명하게 한다. 정보는 종전과 동일하게 전부 유지한다.

const WL_SOURCE_META = {
  seed:   { label: "기본",     tone: "secondary", desc: "config 초기 종목" },
  manual: { label: "수동",     tone: "primary",   desc: "직접 추가" },
  auto:   { label: "야간발굴", tone: "warning",   desc: "야간 전종목 분석 상위" },
  gainer: { label: "급등률",   tone: "danger",    desc: "급등률 상위 자동편입" },
  active: { label: "거래대금", tone: "info",      desc: "거래대금 상위 자동편입" },
};
const wlSrcMeta = (s) => WL_SOURCE_META[s] || { label: s || "—", tone: "secondary", desc: "" };
const dayOf = (iso) => String(iso || "").slice(0, 10);

async function renderWatchSection(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);
    const changed = makeChanged();

    const statusC = card("시장 국면 · 수집 현황", null, { wide: true, icon: "bi-speedometer" });
    const sourceC = card("후보 발굴", null, { wide: true, icon: "bi-rocket-takeoff" });
    const watchC = card("감시목록", null, { wide: true, icon: "bi-eye" });
    const reportC = card("AI 분석 리포트", null, { wide: true, icon: "bi-journal-text" });
    // 짧은 요약 둘(국면·리포트 목록)을 첫 줄에 짝지어 세로를 아낀다.
    // 표 중심 카드(후보·감시목록)는 전폭 — 좁히면 열이 부러진다.
    const CARDS = [["status", statusC, 6], ["report", reportC, 6],
                   ["source", sourceC, 12], ["watch", watchC, 12]];
    CARDS.forEach(([id, c, w], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;
      c.col.className = "col-12 col-xl-" + w;
      row.appendChild(c.col);
    });
    makeLayoutEditable(row, { key: "discover" });

    let watch = {};          // 코드 → 이름 (후보 표의 '감시중' 판정)
    let watchRows = [];      // 감시목록 원본 (필터가 다시 그린다)
    const invalidateSources = () => {
      changed.invalidate("source:scan");
      changed.invalidate("source:disc");
    };
    const afterWatchChange = () => {
      invalidateSources();             // '감시중' 표시가 바뀌므로 후보 표도 다시 그린다
      loadWatch(); loadScanner(); loadDiscovery();
    };

    // ==================================================================
    // 공용 — 종목 차트 모달 · 리포트 모달 · '감시 추가' 버튼
    // ==================================================================
    const chartModalTitle = el("h5", { class: "modal-title" });
    const chartModalMsg = el("span", { class: "text-secondary small ms-2" });
    const chartHost = el("div", { style: "width:100%;height:62vh;min-height:360px" });
    const periods = [["1개월", 21], ["3개월", 63], ["6개월", 126], ["1년", 252], ["전체", "all"]];
    const periodGroup = el("div", { class: "btn-group btn-group-sm" });
    const periodBtns = periods.map(([lbl, n]) => {
      const b = el("button", { class: "btn btn-outline-secondary", type: "button" }, lbl);
      b.onclick = () => { proChart.setVisibleCount(n); periodBtns.forEach((x) => x.classList.toggle("active", x === b)); };
      periodGroup.appendChild(b);
      return b;
    });
    const bbBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "볼린저밴드");
    let bbOn = false;
    bbBtn.onclick = () => { bbOn = !bbOn; bbBtn.classList.toggle("active", bbOn); proChart.setIndicator("bb", bbOn); };
    const maLegend = el("div", { class: "small d-flex gap-2 flex-wrap align-items-center ms-auto" },
      MA_DEFS.map((d) => el("span", { style: `color:${d.color};font-weight:600` }, `━ MA${d.p}`)));
    const stockChartModalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-xl modal-dialog-centered" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header py-2" }, [
            el("div", { class: "d-flex align-items-baseline" }, [chartModalTitle, chartModalMsg]),
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          el("div", { class: "modal-body pt-2" }, [
            el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
              [periodGroup, bbBtn,
               el("span", { class: "small text-secondary" }, "휠 확대·드래그 이동·더블클릭 리셋"),
               maLegend]),
            chartHost,
          ]),
        ]),
      ),
    );
    container.appendChild(stockChartModalEl);
    const stockChartModal = new bootstrap.Modal(stockChartModalEl);
    const proChart = createProChart(chartHost, { up: "#d64545", down: "#3a6fd8" });
    stockChartModalEl.addEventListener("shown.bs.modal", () => proChart.redraw());
    const openStockChart = async (code, name) => {
      chartModalTitle.textContent = `${name} (${code}) 일봉`;
      chartModalMsg.textContent = "불러오는 중…";
      periodBtns.forEach((x) => x.classList.remove("active"));
      stockChartModal.show();
      try {
        const bars = await fetchJSON(`/api/trading/bars/${code}?tf=1d`);
        chartModalMsg.textContent = bars.length ? `${bars.length}봉` : "일봉 데이터 없음 (야간 발굴 수집 후 표시)";
        proChart.setData(bars);
      } catch (e) {
        chartModalMsg.textContent = "불러오기 실패: " + e.message;
      }
    };

    /** 종목명 셀 — 누르면 일봉 차트 모달. */
    // 종목명은 기본정보 모달(공통), 차트 아이콘은 이 화면 전용 동작.
    // 클릭 하나에 둘을 묶으면 어느 쪽이 뜰지 예측할 수 없다.
    const nameCell = (code, name) => {
      const chart = el("button", {
        type: "button", title: "일봉 차트 보기",
        class: "btn btn-link p-0 border-0 ms-1 align-baseline text-secondary",
      }, el("i", { class: "bi bi-bar-chart-line" }));
      chart.onclick = () => openStockChart(code, name);
      return el("td", { class: "text-nowrap" },
                [el("span", { html: stockHTML(code, name) }), chart]);
    };

    const reportModalTitle = el("h5", { class: "modal-title" });
    const reportModalBody = el("div", { class: "modal-body" });
    const reportModalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header" }, [
            reportModalTitle,
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          reportModalBody,
        ]),
      ),
    );
    container.appendChild(reportModalEl);
    const reportModal = new bootstrap.Modal(reportModalEl);
    const openReport = async (date) => {
      reportModalTitle.textContent = `${date} 야간 분석 리포트`;
      reportModalBody.innerHTML = "";
      reportModalBody.appendChild(el("div", { class: "text-secondary small" }, "불러오는 중…"));
      reportModal.show();
      let d;
      try {
        d = await fetchJSON("/api/night-report?date=" + encodeURIComponent(date));
      } catch (e) {
        reportModalBody.innerHTML = "";
        reportModalBody.appendChild(el("div", { class: "text-danger small" }, "불러오기 실패: " + e.message));
        return;
      }
      reportModalBody.innerHTML = "";
      if (!d.exists) {
        reportModalBody.appendChild(el("div", { class: "text-secondary" }, "리포트를 찾을 수 없습니다."));
        return;
      }
      if (d.format === "md") {
        const holder = el("div", { class: "briefing-body" });
        holder.innerHTML = mdToHtml(d.content);
        reportModalBody.appendChild(holder);
      } else {
        renderIframe(reportModalBody, d.content);   // HTML 은 iframe 격리
      }
    };

    const watchBtn = (r) => {
      if (watch[r.code]) return badge("감시중", "success");
      const add = el("button", { class: "btn btn-sm btn-outline-primary py-0" }, "감시 추가");
      add.onclick = async () => {
        add.disabled = true;
        try {
          try {
            await postJSON("/api/trading/watchlist", { code: r.code, name: r.name });
          } catch (err) {
            const g = err.data;
            if (!(g && g.gate)) throw err;
            if (!confirm(`${g.name || g.code} 편입 기준 미달\n\n`
                       + `${g.reasons.join("\n")}\n\n그래도 추가할까요?`)) {
              add.disabled = false;
              return;
            }
            await postJSON("/api/trading/watchlist",
                           { code: r.code, name: r.name, force: true });
          }
          add.textContent = "추가됨";
          add.classList.replace("btn-outline-primary", "btn-success");
          afterWatchChange();
        } catch (e) { alert("실패: " + e.message); add.disabled = false; }
      };
      return add;
    };

    const emptyRow = (msg) => el("div", { class: "text-secondary small py-2" }, msg);
    const tableOf = (head, rows) => {
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: `<tr>${head}</tr>` }));
      const tb = el("tbody");
      rows.forEach((r) => tb.appendChild(r));
      t.appendChild(tb);
      return el("div", { class: "table-responsive" }, t);
    };

    // ==================================================================
    // ① 시장 국면 · 수집 현황 — 종전에 야간 발굴 카드 안에 묻혀 있던 정보
    // ==================================================================
    const stRegime = el("div", { class: "d-flex gap-2 align-items-center flex-wrap" });
    const stCounts = el("div", { class: "d-flex gap-2 align-items-center flex-wrap mt-2 small" });
    const stMeta = el("div", { class: "small text-secondary mt-2" });
    const runBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "지금 야간 분석 실행");
    runBtn.onclick = async () => {
      if (!confirm("전종목 일봉 수집·분석을 시작할까요? (약 10~15분, 주문 없음)")) return;
      try { await postJSON("/api/trading/nightly/run"); } catch (e) { alert(e.message); }
      loadDiscovery();
    };
    statusC.body.append(stRegime, stCounts, stMeta,
      el("div", { class: "mt-2" }, runBtn));

    // ==================================================================
    // ② 후보 발굴 — 탭으로 분리 (실시간 3 + 야간 2)
    // ==================================================================
    const TABS = [
      { id: "active",   label: "거래대금 상위", icon: "cash-stack",
        note: "시장이 실제로 돈을 넣는 종목 — 유동성이 안전하다. 자동 편입(active)." },
      { id: "gainer",   label: "급등률 상위", icon: "rocket-takeoff",
        note: "코스피·코스닥 등락률 상위. 매수 가능 가격대는 매매, 그 외는 수집전용으로 자동 편입(gainer)." },
      { id: "presurge", label: "급등 조짐", icon: "lightning",
        note: "거래량이 먼저 터지고 가격은 아직 — 참고용, 자동 편입하지 않는다." },
      { id: "long",     label: "야간 상승 발굴", icon: "graph-up-arrow",
        note: "전일 전종목 일봉 3규칙(거래량·신고가·정배열) 기계 선별. 상위 일부만 자동 편입(auto)." },
      { id: "short",    label: "야간 하락 후보", icon: "graph-down-arrow",
        note: "역배열·저점근접·60이평 하회 — 반등 페이드용 참고. 자동 편입하지 않는다." },
    ];
    const tabNav = el("ul", { class: "nav nav-tabs nav-fill small mb-2" });
    const panes = {};
    const counts = {};
    let activeTab = TABS[0].id;
    const showTab = (id) => {
      activeTab = id;
      TABS.forEach((t) => {
        panes[t.id].classList.toggle("d-none", t.id !== id);
        tabNav.querySelector(`[data-tab="${t.id}"]`).classList.toggle("active", t.id === id);
      });
    };
    TABS.forEach((t) => {
      const cnt = el("span", { class: "badge text-bg-secondary ms-1" }, "0");
      counts[t.id] = cnt;
      const link = el("button", {
        class: "nav-link" + (t.id === activeTab ? " active" : ""),
        type: "button", "data-tab": t.id,
      }, [el("i", { class: `bi bi-${t.icon} me-1` }), t.label, cnt]);
      link.onclick = () => showTab(t.id);
      tabNav.appendChild(el("li", { class: "nav-item" }, link));
      panes[t.id] = el("div", { class: t.id === activeTab ? "" : "d-none" }, [
        el("div", { class: "small text-secondary mb-2" }, t.note),
        el("div", { class: "pane-body" }, emptyRow("불러오는 중…")),
      ]);
    });
    sourceC.body.append(tabNav, ...TABS.map((t) => panes[t.id]));
    const setPane = (id, node, n) => {
      const body = panes[id].querySelector(".pane-body");
      body.innerHTML = "";
      body.appendChild(node);
      counts[id].textContent = String(n);
      counts[id].className = "badge ms-1 " + (n ? "text-bg-primary" : "text-bg-secondary");
    };

    // ==================================================================
    // ③ 감시목록 — 출처 필터 + 표
    // ==================================================================
    const wQuery = el("input", { class: "form-control form-control-sm",
      placeholder: "종목명 또는 코드 (예: 삼성전자 / 005930)" });
    const wAdd = el("button", { class: "btn btn-sm btn-primary" }, "추가");
    const wMsg = el("div", { class: "small mt-1" });
    const wCands = el("div", { class: "d-flex flex-wrap gap-1 mt-1" });
    const wFilters = el("div", { class: "d-flex flex-wrap gap-1 my-2" });
    const wTblWrap = el("div", { class: "table-responsive" });
    watchC.body.append(
      el("div", { class: "d-flex gap-1", style: "max-width:520px" }, [wQuery, wAdd]),
      wMsg, wCands, wFilters, wTblWrap,
    );

    let wFilter = "trade";   // trade | collect | all | <source>
    const FILTERS = [
      ["trade", "매매 대상"], ["collect", "수집전용"], ["all", "전체"],
      ["manual", "수동"], ["active", "거래대금"], ["gainer", "급등률"],
      ["auto", "야간발굴"], ["seed", "기본"],
    ];
    const matchFilter = (e) => {
      if (wFilter === "all") return true;
      if (wFilter === "trade") return !e.collect_only;
      if (wFilter === "collect") return !!e.collect_only;
      return e.source === wFilter;
    };
    const renderWatch = () => {
      wFilters.innerHTML = "";
      for (const [id, label] of FILTERS) {
        const n = watchRows.filter((e) => {
          if (id === "all") return true;
          if (id === "trade") return !e.collect_only;
          if (id === "collect") return !!e.collect_only;
          return e.source === id;
        }).length;
        if (!n && !["trade", "collect", "all"].includes(id)) continue;  // 없는 출처는 숨김
        const chip = el("button", {
          type: "button",
          class: "btn btn-sm py-0 " + (wFilter === id ? "btn-primary" : "btn-outline-secondary"),
        }, `${label} ${n}`);
        chip.onclick = () => { wFilter = id; renderWatch(); };
        wFilters.appendChild(chip);
      }
      const rows = watchRows.filter(matchFilter);
      wTblWrap.innerHTML = "";
      if (!rows.length) {
        wTblWrap.appendChild(emptyRow("해당 조건의 종목 없음"));
        return;
      }
      const tb = rows.map((it) => {
        const m = wlSrcMeta(it.source);
        const btBtn = el("button", { class: "btn btn-sm btn-outline-secondary py-0 me-1",
          title: "성과·백테스트 페이지에서 실행" }, "백테스트");
        btBtn.onclick = () => {
          sessionStorage.setItem("backtest:symbol", it.code);
          location.hash = "#/backtest";
        };
        const modeBtn = el("button", { class: "btn btn-sm btn-outline-primary py-0 me-1",
          title: it.collect_only ? "매매 대상으로 전환" : "수집전용으로 전환(매매 제외)" },
          it.collect_only ? "매매로" : "수집전용");
        modeBtn.onclick = async () => {
          try { await postJSON("/api/trading/watchlist/mode", { code: it.code, collect_only: !it.collect_only }); }
          catch (e) { alert("실패: " + e.message); }
          afterWatchChange();
        };
        const rm = el("button", { class: "btn btn-sm btn-outline-danger py-0" }, "제거");
        rm.onclick = async () => {
          if (!confirm(`${it.name}(${it.code}) 을 감시목록에서 제거할까요?`)) return;
          try { await postJSON("/api/trading/watchlist/remove", { code: it.code }); }
          catch (e) { alert("실패: " + e.message); }
          afterWatchChange();
        };
        return el("tr", {}, [
          nameCell(it.code, it.name),
          el("td", { class: "text-end fw-semibold", "data-px": it.code },
            it.cur_price ? fmt(it.cur_price) : "—"),
          el("td", {}, el("span", { title: m.desc }, badge(m.label, m.tone))),
          el("td", {}, it.collect_only ? badge("수집전용", "secondary") : badge("매매", "success")),
          el("td", { class: "small text-secondary text-nowrap" }, dayOf(it.added)),
          el("td", { class: "text-nowrap" }, [btBtn, modeBtn, rm]),
        ]);
      });
      wTblWrap.appendChild(tableOf(
        "<th>종목</th><th class='text-end'>현재가</th><th>출처</th><th>모드</th>" +
        "<th>편입일</th><th></th>", tb));
    };

    const loadWatch = async () => {
      let w;
      try { w = await fetchJSON("/api/trading/watchlist"); } catch (e) { return; }
      const codesKey = w.entries.map((e) => e.code).sort().join(",");
      watch = Object.fromEntries(w.entries.map((e) => [e.code, e.name]));
      if (changed("watchCodes", codesKey)) {
        invalidateSources();
        loadScanner(); loadDiscovery();
      }
      const wKey = w.entries.map((e) =>
        `${e.code}:${e.name}:${e.source}:${e.collect_only ? 1 : 0}`).join("|");
      watchRows = w.entries;
      if (!changed("watch", wKey)) return;   // 가격만 바뀐 경우는 refreshPrices 가 처리
      renderWatch();
      renderCounts();
    };

    const renderCounts = () => {
      stCounts.innerHTML = "";
      const nTrade = watchRows.filter((e) => !e.collect_only).length;
      const bySrc = {};
      watchRows.forEach((e) => { bySrc[e.source] = (bySrc[e.source] || 0) + 1; });
      stCounts.append(
        el("span", { class: "text-secondary" }, "감시목록"),
        el("span", { class: "badge text-bg-light text-dark" }, `전체 ${watchRows.length}`),
        badge(`매매 ${nTrade}`, "success"),
        badge(`수집전용 ${watchRows.length - nTrade}`, "secondary"),
        el("span", { class: "text-secondary ms-2" }, "출처"),
        ...Object.entries(bySrc).sort((a, b) => b[1] - a[1]).map(([s, n]) => {
          const m = wlSrcMeta(s);
          return el("span", { title: m.desc }, badge(`${m.label} ${n}`, m.tone));
        }),
      );
    };

    // 종목 추가 (코드/종목명, 다중 매칭 시 후보 칩)
    const addByQuery = async (payload, msgOnMulti) => {
      wMsg.textContent = "";
      wMsg.className = "small mt-1";
      wCands.innerHTML = "";
      wAdd.disabled = true;
      try {
        const r = await postJSON("/api/trading/watchlist", payload);
        if (r.candidates) {
          wMsg.className = "small mt-1 text-secondary";
          wMsg.textContent = msgOnMulti || `여러 종목이 검색됨 — 선택하세요 (${r.candidates.length})`;
          for (const c of r.candidates) {
            // 이 칩 자체가 '이 종목을 고른다' 는 동작이다 — 안에 링크를 넣으면
            // 클릭이 기본정보 모달로 가로채여 선택이 안 된다.
            const chip = el("button", { class: "btn btn-sm btn-outline-primary py-0" },
              el("span", { html: stockHTML(c.code, c.name, { plain: true }) }));
            chip.onclick = () => addByQuery({ code: c.code, name: c.name });
            wCands.appendChild(chip);
          }
          return;
        }
        wQuery.value = "";
        afterWatchChange();
      } catch (e) {
        // 편입 게이트(409)는 거절이 아니라 확인 절차다 — 무엇이 모자란지
        // 숫자로 보여주고, 사람이 근거가 있으면 그대로 넣을 수 있어야 한다.
        const g = e.data;
        if (g && g.gate) {
          wMsg.className = "small mt-1";
          wMsg.innerHTML = `<span class="text-warning">편입 기준 미달</span> `
            + `<span class="text-secondary">${g.name || g.code} — ${g.reasons.join(" · ")}</span>`;
          const m = g.metrics || {};
          wCands.innerHTML = "";
          wCands.appendChild(el("div", { class: "text-secondary small w-100", html:
            `현재가 ${fmt(m.price || 0)}원 · 거래대금 ${fmt(m.trde_prica || 0)}백만`
            + ` · 체결강도 ${m.cntr_str ?? "-"} · 시총 ${fmt(m.mac || 0)}억` }));
          const go = el("button", { class: "btn btn-sm btn-outline-warning py-0" },
                        "그래도 추가");
          go.onclick = () => addByQuery({ code: g.code, name: g.name, force: true });
          wCands.appendChild(go);
          return;
        }
        wMsg.className = "small mt-1 text-danger";
        wMsg.textContent = "추가 실패: " + e.message;
      } finally {
        wAdd.disabled = false;
      }
    };
    wAdd.onclick = () => {
      const q = wQuery.value.trim();
      if (!q) { wMsg.className = "small mt-1 text-danger"; wMsg.textContent = "종목명 또는 코드를 입력하세요"; return; }
      addByQuery({ query: q });
    };
    wQuery.onkeydown = (e) => { if (e.key === "Enter") wAdd.onclick(); };

    // ==================================================================
    // ④ AI 분석 리포트
    // ==================================================================
    const rBody = el("div");
    reportC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-robot"></i> AI가 전종목 데이터를 분석해 독립 선별 · 기술적 소견 + 최신 뉴스 포함' })),
      rBody,
    );
    const loadReport = async () => {
      let d;
      try { d = await fetchJSON("/api/night-report"); } catch (e) { return; }
      if (!changed("report", d)) return;
      rBody.innerHTML = "";
      if (!d.exists || !(d.dates && d.dates.length)) {
        rBody.appendChild(emptyRow(
          "아직 분석 리포트가 없습니다 — Cowork 예약 작업이 리포트를 생성하면 목록에 표시됩니다."));
        return;
      }
      const listg = el("div", { class: "list-group list-group-flush" });
      for (const dt of d.dates) {
        const item = el("button", {
          class: "list-group-item list-group-item-action d-flex justify-content-between align-items-center py-2",
        }, [
          el("span", { html: `<i class="bi bi-file-earmark-text me-2"></i>${dt} 분석 리포트` }),
          dt === d.date ? badge("최신", "success") : el("span", {}),
        ]);
        item.onclick = () => openReport(dt);
        listg.appendChild(item);
      }
      rBody.appendChild(listg);
    };

    // ==================================================================
    // 데이터 로드
    // ==================================================================
    let scanMeta = "", discMeta = "";
    const setMeta = () => {
      stMeta.innerHTML = "";
      stMeta.append(el("div", {}, scanMeta || "실시간 스캔 대기"),
                    el("div", {}, discMeta || "야간 발굴 결과 없음"));
    };

    const loadScanner = async () => {
      let sc;
      try { sc = await fetchJSON("/api/trading/scanner"); } catch (e) { return; }
      if (!changed("source:scan", sc)) return;
      const cfg = sc.config || {};
      scanMeta = `실시간 스캔 — 등락률 +${cfg.min_change_pct ?? 3}% 이상 · 거래대금 상위 교차 · ` +
        (sc.last_scan ? `마지막 ${sc.last_scan.slice(11, 19)}` : "장중에만 실행");
      setMeta();

      // 거래대금 상위 (active 자동편입)
      const res = sc.results || [];
      setPane("active", res.length ? tableOf(
        "<th>종목</th><th class='text-end'>현재가</th><th class='text-end'>등락률</th>" +
        "<th class='text-end'>거래대금</th><th></th>",
        res.map((r) => el("tr", {}, [
          nameCell(r.code, r.name),
          el("td", { class: "text-end" }, fmt(r.price)),
          el("td", { class: "text-end text-danger" }, `+${r.change_pct.toFixed(1)}%`),
          el("td", { class: "text-end" }, fmt(r.trade_value)),
          el("td", {}, watchBtn(r)),
        ]))) : emptyRow("조건에 맞는 종목 없음"), res.length);

      // 급등률 상위 (gainer 자동편입)
      const gn = sc.gainers || [];
      setPane("gainer", gn.length ? tableOf(
        "<th>종목</th><th class='text-end'>현재가</th><th class='text-end'>급등률</th>" +
        "<th>편입 tier</th><th></th>",
        gn.map((r) => el("tr", {}, [
          nameCell(r.code, r.name),
          el("td", { class: "text-end" }, fmt(r.price)),
          el("td", { class: "text-end text-danger fw-semibold" }, `+${r.change_pct.toFixed(1)}%`),
          el("td", {}, r.collect_only ? badge("수집전용", "secondary") : badge("매매", "success")),
          el("td", {}, watchBtn(r)),
        ]))) : emptyRow("조건에 맞는 종목 없음"), gn.length);

      // 급등 조짐 (참고)
      const ps = sc.presurge || [];
      setPane("presurge", ps.length ? tableOf(
        "<th>종목</th><th class='text-end'>현재가</th><th class='text-end'>등락률</th>" +
        "<th class='text-end'>거래량 급증률</th><th></th>",
        ps.map((r) => el("tr", {}, [
          nameCell(r.code, r.name),
          el("td", { class: "text-end" }, fmt(r.price)),
          el("td", { class: "text-end" }, `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(1)}%`),
          el("td", { class: "text-end text-warning" }, `+${fmt(r.surge_pct)}%`),
          el("td", {}, watchBtn(r)),
        ]))) : emptyRow("조건에 맞는 종목 없음"), ps.length);
    };

    const loadDiscovery = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/nightly"); } catch (e) { return; }
      if (!changed("source:disc", d)) return;

      // 시장 국면
      const mk = d.market || {};
      stRegime.innerHTML = "";
      if (mk.regime) {
        const tone = mk.regime === "강세" ? "danger" : mk.regime === "약세" ? "primary" : "secondary";
        stRegime.append(
          el("span", { class: "small text-secondary" }, "시장 국면"),
          el("span", { class: "fs-6" }, badge(mk.regime, tone)),
          el("span", { class: "small text-secondary" },
            `60이평 상회 ${mk.breadth_ma60}% · 20이평 ${mk.breadth_ma20}% · ` +
            `중앙 20일수익률 ${mk.median_ret20}% (${mk.analyzed}종목 분석)`),
        );
      } else {
        stRegime.appendChild(el("span", { class: "small text-secondary" },
          "시장 국면 미산출 — 야간 발굴 실행 후 표시됩니다"));
      }
      discMeta = (d.date ? `야간 발굴 기준일 ${d.date} · ${d.progress || ""}` :
                  "야간 발굴 결과 없음 (평일 17:30 자동 실행)") +
        (d.dataset ? ` · 데이터셋 ${d.dataset.symbol_count}종목 → ${d.dataset.features_file}` : "");
      setMeta();
      runBtn.disabled = !!d.running;
      runBtn.textContent = d.running ? "실행 중… " + (d.progress || "") : "지금 야간 분석 실행";

      // 상승(롱) 발굴
      const picks = d.picks || [];
      setPane("long", picks.length ? tableOf(
        "<th>종목</th><th class='text-end'>종가</th><th class='text-end'>점수</th>" +
        "<th>발굴 사유</th><th></th>",
        picks.map((p) => el("tr", {}, [
          nameCell(p.code, p.name),
          el("td", { class: "text-end" }, fmt(p.close)),
          el("td", { class: "text-end" }, String(p.score)),
          el("td", { class: "small text-secondary" }, (p.reasons || []).join(" · ")),
          el("td", {}, watchBtn(p)),
        ]))) : emptyRow("발굴 결과 없음"), picks.length);

      // 하락(숏) 후보
      const bears = mk.bearish_top || [];
      setPane("short", bears.length ? tableOf(
        "<th>종목</th><th class='text-end'>종가</th><th class='text-end'>하락점수</th>" +
        "<th class='text-end'>상대강도</th><th></th>",
        bears.map((p) => el("tr", {}, [
          nameCell(p.code, p.name),
          el("td", { class: "text-end" }, fmt(p.close)),
          el("td", { class: "text-end" }, String(p.bearish_score)),
          el("td", { class: "text-end " + (p.rs_20 < 0 ? "text-primary" : "text-secondary") },
            `${p.rs_20}%p`),
          el("td", {}, watchBtn(p)),
        ]))) : emptyRow(`하락 후보 없음${mk.bearish_count ? ` (전체 ${mk.bearish_count}종목)` : ""}`),
        bears.length);
    };

    // 현재가만 2초 주기로 셀 부분 갱신(표 재렌더 없이)
    const refreshPrices = async () => {
      let m;
      try { m = await fetchJSON("/api/trading/prices"); } catch (e) { return; }
      const prices = (m && m.prices) || {};
      container.querySelectorAll("[data-px]").forEach((cell) => {
        const p = prices[cell.getAttribute("data-px")];
        if (p == null) return;
        priceCellHTML(cell, p, cell.getAttribute("data-entry"));
      });
    };

    await Promise.all([loadWatch(), loadScanner(), loadDiscovery(), loadReport()]);
    ctx.addTimer(setInterval(refreshPrices, 2_000));
    ctx.addTimer(setInterval(loadScanner, 10_000));
    ctx.addTimer(setInterval(() => { loadWatch(); loadDiscovery(); }, 30_000));
    ctx.addTimer(setInterval(loadReport, 300_000));
}

export default {
  id: "scout",
  title: "발굴 엔진",     // 메뉴 이름 — 사용자 확정 2026-08-01
  icon: "bi-binoculars",
  group: "트레이딩",
  aliases: ["discover"],      // 옛 페이지 해시(#/discover) 북마크 호환
  async render(container, ctx) {
    // 페이지를 두 판으로 가른다 — 운영(후보·감시목록)과 엔진 판단.
    // 통합 직후에는 카드 9장이 한 기둥으로 쌓여 세로 스크롤이 너무 길었다.
    // 매일 보는 것(운영)과 가끔 대조하는 것(엔진 판단)은 리듬이 다르다 —
    // 탭으로 갈라 각자 첫 화면에 들어오게 한다. 선택은 세션에 기억된다.
    const opsPane = el("div");
    const engPane = el("div", { class: "d-none" });
    const engBadge = el("span", { class: "badge text-bg-secondary ms-1" }, "·");
    const navDefs = [
      ["ops", "운영 — 후보·감시목록", "bi-eye", opsPane, null],
      ["engine", "엔진 판단", "bi-diagram-3", engPane, engBadge],
    ];
    const nav = el("ul", { class: "nav nav-pills gap-1 mb-3" });
    const showPane = (id) => {
      navDefs.forEach(([tid, , , pane]) => pane.classList.toggle("d-none", tid !== id));
      nav.querySelectorAll("button").forEach((b) =>
        b.classList.toggle("active", b.dataset.tab === id));
      try { sessionStorage.setItem("scout:tab", id); } catch (e) { /* 프라이빗 모드 */ }
    };
    navDefs.forEach(([tid, label, icon, , bdg]) => {
      const b = el("button", { class: "nav-link", type: "button", "data-tab": tid },
        [el("i", { class: `bi ${icon} me-1` }), label, bdg || el("span")]);
      b.onclick = () => showPane(tid);
      nav.appendChild(el("li", { class: "nav-item" }, b));
    });
    container.append(nav, opsPane, engPane);
    showPane(sessionStorage.getItem("scout:tab") === "engine" ? "engine" : "ops");

    const watchSection = renderWatchSection(opsPane, ctx);
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    engPane.appendChild(row);

    const stateC = card("엔진 상태 · 소스", null, { wide: true, icon: "bi-diagram-3" });
    const queueC = card("후보 큐", null, { wide: true, icon: "bi-list-ol" });
    const diffC = card("판단 대조 (엔진 ↔ 실제 감시목록)", null, { wide: true, icon: "bi-arrow-left-right" });
    const srcC = card("소스별 원시 결과 (패키지 단위)", null, { wide: true, icon: "bi-boxes" });
    const histC = card("승격·강등 이력", null, { wide: true, icon: "bi-clock-history" });
    // 후보 큐와 판단 대조를 나란히 — "엔진이 보는 것"과 "실제 반영"을
    // 한 시선에 대조하는 것이 이 판의 존재 이유다.
    const CARDS = [["state", stateC, 12], ["queue", queueC, 6],
                   ["diff", diffC, 6], ["source", srcC, 12], ["hist", histC, 12]];
    CARDS.forEach(([id, c, w], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;
      c.col.className = "col-12 col-xl-" + w;
      row.appendChild(c.col);
    });
    makeLayoutEditable(row, { key: "scout" });

    const emptyRow = (msg) => el("div", { class: "text-secondary small py-2" }, msg);
    const tableOf = (head, rows) => {
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: `<tr>${head}</tr>` }));
      const tb = el("tbody");
      rows.forEach((r) => tb.appendChild(r));
      t.appendChild(tb);
      return el("div", { class: "table-responsive" }, t);
    };
    const srcBadges = (list) => (list || []).map((s) => {
      const m = srcMeta(s);
      return el("span", { class: `badge text-bg-${m.tone} me-1` }, m.label);
    });

    // ==================================================================
    // ① 엔진 상태
    // ==================================================================
    const stateBody = el("div", {}, emptyRow("불러오는 중…"));
    const runBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "지금 수집");
    stateC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-diagram-3"></i> 여섯 소스(거래대금·등락률·급등조짐·야간발굴·뉴스·수동)가 신호를 <b>에스컬레이션</b>하면 엔진이 취합해 단일 통로로 감시목록을 갱신한다. <b>점수는 매매 tier 를 열지 않는다</b> — 점수 순 상위 N 이 매수가능 무작위보다 0.22R 나빴다는 것이 실측됐다. 매매는 유동성·체결가능성·체류시간 게이트로만 연다.' })),
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [runBtn]),
      stateBody,
    );

    const setMode = async (mode) => {
      const m = MODE_META[mode];
      if (!confirm(`모드를 '${m.label}' 로 바꿉니다.\n\n${m.desc.replace(/<[^>]+>/g, "")}\n\n계속할까요?`)) return;
      try { await postJSON("/api/trading/scout/mode", { mode }); changed.invalidate("scout"); await load(); }
      catch (e) { alert("실패: " + e.message); }
    };
    const setFrozen = async (frozen) => {
      try { await postJSON("/api/trading/scout/mode", { frozen }); changed.invalidate("scout"); await load(); }
      catch (e) { alert("실패: " + e.message); }
    };

    const renderState = (st) => {
      stateBody.innerHTML = "";
      const m = MODE_META[st.mode] || MODE_META.shadow;
      const head = el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [
        el("span", { class: "small text-secondary" }, "모드"),
        el("span", { class: `badge text-bg-${m.tone}` }, m.label),
        st.frozen ? badge("동결됨 — 쓰기 중지", "danger") : null,
        st.ready ? badge("수집 준비 완료", "success")
                 : badge("소스 첫 수집 대기", "warning"),
        el("span", { class: "small text-secondary ms-2" },
           st.last_cycle ? `최근 사이클 ${String(st.last_cycle).replace("T", " ").slice(0, 19)}` : "아직 실행 전"),
      ]);
      stateBody.appendChild(head);
      stateBody.appendChild(el("div", { class: "small text-secondary mb-2", html: m.desc }));
      if (st.last_error) {
        stateBody.appendChild(el("div", { class: "text-danger small mb-2" }, "오류: " + st.last_error));
      }
      // 모드 전환
      const btns = el("div", { class: "d-flex gap-2 flex-wrap mb-3" },
        ["shadow", "collect", "full"].map((k) => {
          const mm = MODE_META[k];
          const b = el("button", {
            class: `btn btn-sm ${k === st.mode ? "btn-" + mm.tone : "btn-outline-" + mm.tone}`,
            type: "button",
          }, mm.label);
          b.disabled = k === st.mode;
          b.onclick = () => setMode(k);
          return b;
        }).concat([(() => {
          const b = el("button", {
            class: "btn btn-sm " + (st.frozen ? "btn-success" : "btn-outline-danger"),
            type: "button",
          }, st.frozen ? "동결 해제" : "동결 (쓰기 중지)");
          b.onclick = () => setFrozen(!st.frozen);
          return b;
        })()]));
      stateBody.appendChild(btns);
      // 소스별 상태
      stateBody.appendChild(tableOf(
        "<th>소스</th><th>상태</th><th class='text-end'>주기</th>" +
        "<th class='text-end'>최근 신호</th><th>최근 성공</th><th>연속 실패</th>",
        (st.sources || []).map((s) => {
          const meta = srcMeta(s.name);
          return el("tr", { class: s.enabled ? "" : "opacity-50" }, [
            el("td", {}, el("span", { class: `badge text-bg-${meta.tone}` }, meta.label)),
            // 조회 성공만 보면 "정상적으로 아무것도 못 찾는" 상태를 놓친다 —
            // presurge 가 사흘간 실패 0 · 신호 0 인 채 정상으로 보였다.
            el("td", {}, !s.enabled ? badge("꺼짐", "secondary")
              : s.empty ? el("span", { class: "badge text-bg-warning",
                  title: "조회는 성공하는데 통과 종목이 없습니다. 필터 문턱이나 응답 단위를 확인하세요." },
                  `0건 ${fmt(s.empty)}회`)
              : s.polled ? badge("정상", "success") : badge("대기", "warning")),
            el("td", { class: "text-end text-secondary" }, `${s.interval_sec}초`),
            el("td", { class: "text-end" }, s.signals == null ? "—" : fmt(s.signals)),
            el("td", { class: "text-secondary" },
               s.last_ok ? String(s.last_ok).replace("T", " ").slice(5, 19) : "—"),
            el("td", {}, s.fails
              ? el("span", { class: "text-danger", title: s.error_msg || "" }, `${s.fails}회`)
              : el("span", { class: "text-secondary" }, "—")),
          ]);
        })));
      // 임계값 — 매매 승격에 점수 임계가 없다는 것이 드러나야 한다
      const th = st.thresholds || {};
      stateBody.appendChild(el("div", { class: "small text-secondary mt-2" }, [
        el("span", { class: "me-3" }, `수집 편입 ≥ ${th.promote_collect}`),
        el("span", { class: "me-3" }, `강등 < ${th.demote_below}`),
        el("span", { class: "me-3" }, `최소 체류 ${th.min_dwell_min}분`),
        el("span", { class: "me-3" }, `매매 상한 ${th.max_trade}`),
        el("span", { class: "me-3" }, `전체 상한 ${th.max_total}`),
        el("span", { class: "badge text-bg-light text-dark",
                     title: "점수 순 상위 N 이 매수가능 무작위보다 0.22R 나빴다(실측)" },
           "매매 승격 = 게이트만 (점수 임계 없음)"),
      ]));
    };

    // ==================================================================
    // ② 후보 큐
    // ==================================================================
    const queueBody = el("div", {}, emptyRow("불러오는 중…"));
    queueC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-list-ol"></i> 점수는 <b>그룹 내 max, 그룹 간 합</b>이다. 거래대금·등락률·급등조짐은 같은 팩터의 세 가지 뷰라 더하면 베타 노출이 3배로 증폭된다 — 그룹 안에서는 가장 센 것 하나만 센다. <b>서로 다른 정보원이 같은 종목을 가리킬 때만 점수가 오른다.</b>' })),
      queueBody,
    );

    const renderQueue = (cands, maxScore) => {
      queueBody.innerHTML = "";
      if (!cands.length) {
        queueBody.appendChild(emptyRow("후보 없음 — 소스가 아직 신호를 내지 않았습니다"));
        return;
      }
      queueBody.appendChild(tableOf(
        "<th style='width:2.5rem'>#</th><th>종목</th><th class='text-end'>점수</th>" +
        "<th>정보원 기여</th><th class='text-end'>합의</th>" +
        "<th class='text-end'>현재가</th><th>현재 tier</th>",
        cands.map((c, i) => {
          const width = Math.max(2, Math.round((c.score / (maxScore || 4)) * 100));
          const bar = el("div", { class: "progress", style: "height:.7rem;min-width:120px" },
            Object.entries(c.by_group || {}).map(([g, v]) => el("div", {
              class: "progress-bar " + (g === "news" ? "bg-success" : g === "daily" ? "bg-primary"
                     : g === "human" ? "bg-dark" : "bg-info"),
              style: `width:${Math.round((v / (maxScore || 4)) * 100)}%`,
              title: `${GROUP_KO[g] || g} ${v.toFixed(3)}`,
            })));
          return el("tr", {}, [
            el("td", { class: "text-secondary" }, String(i + 1)),
            el("td", {}, [
              el("div", { html: stockHTML(c.code, c.name) }),
              el("div", { class: "mt-1" }, srcBadges(c.sources)),
            ]),
            el("td", { class: "text-end fw-semibold" }, c.score.toFixed(3)),
            el("td", { style: `min-width:${width}px` }, bar),
            el("td", { class: "text-end" },
               el("span", { class: "badge " + (c.group_count >= 2 ? "text-bg-success" : "text-bg-light text-dark"),
                            title: "독립 정보원 개수 — 점수보다 이쪽이 해석 가능하다" },
                  `${c.group_count}종`)),
            el("td", { class: "text-end" }, c.price ? fmt(c.price) : "—"),
            el("td", {}, [
              badge(TIER_KO[c.tier] || c.tier, TIER_TONE[c.tier] || "light"),
              c.protected ? el("span", { class: "badge text-bg-light text-dark ms-1",
                                         title: "보유 중이거나 사용자가 직접 지정 — 강등하지 않는다" }, "보호") : null,
            ]),
          ]);
        })));
    };

    // ==================================================================
    // ③ 판단 대조 — shadow 의 핵심
    // ==================================================================
    const diffTabs = makeTabs([
      { id: "pending", label: "엔진의 판단", icon: "cpu",
        note: "지금 이 순간 엔진이 하려는 것. <b>관찰 모드에서는 실행되지 않는다</b> — 기록만 남는다." },
      { id: "actual", label: "실제 감시목록", icon: "eye",
        note: "지금 실제로 감시 중인 종목. 관찰 모드에서는 기존 경로(야간발굴·거래대금·등락률 자동편입)가 주인이다." },
    ]);
    diffTabs.mount(diffC.body);
    diffTabs.set("pending", emptyRow("불러오는 중…"));
    diffTabs.set("actual", emptyRow("불러오는 중…"));

    const decisionRows = (rows, withMode) => tableOf(
      (withMode ? "<th>시각</th>" : "") +
      "<th>종목</th><th>판단</th><th>변화</th><th class='text-end'>점수</th>" +
      "<th class='text-end'>등락률</th><th class='text-end'>체결강도</th>" +
      "<th>정보원</th><th>사유</th>" + (withMode ? "<th>적용</th>" : ""),
      rows.map((d) => el("tr", {}, [
        withMode ? el("td", { class: "text-secondary text-nowrap" },
                      String(d.ts || "").replace("T", " ").slice(5, 19)) : null,
        el("td", {}, `${d.name || d.code} (${d.code})`),
        el("td", {}, badge(ACTION_KO[d.action] || d.action,
                           ACTION_TONE[d.action] || "secondary")),
        el("td", { class: "text-secondary text-nowrap" },
           `${TIER_KO[d.from_tier] || d.from_tier || "—"} → ${TIER_KO[d.to_tier] || d.to_tier || "—"}`),
        el("td", { class: "text-end" }, d.score == null ? "—" : Number(d.score).toFixed(3)),
        // 등락률·체결강도는 **관측만** 한다 — 판단에 쓰지 않는다(promote._dec 주석).
        // 회색으로 두는 것이 그 표시다. 4주 뒤 측정에서 값이 있다고 나오면
        // 그때 게이트로 올리고 색을 준다.
        el("td", { class: "text-end text-secondary" },
           d.change_pct == null ? "—" : Number(d.change_pct).toFixed(2) + "%"),
        el("td", { class: "text-end text-secondary",
                   title: "매수/매도 체결량 비율 · 100 = 균형. 아직 판단에 쓰지 않는다" },
           d.cntr_str == null ? "—" : Number(d.cntr_str).toFixed(1)),
        el("td", {}, srcBadges(d.sources)),
        el("td", { class: "text-secondary small" }, d.reason || ""),
        withMode ? el("td", {}, d.applied
          ? badge("반영됨", "success")
          : el("span", { class: "badge text-bg-light text-dark",
                         title: "관찰 모드였거나 동결 중이라 실행되지 않았다" }, "기록만")) : null,
      ].filter(Boolean))));

    // ==================================================================
    // ④ 소스별 원시 결과 — 각 패키지가 무엇을 올렸는가
    // 후보 큐는 그룹 내 max 로 합쳐진 뒤라 어느 소스가 무엇을 봤는지 안 보인다.
    // 여기서는 취합 **전** 을 패키지 단위로 나눠 본다.
    // ==================================================================
    const SRC_TABS = [
      { id: "volume", label: "거래대금", icon: "cash-stack",
        note: "거래대금 상위(ka10032). <b>시장이 실제로 돈을 넣는</b> 종목이라 유동성이 안전하다. 세기 = 목록 안의 순위." },
      { id: "gainers", label: "등락률", icon: "rocket-takeoff",
        note: "등락률 상위(ka10027), 코스피·코스닥 합산. 세기 = 목록 안의 순위. <b>전 시장 조회가 실패하면 빈 목록이 아니라 예외</b>를 낸다 — 실패를 '급등주 없음'으로 보고하면 목록이 증발한다." },
      { id: "presurge", label: "급등조짐", icon: "lightning",
        note: "거래량은 터졌는데 가격은 아직(ka10023). 세기 = 급증률 자체(로그 스케일) — 목록 길이에 안 흔들린다. <b>편입 경로가 없어 지금껏 한 번도 측정된 적이 없는 소스</b>다." },
      { id: "nightly", label: "야간발굴", icon: "graph-up-arrow",
        note: "평일 17:30 전종목 일봉 3규칙. <b>발굴을 다시 돌리지 않고 결과만 읽는다</b>(3,900종목 배치는 엔진 주기로 못 돈다). 세기 = 점수/3 — 예측력은 없고 합의를 세는 용도다." },
      { id: "news", label: "뉴스·공시", icon: "newspaper",
        note: "TNM 분석 중 고점수분. 악재(impact_direction=negative)는 후보에서 뺀다 — 그 필터의 근거는 성과 페이지의 뉴스 영향 검증에 있다." },
      { id: "manual", label: "수동", icon: "hand-index",
        note: "감시목록의 seed/manual 항목. <b>만료도 감쇠도 없다</b> — 엔진이 사용자의 결정을 덮어쓰지 않는다." },
    ];
    const srcTabs = makeTabs(SRC_TABS);
    srcC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-boxes"></i> 각 소스는 <b>독립 패키지</b>다 — 자기 주기로 돌고, 실패해도 다른 소스를 막지 않으며, config 로 개별 on/off 된다. 여기서는 취합 <b>전</b> 원시 결과를 패키지별로 본다. 세기(0~1)는 예측력이 아니라 <b>그 소스 안에서 얼마나 강하게 지목됐는가</b>다.' })),
      el("div", {}),
    );
    srcTabs.mount(srcC.body);
    SRC_TABS.forEach((t) => srcTabs.set(t.id, emptyRow("불러오는 중…")));

    /** 패키지 운영 상태 줄 — 켜짐/주기/최근/실패. 표보다 이게 먼저다. */
    const srcHead = (h) => {
      if (!h) return el("div", { class: "small text-secondary mb-2" }, "상태 없음");
      const items = [
        h.enabled ? badge("켜짐", "success") : badge("꺼짐", "secondary"),
        h.polled ? badge("수집 완료", "success") : badge("첫 수집 대기", "warning"),
        el("span", { class: "badge text-bg-light text-dark" }, `주기 ${h.interval_sec}초`),
        el("span", { class: "badge text-bg-light text-dark" },
           `최근 신호 ${h.signals == null ? "—" : fmt(h.signals)}`),
        h.last_ok ? el("span", { class: "small text-secondary" },
                       `최근 성공 ${String(h.last_ok).replace("T", " ").slice(5, 19)}`) : null,
        h.fails ? el("span", { class: "badge text-bg-danger", title: h.error_msg || "" },
                     `연속 실패 ${h.fails}회 — 백오프 중`) : null,
        h.empty ? el("span", { class: "badge text-bg-warning",
                     title: "조회 성공은 소스가 살아 있다는 증거가 아니다. 필터 문턱·응답 단위를 확인할 것." },
                     `연속 0건 ${fmt(h.empty)}회`) : null,
      ].filter(Boolean);
      const box = el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, items);
      if (h.fails && h.error_msg) {
        return el("div", {}, [box,
          el("div", { class: "small text-danger mb-2", style: "word-break:break-all" },
             h.error_msg)]);
      }
      return box;
    };

    const renderSources = (d) => {
      const health = Object.fromEntries(
        ((d.status || {}).sources || []).map((s) => [s.name, s]));
      const bySrc = d.by_source || {};
      for (const t of SRC_TABS) {
        const rows = bySrc[t.id] || [];
        const h = health[t.id];
        const body = el("div", {}, [
          srcHead(h),
          rows.length ? tableOf(
            "<th style='width:2.5rem'>#</th><th>종목</th><th class='text-end'>세기</th>" +
            "<th class='text-end'>감쇠 후</th><th class='text-end'>원시값</th>" +
            "<th>근거</th><th class='text-end'>관측</th><th>tier</th>",
            rows.slice(0, 40).map((r, i) => el("tr", {}, [
              el("td", { class: "text-secondary" }, String(i + 1)),
              el("td", {}, [
                el("div", { html: stockHTML(r.code, r.name) }),
                r.kind && r.kind !== t.id
                  ? el("div", { class: "text-secondary", style: "font-size:.72rem" }, r.kind)
                  : null,
              ]),
              el("td", { class: "text-end" }, r.strength.toFixed(3)),
              el("td", { class: "text-end " + (r.effective < r.strength ? "text-secondary" : ""),
                         title: "시간 감쇠 반영 — 취합에 실제로 들어가는 값" },
                 r.effective.toFixed(3)),
              el("td", { class: "text-end" }, r.raw == null ? "—" : String(r.raw)),
              el("td", { class: "text-secondary small", style: "max-width:22rem" },
                 evidenceText(r.evidence)),
              el("td", { class: "text-end text-secondary text-nowrap" },
                 r.age_sec < 90 ? "방금" : `${Math.floor(r.age_sec / 60)}분 전`),
              el("td", {}, badge(TIER_KO[r.tier] || r.tier, TIER_TONE[r.tier] || "light")),
            ]))) : emptyRow(h && !h.enabled
              ? "이 소스는 꺼져 있습니다 (config 에서 켤 수 있습니다)"
              : h && h.fails ? "수집에 실패해 결과가 없습니다 — 위 오류 참조"
              : "이번 주기에 올린 종목이 없습니다"),
          rows.length > 40
            ? el("div", { class: "small text-secondary mt-1" },
                 `상위 40건만 표시 — 전체 ${rows.length}건`) : null,
        ]);
        srcTabs.set(t.id, body, rows.length);
      }
    };

    // ==================================================================
    // ⑤ 결정 이력
    // ==================================================================
    const histBody = el("div", {}, emptyRow("불러오는 중…"));
    histC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-clock-history"></i> 모든 승격·강등이 <b>기여 소스와 함께</b> 남는다. 이것이 소스별 기여도 측정의 입력이다 — <b>장중 소스(거래대금·등락률)는 지금까지 한 번도 측정된 적이 없다.</b> 무엇을 골랐는지 남은 기록이 없었기 때문이다.' })),
      histBody,
    );

    // ==================================================================
    const load = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/scout"); } catch (e) { return; }
      if (!changed("scout", d)) return;
      const st = d.status || {};
      renderState(st);
      renderQueue(d.candidates || [], st.max_score);
      renderSources(d);

      const pending = d.pending || [];
      engBadge.textContent = String(pending.length);
      engBadge.className = "badge ms-1 " + (pending.length ? "text-bg-warning" : "text-bg-secondary");
      diffTabs.set("pending", pending.length ? decisionRows(pending, false)
        : emptyRow("지금 바꿀 것이 없습니다 — 후보와 감시목록이 일치합니다"), pending.length);
      const wl = d.watchlist || [];
      diffTabs.set("actual", wl.length ? tableOf(
        "<th>종목</th><th>tier</th><th>보호</th><th></th>",
        wl.map((w) => el("tr", {}, [
          el("td", { class: "text-nowrap", html: stockHTML(w.code, w.name) }),
          el("td", {}, badge(TIER_KO[w.tier] || w.tier, TIER_TONE[w.tier] || "light")),
          el("td", {}, w.protected
            ? el("span", { class: "badge text-bg-light text-dark",
                           title: "보유 중이거나 사용자가 직접 지정" }, "강등 제외")
            : el("span", { class: "text-secondary" }, "—")),
          el("td", { class: "text-nowrap" }, wlControls(w)),
        ]))) : emptyRow("감시목록이 비어 있습니다"), wl.length);

      const hist = d.decisions || [];
      histBody.innerHTML = "";
      histBody.appendChild(hist.length ? decisionRows(hist, true)
        : emptyRow("아직 결정 이력이 없습니다 — 소스가 첫 수집을 마치면 쌓이기 시작합니다"));
    };

    /** 감시목록 조작 — tier 전환·제거. 발굴·감시 페이지에 있던 것을 여기로 옮겼다.
     *  엔진 판단을 보는 자리와 실제로 손대는 자리가 갈라져 있으면, 대조해 놓고
     *  다른 페이지로 가서 고쳐야 한다. 판단과 조작은 한 화면에 있어야 한다. */
    const wlControls = (w) => {
      const mode = el("button", { class: "btn btn-sm btn-outline-primary py-0 me-1",
        type: "button",
        title: w.tier === "collect" ? "매매 대상으로 전환" : "수집전용으로 전환(매매 제외)" },
        w.tier === "collect" ? "매매로" : "수집전용");
      mode.onclick = async () => {
        mode.disabled = true;
        try {
          await postJSON("/api/trading/watchlist/mode",
                         { code: w.code, collect_only: w.tier !== "collect" });
          changed.invalidate("scout"); await load();
        } catch (e) { alert("실패: " + e.message); mode.disabled = false; }
      };
      const rm = el("button", { class: "btn btn-sm btn-outline-danger py-0",
        type: "button" }, "제거");
      rm.onclick = async () => {
        if (!confirm(`${w.name}(${w.code}) 을 감시목록에서 제거할까요?`)) return;
        rm.disabled = true;
        try {
          await postJSON("/api/trading/watchlist/remove", { code: w.code });
          changed.invalidate("scout"); await load();
        } catch (e) { alert("실패: " + e.message); rm.disabled = false; }
      };
      // 보호 종목은 강등에서 제외되지만 사람이 직접 빼는 것은 막지 않는다 —
      // 다만 보유 중일 수 있으므로 확인 문구가 그 역할을 한다.
      return [mode, rm];
    };

    runBtn.onclick = async () => {
      runBtn.disabled = true;
      try { await postJSON("/api/trading/scout/run"); changed.invalidate("scout"); await load(); }
      catch (e) { alert("실패: " + e.message); }
      finally { runBtn.disabled = false; }
    };

    await load();
    ctx.addTimer(setInterval(load, 20_000));
    await watchSection;
  },
};
