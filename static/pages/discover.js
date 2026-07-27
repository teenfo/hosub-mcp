import { fetchJSON, el, card, badge } from "../app.js";
import { mdToHtml, renderIframe } from "./briefing.js";
import { makeLayoutEditable } from "../layout.js";
import { createProChart, MA_DEFS } from "../chart.js";
import { postJSON, fmt, makeChanged, priceCellHTML } from "./tradelib.js";

// 발굴·감시 페이지 (트레이딩 그룹) — 종목이 '어디서 와서 어디로 가는가' 를 따라간다.
//
//   [후보 발굴]  실시간(거래대금·급등률·조짐) + 야간(상승·하락)
//        ↓ 자동편입 / 수동 추가
//   [감시목록]  매매 tier / 수집전용 tier
//        ↓
//   (매매 데스크 — 신호·주문은 그쪽 담당)
//
// 성격이 다른 표를 한 카드에 쌓지 않고 탭으로 갈라, 지금 무엇을 보고 있는지가
// 항상 분명하게 한다. 정보는 종전과 동일하게 전부 유지한다.

const SOURCE_META = {
  seed:   { label: "기본",     tone: "secondary", desc: "config 초기 종목" },
  manual: { label: "수동",     tone: "primary",   desc: "직접 추가" },
  auto:   { label: "야간발굴", tone: "warning",   desc: "야간 전종목 분석 상위" },
  gainer: { label: "급등률",   tone: "danger",    desc: "급등률 상위 자동편입" },
  active: { label: "거래대금", tone: "info",      desc: "거래대금 상위 자동편입" },
};
const srcMeta = (s) => SOURCE_META[s] || { label: s || "—", tone: "secondary", desc: "" };
const dayOf = (iso) => String(iso || "").slice(0, 10);

export default {
  id: "discover",
  title: "발굴·감시",
  icon: "bi-binoculars",
  group: "트레이딩",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);
    const changed = makeChanged();

    const statusC = card("시장 국면 · 수집 현황", null, { wide: true, icon: "bi-speedometer" });
    const sourceC = card("후보 발굴", null, { wide: true, icon: "bi-rocket-takeoff" });
    const watchC = card("감시목록", null, { wide: true, icon: "bi-eye" });
    const reportC = card("AI 분석 리포트", null, { wide: true, icon: "bi-journal-text" });
    const CARDS = [["status", statusC, 12], ["source", sourceC, 12],
                   ["watch", watchC, 12], ["report", reportC, 12]];
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
    const nameCell = (code, name) => {
      const b = el("button", {
        type: "button", title: "일봉 차트 보기",
        class: "btn btn-link p-0 border-0 align-baseline text-start link-body-emphasis",
        style: "text-decoration: underline dotted; text-underline-offset: 2px",
      }, `${name} (${code})`);
      b.onclick = () => openStockChart(code, name);
      return el("td", {}, b);
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
          await postJSON("/api/trading/watchlist", { code: r.code, name: r.name });
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
      try { await postJSON("/api/trading/discovery/run"); } catch (e) { alert(e.message); }
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
        const m = srcMeta(it.source);
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
          const m = srcMeta(s);
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
            const chip = el("button", { class: "btn btn-sm btn-outline-primary py-0" },
              `${c.name} (${c.code})`);
            chip.onclick = () => addByQuery({ code: c.code, name: c.name });
            wCands.appendChild(chip);
          }
          return;
        }
        wQuery.value = "";
        afterWatchChange();
      } catch (e) {
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
      try { d = await fetchJSON("/api/trading/discovery"); } catch (e) { return; }
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
  },
};
