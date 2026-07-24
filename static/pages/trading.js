import { fetchJSON, el, card, badge } from "../app.js";
import { mdToHtml, renderIframe } from "./briefing.js";
import { makeLayoutEditable } from "../layout.js";
import { createProChart, MA_DEFS } from "../chart.js";

// 트레이딩 페이지: trading 서비스(127.0.0.1:8600)를 /api/trading/* 프록시로 조회하고
// 승인 대기 주문을 승인/거부한다. 차트는 외부 의존성 없는 캔버스 캔들로 그린다.

async function postJSON(path, body) {
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

const fmt = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });
// 가격 셀 렌더: 현재가 + (진입가 있으면) 괴리%. 초기 렌더·부분 갱신에 공용.
function priceCellHTML(cell, price, entry) {
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
// 생성 시각 → "N분 전" / 만료 시각 → "만료 M분" (staleness 표시)
const agoStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return m + "분 전";
  return Math.floor(m / 60) + "시간 " + (m % 60) + "분 전";
};
const leftStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  return m <= 0 ? "만료됨" : "만료까지 " + m + "분";
};
const sideBadge = (side) =>
  badge(side === "short" ? "숏" : "롱", side === "short" ? "danger" : "success");

export default {
  id: "trading",
  title: "매매·모니터링",
  icon: "bi-graph-down-arrow",
  group: "트레이딩",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // 변경 감지: 폴링 데이터가 실제로 바뀔 때만 DOM 을 다시 그린다.
    // (차트는 자체 캔버스라 무관 — 이걸로 나머지 카드의 주기적 깜빡임을 없앤다)
    const _memo = {};
    const changed = (key, data) => {
      const s = JSON.stringify(data);
      if (_memo[key] === s) return false;
      _memo[key] = s;
      return true;
    };

    const status = card("트레이딩 상태", null, { icon: "bi-activity" });
    const watchC = card("감시목록 관리", null, { icon: "bi-eye" });
    const pending = card("승인 대기 주문", null, { wide: true, icon: "bi-hourglass-split" });
    const scannerC = card("급등 스캐너", null, { wide: true, icon: "bi-rocket-takeoff" });
    const discoveryC = card("야간 발굴 (전일 전종목 분석)", null, { wide: true, icon: "bi-moon-stars" });
    const chart = card("1분봉 차트", null, { wide: true, icon: "bi-candlestick" });
    const signals = card("최근 신호", null, { wide: true, icon: "bi-lightning" });
    const reportC = card("분석 보고 리스트", null, { icon: "bi-journal-text" });
    const perfC = card("실거래 성과 로그", null, { icon: "bi-cash-coin" });
    const guardC = card("일일 목표·가드", null, { icon: "bi-shield-check" });
    // 각 카드를 독립 그리드 아이템으로 등록(id·기본 폭). 편집 모드에서 자유 배치·크기조절.
    const CARDS = [
      ["status", status, 6], ["guard", guardC, 6],
      ["watch", watchC, 6], ["pending", pending, 6],
      ["report", reportC, 6], ["scanner", scannerC, 12], ["discovery", discoveryC, 12],
      ["performance", perfC, 12],
      ["chart", chart, 12], ["signals", signals, 12],
    ];
    CARDS.forEach(([id, c, w], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;      // 초기화 시 원래 순서 복원용
      c.col.className = "col-12 col-xl-" + w;
      c.col.querySelector(".card").classList.add("h-100");
      row.appendChild(c.col);
    });
    // 저장된 배치·크기 복원 + '레이아웃 편집' 툴바 (브라우저별 localStorage)
    makeLayoutEditable(row, { key: "trading" });

    // --- 실거래 성과 로그 ---
    const perfBody = el("div");
    perfC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-cash-coin"></i> 승인·발주된 신호의 <b>진입가·청산가·실현손익·슬리피지</b>를 추적. 진입가는 주문체결 실시간 수신 시 <b>실측</b>으로 갱신(미수신분은 <b>근사</b>). 손절/목표 터치는 장중 30초 감시, 미청산분은 장 마감에 종가 정리. 딥리서치의 "실력 지속성은 실재" 검증용.' })),
      perfBody,
    );
    const closePosition = async (id) => {
      if (!confirm("이 추적 포지션을 현재가로 청산 처리할까요? (장부상 기록 — 실제 청산 주문은 별도)")) return;
      try { await postJSON(`/api/trading/positions/${id}/close`); _memo["perf"] = undefined; await loadPerformance(); }
      catch (e) { alert("실패: " + e.message); }
    };
    const won = (n) => (n > 0 ? "+" : "") + fmt(n) + "원";
    const pct = (n) => (n > 0 ? "+" : "") + n + "%";
    const loadPerformance = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/performance"); } catch (e) { return; }
      if (!changed("perf", d)) return;
      perfBody.innerHTML = "";
      const o = (d.stats && d.stats.overall) || {};
      if (!o.trades && !(d.open || []).length) {
        perfBody.appendChild(el("div", { class: "text-secondary small" },
          "아직 기록 없음 — 승인·발주된 주문이 생기면 여기에 진입/청산/손익이 쌓입니다."));
        return;
      }
      // 전체 요약
      if (o.trades) {
        const tone = o.total_pnl_krw >= 0 ? "text-danger" : "text-primary"; // 한국식: 이익 빨강
        const stat = (k, v, cls) => el("div", { class: "col-6 col-md-3 col-xl-2" },
          el("div", { class: "border rounded p-2" }, [el("div", { class: "text-secondary small" }, k), el("div", { class: "fw-semibold " + (cls || "") }, v)]));
        perfBody.appendChild(el("div", { class: "row g-2 mb-3" }, [
          stat("청산", o.trades + "건"), stat("승률", o.win_rate + "%"),
          stat("건당 기대값", pct(o.expectancy_pct)), stat("실현손익", won(o.total_pnl_krw), tone),
          stat("손익비(PF)", o.profit_factor == null ? "∞" : o.profit_factor), stat("평균 슬리피지", o.avg_slippage_pct + "%"),
        ]));
        const byRule = d.stats.by_rule || {};
        if (Object.keys(byRule).length) {
          perfBody.appendChild(el("div", { class: "small text-secondary mb-3" },
            "규칙별 기대값: " + Object.entries(byRule).map(([k, v]) => `${k} ${pct(v.expectancy_pct)}(${v.trades}건 · 승률 ${v.win_rate}%)`).join(" · ")));
        }
      }
      // 오픈 포지션
      const opens = d.open || [];
      perfBody.appendChild(el("div", { class: "fw-semibold small mb-1" }, `보유(추적 중) ${opens.length}건`));
      if (opens.length) {
        const t = el("table", { class: "table table-sm align-middle mb-3 small" });
        t.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입</th><th>체결</th><th>손절</th><th>목표</th><th></th></tr>" }));
        const tb = el("tbody");
        for (const p of opens) {
          const fc = p.fill_confirmed
            ? '<span class="badge text-bg-success">실측</span>'
            : '<span class="badge text-bg-secondary">근사</span>';
          const tr = el("tr", {
            html: `<td>${p.name || p.symbol}</td><td>${p.rule}</td>` +
              `<td>${p.side === "short" ? "숏" : "롱"}</td><td>${fmt(p.entry)}</td><td>${fc}</td>` +
              `<td>${fmt(p.stop)}</td><td>${fmt(p.target)}</td>`,
          });
          const td = el("td");
          const b = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "청산");
          b.onclick = () => closePosition(p.id);
          td.appendChild(b); tr.appendChild(td); tb.appendChild(tr);
        }
        t.appendChild(tb);
        perfBody.appendChild(el("div", { class: "table-responsive" }, t));
      } else {
        perfBody.appendChild(el("div", { class: "text-secondary small mb-3" }, "보유 포지션 없음"));
      }
      // 최근 청산
      const closed = d.closed || [];
      if (closed.length) {
        perfBody.appendChild(el("div", { class: "fw-semibold small mb-1" }, "최근 청산"));
        const t = el("table", { class: "table table-sm align-middle mb-0 small" });
        t.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입→청산</th><th>체결</th><th>사유</th><th>손익%</th><th>손익</th></tr>" }));
        const tb = el("tbody");
        for (const p of closed) {
          const cls = p.pnl_pct >= 0 ? "text-danger" : "text-primary";
          const fc = p.fill_confirmed ? "실측" : "근사";
          tb.appendChild(el("tr", {
            html: `<td>${p.name || p.symbol}</td><td>${p.rule}</td>` +
              `<td>${p.side === "short" ? "숏" : "롱"}</td><td>${fmt(p.entry)} → ${fmt(p.exit)}</td>` +
              `<td class="text-secondary">${fc}</td>` +
              `<td>${p.exit_reason}</td><td class="${cls}">${pct(p.pnl_pct)}</td>` +
              `<td class="${cls}">${won(p.pnl_krw)}</td>`,
          }));
        }
        t.appendChild(tb);
        perfBody.appendChild(el("div", { class: "table-responsive" }, t));
      }
    };

    // --- 일일 목표·가드 (목표값 설정 가능) ---
    const gTarget = el("input", { class: "form-control form-control-sm", type: "number", step: "0.1", min: "0", style: "max-width:88px" });
    const gLoss = el("input", { class: "form-control form-control-sm", type: "number", step: "0.1", min: "0", style: "max-width:88px" });
    const gRisk = el("input", { class: "form-control form-control-sm", type: "number", step: "0.1", min: "0", max: "50", style: "max-width:88px" });
    const gSave = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "저장");
    const gStatus = el("div", { class: "mt-2 small" });
    const saveRisk = async () => {
      gSave.disabled = true;
      try {
        await postJSON("/api/trading/risk", {
          daily_target_pct: parseFloat(gTarget.value),
          daily_loss_limit_pct: parseFloat(gLoss.value),
          risk_per_trade_pct: parseFloat(gRisk.value),
        });
        _memo["risk"] = undefined;
        await loadRisk();
      } catch (e) { alert("저장 실패: " + e.message); }
      finally { gSave.disabled = false; }
    };
    gSave.onclick = saveRisk;
    const fld = (lbl, input) => el("div", {}, [el("label", { class: "form-label small text-secondary mb-0" }, lbl), input]);
    guardC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-shield-check"></i> <b>거래당 리스크</b> = 1회 손절 시 계좌 대비 최대 손실 %(주문 수량을 정함). <b>일일 목표/손실한도</b> = 당일 실현손익이 도달하면 그날 신규 진입을 멈춤. (실거래 성과 로그 기준)' })),
      el("div", { class: "d-flex gap-3 flex-wrap align-items-end" },
        [fld("거래당 리스크 %", gRisk), fld("일일 목표 %", gTarget), fld("손실 한도 %", gLoss), el("div", {}, gSave)]),
      el("div", { class: "small text-secondary mt-1" },
        "팁: 거래당 리스크는 일일 손실한도보다 작게 두세요(예: 0.5% ↔ 1.5% = 하루 손절 3번 여유). 소액 계좌는 절대금액이 작아 대부분 1~2주로 잡힙니다."),
      gStatus,
    );
    const loadRisk = async () => {
      let r;
      try { r = await fetchJSON("/api/trading/risk"); } catch (e) { return; }
      if (!changed("risk", r)) return;
      if (document.activeElement !== gTarget) gTarget.value = r.daily_target_pct;
      if (document.activeElement !== gLoss) gLoss.value = r.daily_loss_limit_pct;
      if (document.activeElement !== gRisk) gRisk.value = r.risk_per_trade_pct;
      gStatus.innerHTML = "";
      const cls = r.pct >= 0 ? "text-danger" : "text-primary";
      gStatus.append(el("div", {}, [
        el("span", { class: "text-secondary" }, "오늘 실현손익 "),
        el("span", { class: "fw-semibold " + cls }, `${won(r.krw)} (${pct(r.pct)})`),
        el("span", { class: "text-secondary" }, ` · ${r.trades}건`),
      ]));
      const bar = el("div", { class: "progress mt-2", style: "height:8px" });
      const hi = r.daily_target_pct || 0;
      const frac = hi > 0 ? Math.max(0, Math.min(100, r.pct / hi * 100)) : 0;
      bar.appendChild(el("div", { class: "progress-bar " + (r.pct >= 0 ? "bg-danger" : "bg-primary"), style: `width:${r.pct >= 0 ? frac : 0}%` }));
      gStatus.appendChild(bar);
      gStatus.appendChild(el("div", { class: "mt-2" },
        r.halted ? el("span", { class: "badge text-bg-warning" }, r.reason)
          : el("span", { class: "badge text-bg-success" }, "정상 — 진입 허용")));
      if (!r.halted && hi > 0 && r.pct < hi) {
        gStatus.appendChild(el("div", { class: "text-secondary mt-1" }, `목표까지 ${(hi - r.pct).toFixed(2)}% 남음`));
      }
    };

    // --- 분석 보고 리스트 + 공용 모달 ---
    const rBody = el("div");
    reportC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-robot"></i> AI가 전종목 데이터를 분석해 독립 선별 · 기술적 소견 + 최신 뉴스 포함' })),
      rBody,
    );

    // 리포트 표시 모달 (리스트/야간발굴 링크 공용) — API 설정 모달과 변수 충돌 방지 위해 report* 접두
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
    // 기간 버튼
    const periods = [["1개월", 21], ["3개월", 63], ["6개월", 126], ["1년", 252], ["전체", "all"]];
    const periodGroup = el("div", { class: "btn-group btn-group-sm" });
    const periodBtns = periods.map(([lbl, n]) => {
      const b = el("button", { class: "btn btn-outline-secondary", type: "button" }, lbl);
      b.onclick = () => { proChart.setVisibleCount(n); periodBtns.forEach((x) => x.classList.toggle("active", x === b)); };
      periodGroup.appendChild(b);
      return b;
    });
    // 볼린저밴드 토글
    const bbBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "볼린저밴드");
    let bbOn = false;
    bbBtn.onclick = () => { bbOn = !bbOn; bbBtn.classList.toggle("active", bbOn); proChart.setIndicator("bb", bbOn); };
    // 이동평균 범례
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
    // 모달이 완전히 표시돼 폭이 잡힌 뒤 다시 그린다
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
      setDiscReportLink(d.date);  // 서버가 최신 날짜를 d.date 로 반환
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

    // 상태 카드 헤더에 설정(기어) 버튼 추가 → 클릭 시 API 설정 모달 표시
    const statusHeader = status.col.querySelector(".card-header");
    statusHeader.classList.add("d-flex", "justify-content-between", "align-items-center");
    const gearBtn = el("button", {
      class: "btn btn-sm btn-link p-0 text-secondary", title: "키움 API 설정",
    }, el("i", { class: "bi bi-gear-fill" }));
    statusHeader.appendChild(gearBtn);

    // --- 감시목록 관리: 종목명/코드로 추가 + 영속 목록 + 제거 ---
    const SOURCE_BADGE = { seed: ["기본", "secondary"], manual: ["수동", "primary"], auto: ["발굴", "warning"] };
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
        const btBtn = el("button", { class: "btn btn-sm btn-outline-secondary py-0 me-1", title: "규칙 백테스트 페이지에서 실행" }, "백테스트");
        btBtn.onclick = () => {
          // 백테스트는 별도 페이지 — 종목을 넘기고 이동하면 그 페이지에서 자동 실행
          sessionStorage.setItem("backtest:symbol", it.code);
          location.hash = "#/backtest";
        };
        const modeBtn = el("button", { class: "btn btn-sm btn-outline-primary py-0 me-1",
          title: it.collect_only ? "매매 대상으로 전환" : "수집전용으로 전환(매매 제외)" },
          it.collect_only ? "매매로" : "수집전용");
        modeBtn.onclick = async () => {
          try { await postJSON("/api/trading/watchlist/mode", { code: it.code, collect_only: !it.collect_only }); }
          catch (e) { alert("실패: " + e.message); }
          loadWatch(); loadStatus();
        };
        const rm = el("button", { class: "btn btn-sm btn-outline-danger py-0" }, "제거");
        rm.onclick = async () => {
          if (!confirm(`${it.name}(${it.code}) 을 감시목록에서 제거할까요?`)) return;
          try { await postJSON("/api/trading/watchlist/remove", { code: it.code }); }
          catch (e) { alert("실패: " + e.message); }
          loadWatch(); loadStatus();
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
        // 성공
        wQuery.value = "";
        loadWatch(); loadStatus();
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

    // --- 키움 API 설정 폼 (시크릿은 서버가 원문을 돌려주지 않음 — 변경 시에만 입력) ---
    const envSel = el("select", { class: "form-select form-select-sm" }, [
      el("option", { value: "mock" }, "모의투자 (mockapi)"),
      el("option", { value: "real" }, "실전 (api.kiwoom.com)"),
    ]);
    const appKeyIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const secretIn = el("input", { class: "form-control form-control-sm", type: "password", autocomplete: "new-password" });
    const accountIn = el("input", { class: "form-control form-control-sm", autocomplete: "off" });
    const saveBtn = el("button", { class: "btn btn-sm btn-primary mt-2" }, "저장");
    const cfgMsg = el("div", { class: "small mt-2" });
    const field = (label, input) =>
      el("div", { class: "mb-2" }, [el("label", { class: "form-label small mb-1" }, label), input]);

    // API 설정 모달 (기어 버튼으로 연다)
    const modalBody = el("div", { class: "modal-body" }, [
      field("환경", envSel),
      field("앱키 (App Key)", appKeyIn),
      field("시크릿 키 (Secret Key)", secretIn),
      field("계좌번호", accountIn),
      cfgMsg,
    ]);
    const modalEl = el("div", { class: "modal fade", tabindex: "-1" },
      el("div", { class: "modal-dialog modal-dialog-centered" },
        el("div", { class: "modal-content" }, [
          el("div", { class: "modal-header" }, [
            el("h5", { class: "modal-title", html: '<i class="bi bi-key"></i> 키움 API 설정' }),
            el("button", { class: "btn-close", type: "button", "data-bs-dismiss": "modal" }),
          ]),
          modalBody,
          el("div", { class: "modal-footer" }, [
            el("button", { class: "btn btn-sm btn-secondary", type: "button", "data-bs-dismiss": "modal" }, "닫기"),
            saveBtn,
          ]),
        ]),
      ),
    );
    container.appendChild(modalEl);
    saveBtn.className = "btn btn-sm btn-primary";  // 모달 푸터용 (mt-2 제거)
    const settingsModal = new bootstrap.Modal(modalEl);
    gearBtn.onclick = () => { loadSettings(); settingsModal.show(); };

    const loadSettings = async () => {
      try {
        const s = await fetchJSON("/api/trading/settings");
        envSel.value = s.env;
        appKeyIn.placeholder = s.app_key_masked || "미설정";
        secretIn.placeholder = s.has_secret ? "설정됨 — 변경 시에만 입력" : "미설정";
        accountIn.placeholder = s.account || s.account_masked || "미설정";
      } catch (e) { /* 서비스 다운은 상태 카드가 알림 */ }
    };
    saveBtn.onclick = async () => {
      if (envSel.value === "real" &&
          !confirm("실전 환경으로 저장합니다. 승인된 주문은 실제 계좌로 발주됩니다. 계속할까요?")) return;
      saveBtn.disabled = true;
      cfgMsg.textContent = "";
      try {
        const r = await postJSON("/api/trading/settings", {
          env: envSel.value,
          app_key: appKeyIn.value,
          secret_key: secretIn.value,
          account: accountIn.value,
        });
        cfgMsg.className = "small mt-2 text-success";
        cfgMsg.textContent = `저장됨 (환경: ${r.env === "real" ? "실전" : "모의투자"})`;
        appKeyIn.value = secretIn.value = accountIn.value = "";
        loadSettings();
        loadStatus();
      } catch (e) {
        cfgMsg.className = "small mt-2 text-danger";
        cfgMsg.textContent = "저장 실패: " + e.message;
      } finally {
        saveBtn.disabled = false;
      }
    };

    // 1분봉도 상용급 인터랙티브 차트(시각 축) — 실시간 갱신은 확대/이동을 보존.
    const mHost = el("div", { style: "width:100%;height:52vh;min-height:300px" });
    const symbolSel = el("select", { class: "form-select form-select-sm w-auto" });
    const mChart = createProChart(mHost, { up: "#d64545", down: "#3a6fd8", axis: "time" });
    const mPeriods = [["30분", 30], ["1시간", 60], ["2시간", 120], ["전체", "all"]];
    const mPeriodGroup = el("div", { class: "btn-group btn-group-sm" });
    const mPeriodBtns = mPeriods.map(([lbl, n]) => {
      const b = el("button", { class: "btn btn-outline-secondary", type: "button" }, lbl);
      b.onclick = () => { mChart.setVisibleCount(n); mPeriodBtns.forEach((x) => x.classList.toggle("active", x === b)); };
      mPeriodGroup.appendChild(b);
      return b;
    });
    const mBB = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "볼린저밴드");
    let mBBon = false;
    mBB.onclick = () => { mBBon = !mBBon; mBB.classList.toggle("active", mBBon); mChart.setIndicator("bb", mBBon); };
    const mLegend = el("div", { class: "small d-flex gap-2 flex-wrap align-items-center ms-auto" },
      MA_DEFS.map((d) => el("span", { style: `color:${d.color};font-weight:600` }, `━ MA${d.p}`)));
    chart.body.append(
      el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
        [symbolSel, mPeriodGroup, mBB, el("span", { class: "small text-secondary" }, "휠 확대·드래그 이동·더블클릭 리셋"), mLegend]),
      mHost,
    );

    let watch = {};
    let mCurSym = "";
    const loadChart = async () => {
      if (!symbolSel.value) return;
      try {
        const bars = await fetchJSON("/api/trading/bars/" + symbolSel.value + "?tf=1m&live=1");
        if (symbolSel.value !== mCurSym) {
          mCurSym = symbolSel.value;
          mChart.setData(bars);                       // 종목 전환 → 새로 그림(확대 리셋)
          mPeriodBtns.forEach((x) => x.classList.remove("active"));
        } else {
          mChart.update(bars);                        // 실시간 갱신 → 확대/이동 보존
        }
      } catch (e) { /* 서비스 다운 시 상태 카드에 표시됨 */ }
    };
    symbolSel.onchange = loadChart;

    const loadStatus = async () => {
      let s;
      try {
        s = await fetchJSON("/api/trading/status");
      } catch (e) {
        if (changed("status", "__err__")) {
          status.body.innerHTML = "";
          status.body.appendChild(
            el("div", { class: "text-danger small" },
              "trading 서비스에 연결할 수 없습니다. systemctl status trading 확인.")
          );
        }
        return;
      }
      let a = null;
      try { a = await fetchJSON("/api/trading/account"); } catch (e) { a = null; }
      // 상태+계좌가 직전과 동일하면 다시 그리지 않는다 (깜빡임 제거)
      if (!changed("status", [s, a])) return;
      status.body.innerHTML = "";
      const envB = badge(s.env === "real" ? "실전" : "모의투자", s.env === "real" ? "danger" : "success");
      const clockBadge = s.server_time
        ? [" ", badge(s.clock_synced ? "동기화 ✓" : "미동기화 ⚠", s.clock_synced ? "success" : "danger")]
        : [];
      const list = el("ul", { class: "list-unstyled small mb-0" }, [
        el("li", {}, ["환경: ", envB]),
        el("li", {}, "엔진: " + (s.engine_enabled ? "가동" : "꺼짐(API 키 미설정)")),
        s.server_time ? el("li", {}, ["서버 시각: " + s.server_time.slice(0, 19).replace("T", " "), ...clockBadge]) : null,
        el("li", {}, "마지막 평가: " + (s.last_run ? s.last_run.slice(0, 19).replace("T", " ") : "—")),
        el("li", {}, "당일 실현손익: " + fmt(s.daily_pnl) + " 원"),
        s.loss_limit_hit
          ? el("li", { class: "text-danger fw-bold" }, "일일 손실 한도 도달 — 신규 신호 차단 중")
          : null,
      ]);
      status.body.appendChild(list);
      // --- 계좌 내역 ---
      if (a) {
        status.body.appendChild(el("hr", { class: "my-2" }));
        if (!a.ok) {
          status.body.appendChild(
            el("div", { class: "text-secondary small" }, "계좌 조회 불가: " + (a.error || ""))
          );
        } else {
          const plTone = a.total_pl >= 0 ? "text-danger" : "text-primary"; // 한국식: 수익 빨강
          status.body.appendChild(
            el("ul", { class: "list-unstyled small mb-1" }, [
              el("li", {}, `계좌번호: ${a.account_no || a.account_name || "—"}`),
              el("li", {}, `추정예탁자산: ${fmt(a.deposit_est)} 원`),
              el("li", {}, `총평가금액: ${fmt(a.total_eval)} 원 (매입 ${fmt(a.total_buy)})`),
              el("li", { class: plTone },
                `평가손익: ${fmt(a.total_pl)} 원 (${a.total_pl_rt.toFixed(2)}%)`),
            ])
          );
          if (a.holdings.length) {
            const tbl = el("table", { class: "table table-sm small mb-0" });
            tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>수량</th><th>평단</th><th>현재가</th><th>손익</th></tr>" }));
            const tb = el("tbody");
            for (const h of a.holdings) {
              tb.appendChild(el("tr", {}, [
                el("td", {}, h.name || h.code),
                el("td", {}, fmt(h.qty)),
                el("td", {}, fmt(h.avg_price)),
                el("td", {}, fmt(h.cur_price)),
                el("td", { class: h.pl_amt >= 0 ? "text-danger" : "text-primary" },
                  `${fmt(h.pl_amt)} (${h.pl_rt.toFixed(1)}%)`),
              ]));
            }
            tbl.appendChild(tb);
            status.body.appendChild(el("div", { class: "table-responsive" }, tbl));
          } else {
            status.body.appendChild(el("div", { class: "text-secondary small" }, "보유 종목 없음"));
          }
        }
      }
      if (s.watchlist && JSON.stringify(s.watchlist) !== JSON.stringify(watch)) {
        const keep = symbolSel.value;
        watch = s.watchlist;
        symbolSel.innerHTML = "";
        for (const [code, name] of Object.entries(watch)) {
          symbolSel.appendChild(el("option", { value: code }, `${name} (${code})`));
        }
        if (keep && watch[keep]) symbolSel.value = keep;
        loadChart();
        // 감시목록이 바뀌면 발굴·스캐너의 '감시중' 표시를 즉시 반영(강제 재렌더)
        _memo["discovery"] = undefined; _memo["scanner"] = undefined;
        loadDiscovery(); loadScanner();
      }
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
      const watchBtn = (r) => {
        if (watch[r.code]) return badge("감시중", "success");  // 이미 등록 → 버튼 숨김
        const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
        add.onclick = async () => {
          add.disabled = true;
          try {
            await postJSON("/api/trading/watchlist", { code: r.code, name: r.name });
            add.textContent = "추가됨";
            add.classList.replace("btn-outline-primary", "btn-success");
            loadWatch(); loadStatus();   // 감시목록 카드·차트 드롭다운 즉시 갱신
          } catch (e) { alert("실패: " + e.message); add.disabled = false; }
        };
        return add;
      };
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
            el("td", {}, watch[r.code] ? badge("감시중", "success") : watchBtn(r)),
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
          const addCell = watch[p.code] ? badge("감시중", "success") : (() => {
            const a = el("button", { class: "btn btn-sm btn-outline-primary py-0" }, "감시 추가");
            a.onclick = async () => {
              a.disabled = true;
              try { await postJSON("/api/trading/watchlist", { code: p.code, name: p.name }); a.textContent = "추가됨"; a.classList.replace("btn-outline-primary", "btn-success"); loadWatch(); loadStatus(); }
              catch (e) { alert("실패: " + e.message); a.disabled = false; }
            };
            return a;
          })();
          btb.appendChild(el("tr", {}, [
            el("td", {}, link), el("td", {}, fmt(p.close)),
            el("td", {}, String(p.bearish_score)),
            el("td", { class: p.rs_20 < 0 ? "text-primary" : "text-secondary" }, `${p.rs_20}%p`),
            el("td", {}, addCell),
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
        let addCell;
        if (watch[p.code]) {
          addCell = badge("감시중", "success");   // 이미 등록 → 감시 추가 버튼 숨김
        } else {
          const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
          add.onclick = async () => {
            add.disabled = true;
            try {
              await postJSON("/api/trading/watchlist", { code: p.code, name: p.name });
              add.textContent = "추가됨";
              add.classList.replace("btn-outline-primary", "btn-success");
              loadWatch(); loadStatus();   // 감시목록 카드·차트 드롭다운 즉시 갱신
            } catch (e) { alert("실패: " + e.message); add.disabled = false; }
          };
          addCell = add;
        }
        const nameLink = el("a", { href: "#", class: "text-decoration-none" },
          `${p.name} (${p.code})`);
        nameLink.onclick = (e) => { e.preventDefault(); openStockChart(p.code, p.name); };
        tb.appendChild(el("tr", {}, [
          el("td", {}, [nameLink, el("i", { class: "bi bi-graph-up ms-1 small text-secondary" })]),
          el("td", {}, fmt(p.close)),
          el("td", {}, String(p.score)),
          el("td", { class: "small text-secondary" }, (p.reasons || []).join(" · ")),
          el("td", {}, addCell),
        ]));
      }
      tbl.appendChild(tb);
      discoveryC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const ORDER_STATUS = {
      pending: ["대기", "secondary"], approved: ["승인", "info"],
      sent: ["발주됨", "success"], rejected: ["거부됨", "danger"],
      error: ["오류", "danger"], expired: ["만료", "secondary"],
    };
    const orderMsg = (o) => {
      try {
        const r = JSON.parse(o.result || "{}");
        if (o.status === "sent") return r.ord_no ? "주문번호 " + r.ord_no : "발주 접수";
        return r.return_msg || r.error || "";
      } catch (e) { return ""; }
    };
    const loadOrders = async () => {
      let all;
      try {
        all = await fetchJSON("/api/trading/orders");
      } catch (e) { return; }
      // 사용자가 발주 수량/금액을 편집 중이면 새로고침으로 입력값을 덮어쓰지 않는다.
      const act = document.activeElement;
      if (act && act.tagName === "INPUT" && pending.body.contains(act)) return;
      if (!changed("orders", all)) return;
      const orders = all.filter((o) => o.status === "pending");
      const history = all.filter((o) => o.status !== "pending").slice(0, 8);
      pending.body.innerHTML = "";
      if (!orders.length) {
        pending.body.appendChild(el("div", { class: "text-secondary small mb-2" }, "대기 중인 주문 없음"));
      } else {
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량 / 발주금액</th><th>경과·현재가 괴리</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const o of orders) {
        const isExit = o.kind === "exit";
        const approve = el("button", { class: "btn btn-sm " + (isExit ? "btn-warning" : "btn-success") + " me-1" }, isExit ? "청산 승인" : "승인");
        const rejectB = el("button", { class: "btn btn-sm btn-outline-danger" }, isExit ? "보류" : "거부");
        // 발주 수량·금액 — 사용자가 승인 전 조정 가능(진입 주문). 금액은 현재가/진입가 기준 예상치.
        const baseQty = o.exec_qty ?? o.qty;
        const px = Number(o.cur_price) || Number(o.entry) || 0;
        let qtyCell;
        const readQty = () => Math.max(0, Math.floor(Number(o.qty) || 0));
        if (isExit) {
          qtyCell = el("td", {}, `${String(baseQty)}주`);
        } else {
          const qtyIn = el("input", { type: "number", min: "1", step: "1", value: String(baseQty),
            class: "form-control form-control-sm text-end", style: "width:5rem" });
          const amtIn = el("input", { type: "number", min: "0", step: "100", value: px ? String(Math.round(baseQty * px)) : "",
            class: "form-control form-control-sm text-end", style: "width:8rem", disabled: px ? null : "" });
          qtyIn.oninput = () => { if (px) amtIn.value = String(Math.round((Number(qtyIn.value) || 0) * px)); };
          amtIn.oninput = () => { if (px) qtyIn.value = String(Math.floor((Number(amtIn.value) || 0) / px)); };
          o._qtyIn = qtyIn;   // approve 핸들러에서 읽음
          qtyCell = el("td", {}, el("div", { class: "d-flex flex-column gap-1" }, [
            el("div", { class: "input-group input-group-sm", style: "width:6.5rem" }, [qtyIn, el("span", { class: "input-group-text" }, "주")]),
            el("div", { class: "input-group input-group-sm", style: "width:9.5rem" }, [amtIn, el("span", { class: "input-group-text" }, "원")]),
          ]));
        }
        approve.onclick = async () => {
          const qty = isExit ? Number(baseQty) : (o._qtyIn ? Math.floor(Number(o._qtyIn.value) || 0) : readQty());
          if (!isExit && qty < 1) { alert("발주 수량은 1주 이상이어야 합니다"); return; }
          const amtStr = px ? ` (약 ${fmt(qty * px)}원)` : "";
          const msg = isExit
            ? `[${o.symbol}] 목표 도달 — ${qty}주 시장가 매도(청산)할까요?`
            : `[${o.symbol}] ${o.rule} ${o.side} ${qty}주${amtStr} — 실제로 발주할까요?`;
          if (!confirm(msg)) return;
          approve.disabled = true;
          try {
            const r = await postJSON(`/api/trading/orders/${o.id}/approve`, isExit ? undefined : { qty });
            if (r.ok) alert("✅ 발주 접수됨\n" + (r.message || ""));
            else if (r.retryable) alert("⚠️ " + (r.message || "증거금 부족 — 대기열에 유지됨"));
            else alert("❌ 발주 거부/실패\n" + (r.message || r.error || ""));
          } catch (e) { alert("발주 오류: " + e.message); }
          loadOrders(); loadPerformance();
        };
        rejectB.onclick = async () => {
          try { await postJSON(`/api/trading/orders/${o.id}/reject`); } catch (e) {}
          loadOrders();
        };
        // 신호 진입가 대비 현재가 괴리 (이미 멀어진 신호를 걸러내게)
        const gap = (o.cur_price && o.entry) ? (o.cur_price - o.entry) / o.entry * 100 : null;
        const gapEl = gap == null ? null : el("div", { class: Math.abs(gap) >= 0.5 ? "text-danger" : "text-secondary" },
          `현재 ${fmt(o.cur_price)} (${gap >= 0 ? "+" : ""}${gap.toFixed(2)}%)`);
        tb.appendChild(el("tr", {}, [
          el("td", {}, o.name && o.name !== o.symbol ? `${o.name} (${o.symbol})` : o.symbol),
          el("td", {}, o.rule),
          el("td", {}, isExit ? badge("청산", "warning") : sideBadge(o.side)),
          el("td", {}, `${fmt(o.entry)} / ${fmt(o.stop)} / ${fmt(o.target)}`),
          qtyCell,
          el("td", { class: "small text-secondary text-nowrap" }, [
            el("div", {}, agoStr(o.created)),
            el("div", {}, leftStr(o.expires)),
            gapEl,
          ]),
          el("td", {}, [approve, rejectB]),
        ]));
      }
      tbl.appendChild(tb);
      pending.body.appendChild(el("div", { class: "table-responsive" }, tbl));
      }

      // 최근 주문 결과 — 승인 후 실제 발주/거부 이력 (사용자가 결과를 확인)
      const histWrap = el("div", { class: "mt-3" });
      histWrap.appendChild(el("div", { class: "small text-secondary mb-1" }, "최근 주문 결과"));
      if (!history.length) {
        histWrap.appendChild(el("div", { class: "text-secondary small" }, "아직 발주 이력 없음"));
      } else {
        const htbl = el("table", { class: "table table-sm align-middle mb-0" });
        htbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>방향</th><th>수량</th><th>상태</th><th>결과</th></tr>" }));
        const htb = el("tbody");
        for (const o of history) {
          const [label, color] = ORDER_STATUS[o.status] || [o.status, "secondary"];
          htb.appendChild(el("tr", {}, [
            el("td", { class: "small text-nowrap" }, agoStr(o.created)),
            el("td", {}, o.name && o.name !== o.symbol ? `${o.name} (${o.symbol})` : o.symbol),
            el("td", {}, o.kind === "exit" ? badge("청산", "warning") : sideBadge(o.side)),
            el("td", {}, String(o.exec_qty ?? o.qty)),
            el("td", {}, badge(label, color)),
            el("td", { class: "small" }, orderMsg(o)),
          ]));
        }
        htbl.appendChild(htb);
        histWrap.appendChild(el("div", { class: "table-responsive" }, htbl));
      }
      pending.body.appendChild(histWrap);
    };

    const loadSignals = async () => {
      let sigs;
      try {
        sigs = await fetchJSON("/api/trading/signals");
      } catch (e) { return; }
      // 가격은 refreshPrices 가 셀만 갱신 — 표 재렌더는 신호 목록이 바뀔 때만.
      const sKey = sigs.map((s) => `${s.ts}:${s.symbol}:${s.rule}:${s.qty}:${s.actionable ? 1 : 0}`).join("|");
      if (!changed("signals", sKey)) return;
      signals.body.innerHTML = "";
      if (!sigs.length) {
        signals.body.appendChild(el("div", { class: "text-secondary small" }, "오늘 신호 없음"));
        return;
      }
      signals.body.appendChild(el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-info-circle"></i> 감시 신호는 <b>금액 제한 없이</b> 산출(감사용) · 실제 발주는 잔고를 반영한 “승인 대기 주문”에서 · 진입가는 <b>감지 시점 기준</b>' })));
      const tbl = el("table", { class: "table table-sm mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>현재가</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량·상태</th><th>사유</th></tr>" }));
      const tb = el("tbody");
      for (const s of sigs.slice(0, 15)) {
        const statusEl = s.actionable
          ? badge(`승인대기 ${s.qty}주`, "success")
          : el("span", { class: "small text-secondary", title: s.note || "" },
              s.qty >= 1 ? badge("보류", "secondary") : badge("잔고 부족", "warning"));
        // 현재가 셀 — data-px/data-entry 로 태깅해 refreshPrices 가 값만 갱신한다.
        const curTd = el("td", { class: "small text-end text-nowrap", "data-px": s.symbol,
          "data-entry": s.entry || "" }, s.cur_price ? fmt(s.cur_price) : "—");
        priceCellHTML(curTd, s.cur_price, s.entry);
        tb.appendChild(el("tr", {}, [
          el("td", { class: "small text-secondary text-nowrap" }, agoStr(s.ts)),
          el("td", {}, `${s.name} (${s.symbol})`),
          curTd,
          el("td", {}, s.rule),
          el("td", {}, sideBadge(s.side)),
          el("td", { class: "small" }, s.entry ? `${fmt(s.entry)} / ${fmt(s.stop)} / ${fmt(s.target)}` : "—"),
          el("td", { class: "small text-nowrap" }, statusEl),
          el("td", { class: "small text-secondary" }, s.note || s.reason),
        ]));
      }
      tbl.appendChild(tb);
      signals.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    // 현재가만 2초 주기로 셀 부분 갱신(표 재렌더 없이 → 버튼 안 흔들림)
    const refreshPrices = async () => {
      let m;
      try { m = await fetchJSON("/api/trading/prices"); } catch (e) { return; }
      const prices = (m && m.prices) || {};
      container.querySelectorAll("[data-px]").forEach((cell) => {
        const p = prices[cell.getAttribute("data-px")];
        if (p == null) return;   // 값 없으면 직전 표시 유지
        priceCellHTML(cell, p, cell.getAttribute("data-entry"));
      });
    };

    await Promise.all([loadStatus(), loadOrders(), loadSignals(), loadScanner(), loadDiscovery(), loadWatch(), loadReport(), loadPerformance(), loadRisk()]);
    ctx.addTimer(setInterval(refreshPrices, 2_000));   // 현재가 셀만 2초 갱신
    ctx.addTimer(setInterval(() => { loadStatus(); loadOrders(); loadSignals(); loadScanner(); }, 10_000));
    ctx.addTimer(setInterval(() => { loadDiscovery(); loadWatch(); loadPerformance(); loadRisk(); }, 30_000));
    ctx.addTimer(setInterval(() => { loadReport(); }, 300_000));
    ctx.addTimer(setInterval(loadChart, 5_000)); // 실시간 분봉 (WS 집계 + 형성 중 봉 포함)
  },
};
