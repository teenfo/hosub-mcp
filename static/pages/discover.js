import { fetchJSON, el, card, badge } from "../app.js";
import { mdToHtml, renderIframe } from "./briefing.js";
import { makeLayoutEditable } from "../layout.js";
import { createProChart, MA_DEFS } from "../chart.js";
import { postJSON, fmt, makeChanged, priceCellHTML } from "./tradelib.js";

// 발굴·감시 페이지 (트레이딩 그룹): 급등 스캐너 · 야간 발굴 · 감시목록 관리 · 분석 보고.
// 종목 소싱 작업 공간 — 실제 매매(승인·신호)는 '매매 데스크' 페이지가 담당한다.

export default {
  id: "discover",
  title: "발굴·감시",
  icon: "bi-binoculars",
  group: "트레이딩",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);
    const changed = makeChanged();

    const watchC = card("감시목록 관리", null, { icon: "bi-eye" });
    const reportC = card("분석 보고 리스트", null, { icon: "bi-journal-text" });
    const scannerC = card("급등 스캐너", null, { wide: true, icon: "bi-rocket-takeoff" });
    const discoveryC = card("야간 발굴 (전일 전종목 분석)", null, { wide: true, icon: "bi-moon-stars" });
    const CARDS = [
      ["watch", watchC, 6], ["report", reportC, 6],
      ["scanner", scannerC, 12], ["discovery", discoveryC, 12],
    ];
    CARDS.forEach(([id, c, w], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;
      c.col.className = "col-12 col-xl-" + w;
      c.col.querySelector(".card").classList.add("h-100");
      row.appendChild(c.col);
    });
    makeLayoutEditable(row, { key: "discover" });

    // 감시목록 맵(코드→이름) — 스캐너/발굴의 '감시중' 표시용. loadWatch 가 채운다.
    let watch = {};
    const afterWatchChange = () => {
      changed.invalidate("scanner");
      changed.invalidate("discovery");
      loadWatch(); loadScanner(); loadDiscovery();
    };

    // --- 감시목록 관리: 종목명/코드로 추가 + 영속 목록 + 제거 ---
    const SOURCE_BADGE = { seed: ["기본", "secondary"], manual: ["수동", "primary"], auto: ["발굴", "warning"], gainer: ["급등", "danger"] };
    const wQuery = el("input", { class: "form-control form-control-sm",
      placeholder: "종목명 또는 코드 (예: 삼성전자 / 005930)" });
    const wAdd = el("button", { class: "btn btn-sm btn-primary" }, "추가");
    const wMsg = el("div", { class: "small mt-1" });
    const wCands = el("div", { class: "d-flex flex-wrap gap-1 mt-1" });  // 후보 선택 칩
    const wTblWrap = el("div", { class: "table-responsive mt-2" });
    watchC.body.append(
      el("div", { class: "d-flex gap-1" }, [wQuery, wAdd]), wMsg, wCands, wTblWrap,
    );
    const loadWatch = async () => {
      let w;
      try { w = await fetchJSON("/api/trading/watchlist"); } catch (e) { return; }
      watch = Object.fromEntries(w.entries.map((e) => [e.code, e.name]));
      // 가격은 refreshPrices 가 셀만 갱신하므로, 표 재렌더는 '구조 변경'일 때만.
      const wKey = w.entries.map((e) => `${e.code}:${e.name}:${e.source}:${e.collect_only ? 1 : 0}`).join("|");
      if (!changed("watch", wKey)) return;
      wTblWrap.innerHTML = "";
      const nTrade = w.entries.filter((e) => !e.collect_only).length;
      const nCollect = w.entries.length - nTrade;
      wTblWrap.appendChild(el("div", { class: "small text-secondary mb-2" },
        `매매 ${nTrade} · 수집전용 ${nCollect} — 수집전용은 데이터만 모으고 신호·주문은 만들지 않음`));
      const tbl = el("table", { class: "table table-sm align-middle mb-0 small" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>출처</th><th>모드</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const it of w.entries) {
        const [label, tone] = SOURCE_BADGE[it.source] || [it.source, "secondary"];
        const modeBadge = it.collect_only ? badge("수집전용", "secondary") : badge("매매", "success");
        const btBtn = el("button", { class: "btn btn-sm btn-outline-secondary py-0 me-1", title: "성과·백테스트 페이지에서 실행" }, "백테스트");
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
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${it.name} (${it.code})`),
          el("td", { class: "text-end fw-semibold", "data-px": it.code }, it.cur_price ? fmt(it.cur_price) : "—"),
          el("td", {}, badge(label, tone)),
          el("td", {}, modeBadge),
          el("td", { class: "text-nowrap" }, [btBtn, modeBtn, rm]),
        ]));
      }
      tbl.appendChild(tb);
      wTblWrap.appendChild(tbl);
    };
    const addByQuery = async (payload, msgOnMulti) => {
      wMsg.textContent = "";
      wMsg.className = "small mt-1";
      wCands.innerHTML = "";
      wAdd.disabled = true;
      try {
        const r = await postJSON("/api/trading/watchlist", payload);
        if (r.candidates) {
          // 여러 종목 매칭 → 선택 칩 표시
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

    // --- 분석 보고 리스트 + 공용 모달 ---
    const rBody = el("div");
    reportC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-robot"></i> AI가 전종목 데이터를 분석해 독립 선별 · 기술적 소견 + 최신 뉴스 포함' })),
      rBody,
    );
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

    // 종목 일봉 차트 모달 (발굴 종목명 클릭 시) — 상용 HTS 급 인터랙티브 차트
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
    const chartToolbar = el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
      [periodGroup, bbBtn, el("span", { class: "small text-secondary" }, "휠 확대·드래그 이동·더블클릭 리셋"), maLegend]);
    const stockChartModalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-xl modal-dialog-centered" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header py-2" }, [
            el("div", { class: "d-flex align-items-baseline" }, [chartModalTitle, chartModalMsg]),
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          el("div", { class: "modal-body pt-2" }, [chartToolbar, chartHost]),
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
        renderIframe(reportModalBody, d.content);  // HTML 은 iframe 격리
      }
    };

    // 야간 발굴 카드 헤더에 최신 리포트 링크 (loadReport 가 갱신)
    const discHeader = discoveryC.body.closest(".card").querySelector(".card-header");
    discHeader.classList.add("d-flex", "justify-content-between", "align-items-center");
    const discReportLink = el("a", { href: "#", class: "small text-decoration-none d-none" });
    discHeader.appendChild(discReportLink);
    const setDiscReportLink = (date) => {
      if (!date) { discReportLink.classList.add("d-none"); return; }
      discReportLink.innerHTML = `<i class="bi bi-journal-text"></i> ${date} 리포트`;
      discReportLink.classList.remove("d-none");
      discReportLink.onclick = (e) => { e.preventDefault(); openReport(date); };
    };

    const loadReport = async () => {
      let d;
      try { d = await fetchJSON("/api/night-report"); } catch (e) { return; }
      if (!changed("report", d)) return;
      rBody.innerHTML = "";
      if (!d.exists || !(d.dates && d.dates.length)) {
        setDiscReportLink(null);
        rBody.appendChild(el("div", { class: "text-secondary small py-3 text-center" },
          "아직 분석 리포트가 없습니다 — Cowork 예약 작업이 리포트를 생성하면 목록에 표시됩니다."));
        return;
      }
      setDiscReportLink(d.date);
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

    // '감시 추가' 공용 버튼 (스캐너·발굴)
    const watchBtn = (r) => {
      if (watch[r.code]) return badge("감시중", "success");
      const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
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

    const loadScanner = async () => {
      let sc;
      try {
        sc = await fetchJSON("/api/trading/scanner");
      } catch (e) { return; }
      if (!changed("scanner", sc)) return;
      scannerC.body.innerHTML = "";
      const cfg = sc.config || {};
      scannerC.body.appendChild(el("div", { class: "text-secondary small mb-2" },
        `등락률 +${cfg.min_change_pct ?? 3}% ↑ · 거래대금 상위 교차 · ` +
        (sc.last_scan ? `마지막 스캔 ${sc.last_scan.slice(11, 19)}` : "장중에만 스캔")));
      // --- KOSPI 급등률 상위 (자동 감시편입) ---
      if ((sc.gainers || []).length) {
        scannerC.body.appendChild(el("div", { class: "small fw-bold text-danger" }, "🚀 KOSPI 급등률 상위 — 저가주는 매매, 고가주는 수집전용으로 자동 편입"));
        const gtbl = el("table", { class: "table table-sm align-middle mb-2" });
        gtbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>급등률</th><th>편입</th><th></th></tr>" }));
        const gtb = el("tbody");
        for (const r of sc.gainers) {
          gtb.appendChild(el("tr", {}, [
            el("td", {}, `${r.name} (${r.code})`),
            el("td", {}, fmt(r.price)),
            el("td", { class: "text-danger fw-semibold" }, `+${r.change_pct.toFixed(1)}%`),
            el("td", {}, r.collect_only ? badge("수집전용", "secondary") : badge("매매", "success")),
            el("td", {}, watchBtn(r)),
          ]));
        }
        gtbl.appendChild(gtb);
        scannerC.body.appendChild(el("div", { class: "table-responsive" }, gtbl));
      }
      // --- 급등 조짐 (거래량 선행) ---
      if ((sc.presurge || []).length) {
        scannerC.body.appendChild(el("div", { class: "small fw-bold text-warning" }, "⚡ 급등 조짐 — 거래량 급증, 가격은 아직"));
        const ptbl = el("table", { class: "table table-sm align-middle mb-2" });
        ptbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>등락률</th><th>거래량 급증률</th><th></th></tr>" }));
        const ptb = el("tbody");
        for (const r of sc.presurge) {
          ptb.appendChild(el("tr", {}, [
            el("td", {}, `${r.name} (${r.code})`),
            el("td", {}, fmt(r.price)),
            el("td", {}, `${r.change_pct >= 0 ? "+" : ""}${r.change_pct.toFixed(1)}%`),
            el("td", { class: "text-warning" }, `+${fmt(r.surge_pct)}%`),
            el("td", {}, watchBtn(r)),
          ]));
        }
        ptbl.appendChild(ptb);
        scannerC.body.appendChild(el("div", { class: "table-responsive" }, ptbl));
      }
      if (!sc.results.length) {
        scannerC.body.appendChild(el("div", { class: "text-secondary small" }, "조건에 맞는 급등 종목 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>등락률</th><th>거래대금</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const r of sc.results) {
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${r.name} (${r.code})`),
          el("td", {}, fmt(r.price)),
          el("td", { class: "text-danger" }, `+${r.change_pct.toFixed(1)}%`),
          el("td", {}, fmt(r.trade_value)),
          el("td", {}, watchBtn(r)),
        ]));
      }
      tbl.appendChild(tb);
      scannerC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadDiscovery = async () => {
      let d;
      try {
        d = await fetchJSON("/api/trading/discovery");
      } catch (e) { return; }
      if (!changed("discovery", d)) return;
      discoveryC.body.innerHTML = "";
      discoveryC.body.appendChild(el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-gear"></i> 고정 3규칙(거래량·신고가·정배열) 기계 선별 · ETF/ETN/리츠 제외 · 참고용' })));
      const runBtn = el("button", { class: "btn btn-sm btn-outline-secondary mb-2" },
        d.running ? "실행 중… " + (d.progress || "") : "지금 분석 실행");
      runBtn.disabled = !!d.running;
      runBtn.onclick = async () => {
        if (!confirm("전종목 일봉 수집·분석을 시작할까요? (약 10~15분, 주문 없음)")) return;
        try { await postJSON("/api/trading/discovery/run"); } catch (e) { alert(e.message); }
        loadDiscovery();
      };
      discoveryC.body.appendChild(el("div", { class: "d-flex gap-2 align-items-center" }, [
        runBtn,
        el("span", { class: "text-secondary small" },
          d.date ? `기준일 ${d.date} · ${d.progress || ""}` : "아직 분석 결과 없음 (평일 17:30 자동 실행)"),
      ]));
      // --- 시장 국면(breadth) + 상대강도 ---
      const mk = d.market || {};
      if (mk.regime) {
        const tone = mk.regime === "강세" ? "danger" : mk.regime === "약세" ? "primary" : "secondary";
        discoveryC.body.appendChild(el("div", { class: "mb-2" }, [
          el("span", { class: "small text-secondary me-1" }, "시장 국면:"),
          badge(mk.regime, tone),
          el("span", { class: "small text-secondary ms-2" },
            `60이평 상회 ${mk.breadth_ma60}% · 20이평 ${mk.breadth_ma20}% · 중앙 20일수익률 ${mk.median_ret20}% (${mk.analyzed}종목)`),
        ]));
      }
      if (d.dataset) {
        discoveryC.body.appendChild(el("div", { class: "text-secondary small mb-2" },
          `📄 데이터셋: ${d.dataset.symbol_count}종목 피처 → ${d.dataset.features_file} (스케줄러 분석용)`));
      }
      // --- 하락(숏) 후보 — 약세장 반등 페이드용 ---
      if ((mk.bearish_top || []).length) {
        discoveryC.body.appendChild(el("div", { class: "small fw-bold text-primary mt-1" },
          `📉 하락(숏) 후보 ${mk.bearish_count}종목 — 역배열·저점근접·60이평 하회 (반등 페이드용)`));
        const bt = el("table", { class: "table table-sm align-middle mb-2 small" });
        bt.appendChild(el("thead", { html: "<tr><th>종목</th><th>종가</th><th>하락점수</th><th>상대강도</th><th></th></tr>" }));
        const btb = el("tbody");
        for (const p of mk.bearish_top.slice(0, 8)) {
          const link = el("a", { href: "#", class: "text-decoration-none" }, `${p.name} (${p.code})`);
          link.onclick = (e) => { e.preventDefault(); openStockChart(p.code, p.name); };
          btb.appendChild(el("tr", {}, [
            el("td", {}, link), el("td", {}, fmt(p.close)),
            el("td", {}, String(p.bearish_score)),
            el("td", { class: p.rs_20 < 0 ? "text-primary" : "text-secondary" }, `${p.rs_20}%p`),
            el("td", {}, watchBtn(p)),
          ]));
        }
        bt.appendChild(btb);
        discoveryC.body.appendChild(el("div", { class: "table-responsive" }, bt));
      }
      if (!(d.picks || []).length) return;
      discoveryC.body.appendChild(el("div", { class: "small fw-bold text-danger mt-1" }, "📈 상승(롱) 발굴"));
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>종가</th><th>점수</th><th>발굴 사유</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const p of d.picks) {
        const nameLink = el("a", { href: "#", class: "text-decoration-none" },
          `${p.name} (${p.code})`);
        nameLink.onclick = (e) => { e.preventDefault(); openStockChart(p.code, p.name); };
        tb.appendChild(el("tr", {}, [
          el("td", {}, [nameLink, el("i", { class: "bi bi-graph-up ms-1 small text-secondary" })]),
          el("td", {}, fmt(p.close)),
          el("td", {}, String(p.score)),
          el("td", { class: "small text-secondary" }, (p.reasons || []).join(" · ")),
          el("td", {}, watchBtn(p)),
        ]));
      }
      tbl.appendChild(tb);
      discoveryC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
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
