import { fetchJSON, el, card, badge } from "../app.js";

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
const sideBadge = (side) =>
  badge(side === "short" ? "숏" : "롱", side === "short" ? "danger" : "success");

// --- 캔버스 캔들차트 (한국식: 상승 빨강 / 하락 파랑) ---
function drawCandles(canvas, bars) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600;
  const h = 320;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  canvas.style.height = h + "px";
  const g = canvas.getContext("2d");
  g.scale(dpr, dpr);
  g.clearRect(0, 0, w, h);
  if (!bars.length) {
    g.fillStyle = "#888";
    g.font = "13px sans-serif";
    g.fillText("데이터 없음 — 장중 수집 후 표시됩니다", 16, 40);
    return;
  }
  const view = bars.slice(-120);
  const padL = 8, padR = 56, padY = 12;
  const lo = Math.min(...view.map((b) => b.low));
  const hi = Math.max(...view.map((b) => b.high));
  const y = (p) => padY + (hi - p) / (hi - lo || 1) * (h - padY * 2);
  const cw = (w - padL - padR) / view.length;
  view.forEach((b, i) => {
    const x = padL + i * cw + cw / 2;
    const up = b.close >= b.open;
    g.strokeStyle = g.fillStyle = up ? "#d64545" : "#3a6fd8";
    g.beginPath();
    g.moveTo(x, y(b.high));
    g.lineTo(x, y(b.low));
    g.stroke();
    const bh = Math.max(1, Math.abs(y(b.open) - y(b.close)));
    g.fillRect(x - cw * 0.35, Math.min(y(b.open), y(b.close)), cw * 0.7, bh);
  });
  // 우측 가격 축 (고/중/저)
  g.fillStyle = "#999";
  g.font = "11px sans-serif";
  [hi, (hi + lo) / 2, lo].forEach((p) => g.fillText(fmt(p), w - padR + 6, y(p) + 4));
  // 하단 시간 축 (시작/끝)
  const t = (s) => new Date(s * 1000).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
  g.fillText(t(view[0].time), padL, h - 2);
  g.fillText(t(view[view.length - 1].time), w - padR - 40, h - 2);
}

