import { fetchJSON, el, card } from "../app.js";
import { postJSON, fmt, won, pct, makeChanged } from "./tradelib.js";

// 성과·백테스트 페이지 (트레이딩 그룹): 실거래 성과 로그 + 규칙 백테스트(검증·리뷰).
// 감시목록 카드의 '백테스트' 버튼이 sessionStorage("backtest:symbol") 로
// 종목을 넘겨 이 페이지에서 자동 실행된다.

export default {
  id: "backtest",
  title: "성과·백테스트",
  icon: "bi-clipboard-data",
  group: "트레이딩",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);
    const perfC = card("실거래 성과 로그", null, { wide: true, icon: "bi-cash-coin" });
    perfC.col.className = "col-12";
    row.appendChild(perfC.col);
    const backtestC = card("규칙 백테스트 (내 데이터 검증)", null, { wide: true, icon: "bi-clipboard-data" });
    backtestC.col.className = "col-12";
    row.appendChild(backtestC.col);

    // --- 실거래 성과 로그 (매매 데스크에서 이동 — 검증·리뷰 페이지 소속) ---
    const perfBody = el("div");
    perfC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-cash-coin"></i> 승인·발주된 신호의 <b>진입가·청산가·실현손익·슬리피지</b>를 추적. 진입가는 주문체결 실시간 수신 시 <b>실측</b>으로 갱신(미수신분은 <b>근사</b>). 손절/목표 터치는 장중 30초 감시, 미청산분은 장 마감에 종가 정리.' })),
      perfBody,
    );
    const closePosition = async (id) => {
      if (!confirm("이 추적 포지션을 현재가로 청산 처리할까요? (장부상 기록 — 실제 청산 주문은 별도)")) return;
      try { await postJSON(`/api/trading/positions/${id}/close`); changed.invalidate("perf"); await loadPerformance(); }
      catch (e) { alert("실패: " + e.message); }
    };
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

    // --- 규칙 백테스트 (비용 반영, 내 데이터 검증) ---
    const btInput = el("input", { class: "form-control form-control-sm", placeholder: "종목코드 6자리", style: "max-width:150px" });
    const btTf = el("select", { class: "form-select form-select-sm", style: "max-width:110px" },
      [el("option", { value: "1m" }, "분봉"), el("option", { value: "1d" }, "일봉")]);
    const btRun = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "실행");
    const btOut = el("div", { class: "small mt-2" });
    const runBacktest = async () => {
      const code = btInput.value.trim();
      if (!/^\d{6}$/.test(code)) { btOut.innerHTML = ""; btOut.appendChild(el("div", { class: "text-danger" }, "6자리 종목코드를 입력하세요")); return; }
      if (btRun.disabled) return; // 실행 중 중복 요청 방지
      btRun.disabled = true;
      btOut.textContent = "실행 중… (봉이 많거나 스윕과 겹치면 수십 초 걸릴 수 있음)";
      try {
        const r = await fetchJSON(`/api/trading/backtest/${code}?tf=${btTf.value}`);
        btOut.innerHTML = "";
        if (!r.ok) { btOut.appendChild(el("div", { class: "text-secondary" }, r.error || "결과 없음")); return; }
        const s = r.stats || {};
        if (!s.trades) {
          btOut.appendChild(el("div", { class: "text-secondary" },
            `${r.days}일치 ${btTf.value === "1m" ? "분봉" : "일봉"} · 체결 신호 없음 (분봉은 장중 축적될수록 표본이 늘어납니다)`));
          return;
        }
        const stat = (k, v) => el("div", { class: "col-6 col-md-3" },
          el("div", { class: "border rounded p-2" }, [el("div", { class: "text-secondary" }, k), el("div", { class: "fw-semibold" }, v)]));
        btOut.append(
          el("div", { class: "text-secondary mb-1" }, `${r.name ? r.name + " (" + r.symbol + ")" : r.symbol} · ${r.days}일치 · 총 ${s.trades}건`),
          el("div", { class: "row g-2" }, [
            stat("승률", s.win_rate + "%"), stat("건당 손익(계좌)", s.avg_pnl_pct + "%"),
            stat("기대값 R", s.avg_r ?? "-"),
            stat("손익비(PF)", s.profit_factor == null ? "∞" : s.profit_factor), stat("누적수익(계좌)", s.total_return_pct + "%"),
            stat("최대낙폭(계좌)", s.max_drawdown_pct + "%"),
          ]),
          el("div", { class: "text-secondary small mt-2" }, [
            el("div", {}, "규칙별 건당 손익%(계좌): " +
              Object.entries(s.by_rule || {}).map(([k, v]) => `${k} ${v}`).join(" · ")),
            el("div", { class: "mt-1" }, `※ 계좌 기준 = 포지션 사이징(1회 리스크 ${s.risk_per_trade_pct ?? 0.5}%) 반영. 참고 주가변동률 ${s.avg_price_pct}%`),
          ]),
        );
      } catch (e) { btOut.innerHTML = ""; btOut.appendChild(el("div", { class: "text-danger" }, "실패: " + e.message)); }
      finally { btRun.disabled = false; }
    };
    btRun.onclick = runBacktest;
    btInput.addEventListener("keydown", (e) => { if (e.key === "Enter") runBacktest(); });
    backtestC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-clipboard-data"></i> 저장된 봉으로 규칙을 <b>비용(수수료·세금·슬리피지) 반영</b> 백테스트. 딥리서치 원칙 “진입기법보다 청산 설계·비용, 내 데이터로 검증”을 반영.' })),
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center" }, [btInput, btTf, btRun]),
      btOut,
    );

    // --- 감시목록 종목별 분봉 축적 일수 (백테스트 표본 크기) ---
    const covOut = el("div", { class: "small mt-2" });
    const loadCoverage = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/backtest/coverage"); } catch (e) { return; }
      if (!changed("coverage", d)) return;
      covOut.innerHTML = "";
      covOut.appendChild(el("div", { class: "text-secondary mb-1" },
        el("span", { html: '<i class="bi bi-database"></i> 감시목록 분봉 축적 일수 (3일↑부터 백테스트 표본 형성)' })));
      if (!(d.symbols || []).length) {
        covOut.appendChild(el("div", { class: "text-secondary" }, "감시목록이 비어 있습니다"));
        return;
      }
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>종목</th><th>분봉 일수</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const r of d.symbols) {
        const run = el("button", { class: "btn btn-sm btn-outline-secondary py-0" }, "백테스트");
        run.onclick = () => { btInput.value = r.code; btTf.value = "1m"; runBacktest(); };
        tb.appendChild(el("tr", {}, [
          el("td", {}, `${r.name} (${r.code})`),
          el("td", { class: r.days >= 3 ? "text-success fw-semibold" : "text-secondary" }, `${r.days}일`),
          el("td", {}, run),
        ]));
      }
      t.appendChild(tb);
      covOut.appendChild(el("div", { class: "table-responsive" }, t));
    };
    backtestC.body.appendChild(covOut);

    // --- 자동 백테스트 리포트 (분봉 축적분, 평일 장 마감 후 자동) ---
    const rptOut = el("div", { class: "small" });
    const rptRun = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "지금 실행");
    const loadBacktestReport = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/backtest/report/latest"); } catch (e) { return; }
      if (!changed("btreport", d)) return;
      rptOut.innerHTML = "";
      if (!d.run_ts) {
        rptOut.appendChild(el("div", { class: "text-secondary" },
          "아직 리포트 없음 — 분봉이 최소 3일 이상 쌓이면 평일 장 마감 후 자동 생성됩니다."));
        return;
      }
      const s = d.summary || {};
      rptOut.append(
        el("div", { class: "text-secondary mb-1" }, `최근 실행 ${d.run_ts.replace("T", " ")} · 대상 ${s.symbols || 0}종목 · 체결 ${s.trades || 0}건`),
      );
      if (s.trades) {
        rptOut.append(el("div", { class: "mb-2" }, `전체 승률 ${s.win_rate}% · 건당 손익(계좌) ${s.avg_pnl_pct}% · 규칙별 ` +
          Object.entries(s.by_rule || {}).map(([k, v]) => `${k} ${v}`).join(" · ")));
        const tbl = el("table", { class: "table table-sm align-middle mb-0 small" });
        tbl.appendChild(el("thead", { html: "<tr><th>종목</th><th>일수</th><th>체결</th><th>승률</th><th>건당손익%(계좌)</th><th>PF</th><th>누적%</th><th>MDD%</th></tr>" }));
        const tb = el("tbody");
        for (const r of d.symbols) {
          tb.appendChild(el("tr", {
            html: `<td>${r.name ? r.name + " (" + r.symbol + ")" : r.symbol}</td><td>${r.days}</td><td>${r.trades || 0}</td>` +
              `<td>${r.win_rate ?? "-"}</td><td>${r.avg_pnl_pct ?? "-"}</td><td>${r.trades ? (r.profit_factor == null ? "∞" : r.profit_factor) : "-"}</td>` +
              `<td>${r.total_return_pct ?? "-"}</td><td>${r.max_drawdown_pct ?? "-"}</td>`,
          }));
        }
        tbl.appendChild(tb);
        rptOut.appendChild(el("div", { class: "table-responsive" }, tbl));
      } else {
        rptOut.appendChild(el("div", { class: "text-secondary" }, "체결 신호 없음 (분봉 축적 진행 중 — 표본이 늘어나면 결과가 나타납니다)"));
      }
    };
    rptRun.onclick = async () => {
      rptRun.disabled = true; rptOut.textContent = "실행 중… (전 종목 백테스트, 수십 초 소요)";
      try { await postJSON("/api/trading/backtest/report/run"); changed.invalidate("btreport"); await loadBacktestReport(); }
      catch (e) { rptOut.textContent = "실패: " + e.message; }
      finally { rptRun.disabled = false; }
    };
    backtestC.body.append(
      el("hr", { class: "my-3" }),
      el("div", { class: "d-flex align-items-center gap-2 mb-2" }, [
        el("span", { class: "fw-semibold small", html: '<i class="bi bi-graph-up-arrow"></i> 자동 백테스트 리포트' }),
        el("span", { class: "text-secondary small" }, "분봉 축적분 · 평일 장 마감 후 자동"),
        rptRun,
      ]),
      rptOut,
    );

    // 감시목록 카드의 '백테스트' 버튼에서 넘어온 종목 자동 실행
    const pending = sessionStorage.getItem("backtest:symbol");
    if (pending) {
      sessionStorage.removeItem("backtest:symbol");
      btInput.value = pending;
      btTf.value = "1m";
      runBacktest();
    }

    await Promise.all([loadPerformance(), loadCoverage(), loadBacktestReport()]);
    ctx.addTimer(setInterval(() => { loadPerformance(); loadCoverage(); }, 30_000));
    ctx.addTimer(setInterval(loadBacktestReport, 300_000));
  },
};