export default {
  id: "trading",
  title: "트레이딩",
  icon: "bi-graph-down-arrow",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const status = card("트레이딩 상태", null, { icon: "bi-activity" });
    const apiCfg = card("키움 API 설정", null, { icon: "bi-key" });
    const pending = card("승인 대기 주문", null, { wide: true, icon: "bi-hourglass-split" });
    const scannerC = card("급등 스캐너 (하락장 주도주)", null, { wide: true, icon: "bi-rocket-takeoff" });
    const chart = card("1분봉 차트", null, { wide: true, icon: "bi-candlestick" });
    const signals = card("최근 신호", null, { wide: true, icon: "bi-lightning" });
    row.append(status.col, apiCfg.col, pending.col, scannerC.col, chart.col, signals.col);

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
    apiCfg.body.append(
      field("환경", envSel),
      field("앱키 (App Key)", appKeyIn),
      field("시크릿 키 (Secret Key)", secretIn),
      field("계좌번호", accountIn),
      saveBtn, cfgMsg,
    );

    const loadSettings = async () => {
      try {
        const s = await fetchJSON("/api/trading/settings");
        envSel.value = s.env;
        appKeyIn.placeholder = s.app_key_masked || "미설정";
        secretIn.placeholder = s.has_secret ? "설정됨 — 변경 시에만 입력" : "미설정";
        accountIn.placeholder = s.account_masked || "미설정";
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

    const canvas = el("canvas", { style: "width:100%" });
    const symbolSel = el("select", { class: "form-select form-select-sm w-auto mb-2" });
    chart.body.append(symbolSel, canvas);

    let watch = {};
    const loadChart = async () => {
      if (!symbolSel.value) return;
      try {
        drawCandles(canvas, await fetchJSON("/api/trading/bars/" + symbolSel.value));
      } catch (e) { /* 서비스 다운 시 상태 카드에 표시됨 */ }
    };
    symbolSel.onchange = loadChart;

    const loadStatus = async () => {
      let s;
      try {
        s = await fetchJSON("/api/trading/status");
      } catch (e) {
        status.body.innerHTML = "";
        status.body.appendChild(
          el("div", { class: "text-danger small" },
            "trading 서비스에 연결할 수 없습니다. systemctl status trading 확인.")
        );
        return;
      }
      status.body.innerHTML = "";
      const envB = badge(s.env === "real" ? "실전" : "모의투자", s.env === "real" ? "danger" : "success");
      const list = el("ul", { class: "list-unstyled small mb-0" }, [
        el("li", {}, ["환경: ", envB]),
        el("li", {}, "엔진: " + (s.engine_enabled ? "가동" : "꺼짐(API 키 미설정)")),
        el("li", {}, "마지막 평가: " + (s.last_run || "—")),
        el("li", {}, "당일 실현손익: " + fmt(s.daily_pnl) + " 원"),
        s.loss_limit_hit
          ? el("li", { class: "text-danger fw-bold" }, "일일 손실 한도 도달 — 신규 신호 차단 중")
          : null,
      ]);
      status.body.appendChild(list);
      // --- 계좌 내역 ---
      try {
        const a = await fetchJSON("/api/trading/account");
        status.body.appendChild(el("hr", { class: "my-2" }));
        if (!a.ok) {
          status.body.appendChild(
            el("div", { class: "text-secondary small" }, "계좌 조회 불가: " + (a.error || ""))
          );
        } else {
          const plTone = a.total_pl >= 0 ? "text-danger" : "text-primary"; // 한국식: 수익 빨강
          status.body.appendChild(
            el("ul", { class: "list-unstyled small mb-1" }, [
              el("li", {}, `계좌: ${a.account_name || "—"}`),
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
      } catch (e) { /* 계좌 조회 실패는 치명적이지 않음 */ }
      if (s.watchlist && JSON.stringify(s.watchlist) !== JSON.stringify(watch)) {
        const keep = symbolSel.value;
        watch = s.watchlist;
        symbolSel.innerHTML = "";
        for (const [code, name] of Object.entries(watch)) {
          symbolSel.appendChild(el("option", { value: code }, `${name} (${code})`));
        }
        if (keep && watch[keep]) symbolSel.value = keep;
        loadChart();
      }
    };

    const loadScanner = async () => {
      let sc;
      try {
        sc = await fetchJSON("/api/trading/scanner");
      } catch (e) { return; }
      scannerC.body.innerHTML = "";
      const cfg = sc.config || {};
      scannerC.body.appendChild(el("div", { class: "text-secondary small mb-2" },
        `등락률 +${cfg.min_change_pct ?? 3}% ↑ · 거래대금 상위 교차 · ` +
        (sc.last_scan ? `마지막 스캔 ${sc.last_scan.slice(11, 19)}` : "장중에만 스캔")));
      if (!sc.results.length) {
        scannerC.body.appendChild(el("div", { class: "text-secondary small" }, "조건에 맞는 급등 종목 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>현재가</th><th>등락률</th><th>거래대금</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const r of sc.results) {
        const add = el("button", { class: "btn btn-sm btn-outline-primary" }, "감시 추가");
        add.onclick = async () => {
          add.disabled = true;
          try {
            await postJSON("/api/trading/watchlist", { code: r.code, name: r.name });
            add.textContent = "추가됨";
            loadStatus();
          } catch (e) { alert("실패: " + e.message); add.disabled = false; }
        };
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${r.name} (${r.code})`),
          el("td", {}, fmt(r.price)),
          el("td", { class: "text-danger" }, `+${r.change_pct.toFixed(1)}%`),
          el("td", {}, fmt(r.trade_value)),
          el("td", {}, add),
        ]));
      }
      tbl.appendChild(tb);
      scannerC.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadOrders = async () => {
      let orders;
      try {
        orders = await fetchJSON("/api/trading/orders?status=pending");
      } catch (e) { return; }
      pending.body.innerHTML = "";
      if (!orders.length) {
        pending.body.appendChild(el("div", { class: "text-secondary small" }, "대기 중인 주문 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>진입/손절/목표</th><th>수량</th><th>사유</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const o of orders) {
        const approve = el("button", { class: "btn btn-sm btn-success me-1" }, "승인");
        const rejectB = el("button", { class: "btn btn-sm btn-outline-danger" }, "거부");
        approve.onclick = async () => {
          if (!confirm(`[${o.symbol}] ${o.rule} ${o.side} ${o.qty}주 — 실제로 발주할까요?`)) return;
          approve.disabled = true;
          try { await postJSON(`/api/trading/orders/${o.id}/approve`); }
          catch (e) { alert("발주 실패: " + e.message); }
          loadOrders();
        };
        rejectB.onclick = async () => {
          try { await postJSON(`/api/trading/orders/${o.id}/reject`); } catch (e) {}
          loadOrders();
        };
        tb.appendChild(el("tr", {}, [
          el("td", {}, o.symbol),
          el("td", {}, o.rule),
          el("td", {}, sideBadge(o.side)),
          el("td", {}, `${fmt(o.entry)} / ${fmt(o.stop)} / ${fmt(o.target)}`),
          el("td", {}, String(o.qty)),
          el("td", { class: "small text-secondary" }, o.reason || ""),
          el("td", {}, [approve, rejectB]),
        ]));
      }
      tbl.appendChild(tb);
      pending.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    const loadSignals = async () => {
      let sigs;
      try {
        sigs = await fetchJSON("/api/trading/signals");
      } catch (e) { return; }
      signals.body.innerHTML = "";
      if (!sigs.length) {
        signals.body.appendChild(el("div", { class: "text-secondary small" }, "오늘 신호 없음"));
        return;
      }
      const tbl = el("table", { class: "table table-sm mb-0" });
      tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>규칙</th><th>방향</th><th>사유</th></tr>" }));
      const tb = el("tbody");
      for (const s of sigs.slice(0, 15)) {
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${s.name} (${s.symbol})`),
          el("td", {}, s.rule),
          el("td", {}, sideBadge(s.side)),
          el("td", { class: "small text-secondary" }, s.reason),
        ]));
      }
      tbl.appendChild(tb);
      signals.body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    await Promise.all([loadStatus(), loadOrders(), loadSignals(), loadScanner()]);
    ctx.addTimer(setInterval(() => { loadStatus(); loadOrders(); loadSignals(); loadScanner(); }, 10_000));
    ctx.addTimer(setInterval(loadChart, 5_000)); // 실시간 분봉 (WS 집계 + 형성 중 봉 포함)
  },
};
