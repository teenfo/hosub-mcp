import { fetchJSON, el, card, badge } from "../app.js";
import { makeLayoutEditable } from "../layout.js";
import { postJSON, fmt, won, pct, makeChanged, makeTabs, sideBadge } from "./tradelib.js";

// 성과·백테스트 페이지 (트레이딩 그룹) — '실제로 번 돈' 과 '규칙이 벌었을 돈' 을
// 나란히 놓고 본다. 발굴·감시 페이지와 같은 방식으로 성격별 카드 + 탭 구성.
//
//   [성과 요약]    실거래 KPI — 지금 이 계좌가 어떤 상태인가
//        ↓
//   [포지션·체결]  보유(추적 중) / 최근 청산 — 그 숫자가 어디서 나왔나
//        ↓
//   [규칙 검증]    종목 단건 백테스트 / 전종목 자동 리포트 — 규칙이 맞나
//        ↓
//   [데이터 확보]  분봉 축적 일수 · 과거확보 — 위 검증을 믿을 만한가
//
// 감시목록 카드의 '백테스트' 버튼이 sessionStorage("backtest:symbol") 로
// 종목을 넘겨 이 페이지에서 자동 실행된다.

const EXIT_TONE = { target: "success", stop: "danger", timeout: "warning", eod: "secondary" };
const EXIT_KO = { target: "목표", stop: "손절", timeout: "시간청산", eod: "장마감", manual: "수동" };

export default {
  id: "backtest",
  title: "성과·백테스트",
  icon: "bi-clipboard-data",
  group: "트레이딩",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const summaryC = card("성과 요약 (실거래)", null, { wide: true, icon: "bi-cash-coin" });
    const posC = card("포지션 · 체결 내역", null, { wide: true, icon: "bi-list-check" });
    const verifyC = card("규칙 검증 (백테스트)", null, { wide: true, icon: "bi-clipboard-data" });
    const studyC = card("발굴 점수 검증 (이벤트 스터디)", null, { wide: true, icon: "bi-bar-chart-steps" });
    const rankC = card("랭킹 방식 비교 (브래킷 근사)", null, { wide: true, icon: "bi-sort-down" });
    const regimeC = card("국면 판정 검증 (야간 리포트 vs 대조군)", null,
                         { wide: true, icon: "bi-cloud-moon" });
    const newsC = card("뉴스 영향 검증 (LLM 판정 vs 실제 수익률)", null,
                       { wide: true, icon: "bi-newspaper" });
    const dataC = card("데이터 확보 현황", null, { wide: true, icon: "bi-database" });
    const CARDS = [["summary", summaryC], ["positions", posC], ["verify", verifyC],
                   ["study", studyC], ["ranking", rankC], ["regime", regimeC],
                   ["news", newsC], ["data", dataC]];
    CARDS.forEach(([id, c], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;
      c.col.className = "col-12 col-xl-12";
      row.appendChild(c.col);
    });
    makeLayoutEditable(row, { key: "backtest" });

    const emptyRow = (msg) => el("div", { class: "text-secondary small py-2" }, msg);
    const tableOf = (head, rows) => {
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: `<tr>${head}</tr>` }));
      const tb = el("tbody");
      rows.forEach((r) => tb.appendChild(r));
      t.appendChild(tb);
      return el("div", { class: "table-responsive" }, t);
    };
    /** KPI 타일 — 라벨·값·(선택) 보조 설명. */
    const stat = (k, v, cls, hint) => el("div", { class: "col-6 col-md-4 col-xl-2" },
      el("div", { class: "border rounded p-2 h-100" }, [
        el("div", { class: "text-secondary small" }, k),
        el("div", { class: "fw-semibold " + (cls || "") }, v),
        hint ? el("div", { class: "text-secondary", style: "font-size:.72rem" }, hint) : null,
      ]));

    // ==================================================================
    // ① 성과 요약 — 실거래 KPI + 규칙별 기대값
    // ==================================================================
    const sumBody = el("div", {}, emptyRow("불러오는 중…"));
    summaryC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-cash-coin"></i> 승인·발주된 신호의 <b>실현손익</b> 집계. 진입가는 주문체결 실시간 수신 시 <b>실측</b>으로 갱신되고, 미수신분은 <b>근사</b>로 표시된다.' })),
      sumBody,
    );

    // ==================================================================
    // ② 포지션 · 체결 내역 — 보유 / 최근 청산
    // ==================================================================
    const posTabs = makeTabs([
      { id: "open", label: "보유 (추적 중)", icon: "hourglass-split",
        note: "손절/목표 터치는 장중 30초 감시, 미청산분은 장 마감에 종가 정리된다." },
      { id: "closed", label: "최근 청산", icon: "check2-circle",
        note: "청산 사유별 성적이 규칙 보완의 1차 단서다 — 시간청산이 많으면 보유 상한이, 손절이 몰리면 손절폭이 의심 대상." },
    ]);
    posTabs.mount(posC.body);
    posTabs.set("open", emptyRow("불러오는 중…"));
    posTabs.set("closed", emptyRow("불러오는 중…"));

    const closePosition = async (id) => {
      if (!confirm("이 추적 포지션을 현재가로 청산 처리할까요? (장부상 기록 — 실제 청산 주문은 별도)")) return;
      try { await postJSON(`/api/trading/positions/${id}/close`); changed.invalidate("perf"); await loadPerformance(); }
      catch (e) { alert("실패: " + e.message); }
    };

    const loadPerformance = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/performance"); } catch (e) { return; }
      if (!changed("perf", d)) return;
      const o = (d.stats && d.stats.overall) || {};
      const opens = d.open || [];
      const closed = d.closed || [];

      // --- 요약 ---
      sumBody.innerHTML = "";
      if (!o.trades && !opens.length) {
        sumBody.appendChild(emptyRow(
          "아직 기록 없음 — 승인·발주된 주문이 생기면 여기에 진입/청산/손익이 쌓입니다."));
      } else if (!o.trades) {
        sumBody.appendChild(emptyRow(
          `청산 완료 건이 아직 없습니다 — 보유 ${opens.length}건이 청산되면 집계가 시작됩니다.`));
      } else {
        const tone = o.total_pnl_krw >= 0 ? "text-danger" : "text-primary"; // 한국식: 이익 빨강
        sumBody.appendChild(el("div", { class: "row g-2" }, [
          stat("실현손익", won(o.total_pnl_krw), tone, `청산 ${o.trades}건`),
          stat("승률", o.win_rate + "%", null, "이긴 비율"),
          stat("건당 기대값", pct(o.expectancy_pct), null, "1회 평균 손익"),
          stat("손익비(PF)", o.profit_factor == null ? "∞" : o.profit_factor, null, "총이익÷총손실"),
          stat("평균 슬리피지", o.avg_slippage_pct + "%", null, "주문가 대비 체결"),
          stat("보유 중", opens.length + "건", null, "청산 대기"),
        ]));
        const byRule = d.stats.by_rule || {};
        if (Object.keys(byRule).length) {
          sumBody.appendChild(el("div", { class: "small mt-2" }, [
            el("span", { class: "text-secondary me-1" }, "규칙별 기대값"),
            ...Object.entries(byRule).map(([k, v]) => el("span", {
              class: "badge me-1 " + (v.expectancy_pct >= 0 ? "text-bg-danger" : "text-bg-primary"),
              title: `${v.trades}건 · 승률 ${v.win_rate}%`,
            }, `${k} ${pct(v.expectancy_pct)} (${v.trades}건)`)),
          ]));
        }
      }

      // --- 보유 ---
      posTabs.set("open", opens.length ? tableOf(
        "<th>종목</th><th>규칙</th><th>방향</th><th class='text-end'>진입</th><th>체결</th>" +
        "<th class='text-end'>손절</th><th class='text-end'>목표</th><th></th>",
        opens.map((p) => {
          const b = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "청산");
          b.onclick = () => closePosition(p.id);
          return el("tr", {}, [
            el("td", {}, `${p.name || p.symbol}`),
            el("td", {}, p.rule),
            el("td", {}, sideBadge(p.side)),
            el("td", { class: "text-end" }, fmt(p.entry)),
            el("td", {}, badge(p.fill_confirmed ? "실측" : "근사",
                               p.fill_confirmed ? "success" : "secondary")),
            el("td", { class: "text-end" }, fmt(p.stop)),
            el("td", { class: "text-end" }, fmt(p.target)),
            el("td", {}, b),
          ]);
        })) : emptyRow("보유 포지션 없음"), opens.length);

      // --- 청산 ---
      posTabs.set("closed", closed.length ? tableOf(
        "<th>종목</th><th>규칙</th><th>방향</th><th class='text-end'>진입 → 청산</th>" +
        "<th>체결</th><th>사유</th><th class='text-end'>손익%</th><th class='text-end'>손익</th>",
        closed.map((p) => {
          const cls = p.pnl_pct >= 0 ? "text-danger" : "text-primary";
          return el("tr", {}, [
            el("td", {}, `${p.name || p.symbol}`),
            el("td", {}, p.rule),
            el("td", {}, sideBadge(p.side)),
            el("td", { class: "text-end text-nowrap" }, `${fmt(p.entry)} → ${fmt(p.exit)}`),
            el("td", { class: "text-secondary" }, p.fill_confirmed ? "실측" : "근사"),
            el("td", {}, badge(EXIT_KO[p.exit_reason] || p.exit_reason,
                               EXIT_TONE[p.exit_reason] || "secondary")),
            el("td", { class: "text-end " + cls }, pct(p.pnl_pct)),
            el("td", { class: "text-end " + cls }, won(p.pnl_krw)),
          ]);
        })) : emptyRow("청산 기록 없음"), closed.length);
    };

    // ==================================================================
    // ③ 규칙 검증 — 종목 단건 / 전종목 자동 리포트
    // ==================================================================
    const vfTabs = makeTabs([
      { id: "single", label: "종목 백테스트", icon: "search",
        note: '저장된 봉으로 규칙을 <b>비용(수수료·세금·슬리피지) 반영</b> 재생. 딥리서치 원칙 “진입기법보다 청산 설계·비용, 내 데이터로 검증”을 반영.' },
      { id: "report", label: "전종목 자동 리포트", icon: "graph-up-arrow",
        note: "분봉 축적분 전체를 평일 장 마감 후 자동 재생. 한 종목의 우연을 걸러내는 교차 확인용." },
    ]);
    vfTabs.mount(verifyC.body);

    const btInput = el("input", { class: "form-control form-control-sm", placeholder: "종목코드 6자리", style: "max-width:150px" });
    const btTf = el("select", { class: "form-select form-select-sm", style: "max-width:110px" },
      [el("option", { value: "1m" }, "분봉"), el("option", { value: "1d" }, "일봉")]);
    const btRun = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "실행");
    const btOut = el("div", { class: "small mt-2" });
    vfTabs.set("single", el("div", {}, [
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center" }, [btInput, btTf, btRun]),
      btOut,
    ]));

    const runBacktest = async () => {
      const code = btInput.value.trim();
      vfTabs.show("single");
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
        btOut.append(
          el("div", { class: "text-secondary mb-1" }, `${r.name ? r.name + " (" + r.symbol + ")" : r.symbol} · ${r.days}일치 · 총 ${s.trades}건`),
          el("div", { class: "row g-2" }, [
            stat("승률", s.win_rate + "%"),
            stat("건당 손익(계좌)", s.avg_pnl_pct + "%"),
            stat("기대값 R", s.avg_r ?? "-"),
            stat("손익비(PF)", s.profit_factor == null ? "∞" : s.profit_factor),
            stat("누적수익(계좌)", s.total_return_pct + "%"),
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

    const rptOut = el("div", { class: "small" });
    const rptRun = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "지금 실행");
    vfTabs.set("report", el("div", {}, [
      el("div", { class: "mb-2" }, rptRun),
      rptOut,
    ]));
    const loadBacktestReport = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/backtest/report/latest"); } catch (e) { return; }
      if (!changed("btreport", d)) return;
      rptOut.innerHTML = "";
      if (!d.run_ts) {
        rptOut.appendChild(emptyRow(
          "아직 리포트 없음 — 분봉이 최소 3일 이상 쌓이면 평일 장 마감 후 자동 생성됩니다."));
        return;
      }
      const s = d.summary || {};
      rptOut.appendChild(el("div", { class: "text-secondary mb-1" },
        `최근 실행 ${d.run_ts.replace("T", " ")} · 대상 ${s.symbols || 0}종목 · 체결 ${s.trades || 0}건`));
      if (!s.trades) {
        rptOut.appendChild(emptyRow("체결 신호 없음 (분봉 축적 진행 중 — 표본이 늘어나면 결과가 나타납니다)"));
        return;
      }
      rptOut.appendChild(el("div", { class: "mb-2" }, `전체 승률 ${s.win_rate}% · 건당 손익(계좌) ${s.avg_pnl_pct}% · 규칙별 ` +
        Object.entries(s.by_rule || {}).map(([k, v]) => `${k} ${v}`).join(" · ")));
      rptOut.appendChild(tableOf(
        "<th>종목</th><th class='text-end'>일수</th><th class='text-end'>체결</th>" +
        "<th class='text-end'>승률</th><th class='text-end'>건당손익%(계좌)</th>" +
        "<th class='text-end'>PF</th><th class='text-end'>누적%</th><th class='text-end'>MDD%</th>",
        d.symbols.map((r) => el("tr", {
          html: `<td>${r.name ? r.name + " (" + r.symbol + ")" : r.symbol}</td>` +
            `<td class="text-end">${r.days}</td><td class="text-end">${r.trades || 0}</td>` +
            `<td class="text-end">${r.win_rate ?? "-"}</td><td class="text-end">${r.avg_pnl_pct ?? "-"}</td>` +
            `<td class="text-end">${r.trades ? (r.profit_factor == null ? "∞" : r.profit_factor) : "-"}</td>` +
            `<td class="text-end">${r.total_return_pct ?? "-"}</td><td class="text-end">${r.max_drawdown_pct ?? "-"}</td>`,
        }))));
    };
    rptRun.onclick = async () => {
      rptRun.disabled = true; rptOut.textContent = "실행 중… (전 종목 백테스트, 수십 초 소요)";
      try { await postJSON("/api/trading/backtest/report/run"); changed.invalidate("btreport"); await loadBacktestReport(); }
      catch (e) { rptOut.textContent = "실패: " + e.message; }
      finally { rptRun.disabled = false; }
    };

    // ==================================================================
    // ④ 발굴 점수 검증 (이벤트 스터디)
    // 야간 발굴 3규칙 점수가 실제로 익일 수익률과 상관이 있는가. 머신러닝을
    // 얹기 전에 답해야 할 질문이다 — 상관이 없으면 어떤 모델도 소용없다.
    // ==================================================================
    const esRun = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "지금 실행");
    const esScope = el("select", { class: "form-select form-select-sm", style: "max-width:190px" }, [
      el("option", { value: "liquid" }, "매수 가능 종목만"),
      el("option", { value: "all" }, "전종목"),
    ]);
    // 목적어 — 발굴이 '방향'을 공급해야 하는지 '움직임'을 공급해야 하는지가 갈린다.
    // orb·momentum 은 장중 규칙이라 익일 종가 방향이 아니라 변동폭이 필요하다.
    const esTarget = el("select", { class: "form-select form-select-sm", style: "max-width:230px" });
    const esBody = el("div", {}, emptyRow("불러오는 중…"));
    studyC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-bar-chart-steps"></i> 발굴 3규칙 점수(0~3)와 <b>익일 수익률</b>의 관계. 진입 기준은 <b>익일 시가</b> — 발굴 배치가 17:30에 도니 당일 종가에는 못 산다. <b>초과수익</b>은 같은 날 전 종목 평균을 뺀 값이라 시장이 오른 날의 덕을 규칙 성적으로 세지 않는다.' })),
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [esScope, esTarget, esRun]),
      esBody,
    );

    let esData = null;
    const renderStudy = () => {
      const d = esData;
      esBody.innerHTML = "";
      if (!d || d.ok === false || !d.run_ts) {
        esBody.appendChild(emptyRow(
          (d && d.error) || "아직 실행되지 않았습니다 — '지금 실행'을 누르세요 (전종목 × 전 구간, 수 분 소요)"));
        return;
      }
      const liquid = esScope.value === "liquid";
      const buckets = (liquid ? d.buckets_liquid : d.buckets) || [];
      // 목적어 드롭다운 채우기 — 서버가 내려준 목록에서 생성한다
      const targets = d.targets || { fwd_1: "익일 방향" };
      if (esTarget.options.length !== Object.keys(targets).length) {
        esTarget.innerHTML = "";
        for (const [k, label] of Object.entries(targets)) {
          esTarget.appendChild(el("option", { value: k }, `목적어: ${label}`));
        }
      }
      const tgt = esTarget.value || "fwd_1";
      // 목적어별 IC 는 매수 가능 표본 기준으로만 계산한다(판단에 쓰이는 쪽)
      const ic = (tgt === "fwd_1" && !liquid)
        ? (d.ic || [])
        : ((d.ic_by_target || {})[tgt] || (liquid ? d.ic_liquid : d.ic) || []);
      const bonf = d.bonferroni_t || 2;
      esBody.appendChild(el("div", { class: "small text-secondary mb-2" },
        `${d.date_from} ~ ${d.date_to} · ${d.days}거래일 · ${d.symbols}종목 · ` +
        `표본 ${(liquid ? d.liquid_rows : d.rows).toLocaleString()}건 · ` +
        `왕복 비용 ${d.cost_pct}% 반영 · 실행 ${String(d.run_ts).replace("T", " ").slice(0, 16)}`));
      if (!buckets.length) {
        esBody.appendChild(emptyRow("표본 없음"));
        return;
      }
      // 점수별 성적
      esBody.appendChild(el("div", { class: "fw-semibold small mb-1" }, "점수별 익일 성적"));
      esBody.appendChild(tableOf(
        "<th>점수</th><th class='text-end'>표본</th><th class='text-end'>익일 수익률</th>" +
        "<th class='text-end'>초과수익</th><th class='text-end'>비용 반영</th>" +
        "<th class='text-end'>승률</th><th class='text-end'>t값</th>" +
        "<th class='text-end'>못 먹는 갭</th>",
        buckets.map((b) => {
          const tone = (v) => (v > 0 ? "text-danger" : v < 0 ? "text-primary" : "");
          // |t| > 2 라야 우연과 구분된다. 그 아래는 흐리게 — 눈이 속지 않게.
          const solid = b.reliable && Math.abs(b.t_stat) > 2;
          return el("tr", { class: b.reliable ? "" : "opacity-50" }, [
            el("td", { class: "fw-semibold" }, `${b.score}점`),
            el("td", { class: "text-end" }, b.n.toLocaleString()),
            el("td", { class: "text-end " + tone(b.mean_pct) }, `${b.mean_pct}%`),
            el("td", { class: "text-end fw-semibold " + tone(b.excess_pct) }, `${b.excess_pct}%`),
            el("td", { class: "text-end " + tone(b.net_pct) }, `${b.net_pct}%`),
            el("td", { class: "text-end" }, `${b.win_rate}%`),
            el("td", { class: "text-end " + (solid ? "fw-semibold" : "text-secondary"),
                       title: solid ? "우연으로 보기 어려움" : "우연과 구분되지 않음" },
               b.reliable ? b.t_stat : "표본부족"),
            el("td", { class: "text-end text-secondary" }, `${b.gap_pct}%`),
          ]);
        })));
      // IC
      if (ic.length) {
        esBody.appendChild(el("div", { class: "fw-semibold small mt-3 mb-1" },
          `피처별 예측력 — 목적어: ${targets[tgt] || tgt}`));
        esBody.appendChild(el("div", { class: "small text-secondary mb-1", html:
          "평균 IC 0.02~0.05면 실전에서 쓸 만한 수준으로 본다. " +
          `피처를 ${ic.length}개 훑으므로 우연히 넘는 것을 걸러내려면 <b>|t| > ${bonf}</b>(본페로니)를 요구한다.<br>` +
          `<b>잔차 IC</b>는 변동성(<code>${d.beta_control || "atr_pct"}</code>) 순위로 설명되는 부분을 뺀 값이다. ` +
          "날짜별 순위상관은 그날 시장 <i>수준</i>만 제거할 뿐, 베타는 종목의 <i>성질</i>이라 그대로 남는다 — " +
          "<b>잔차 IC가 살아남아야 알파다.</b>" }));
        esBody.appendChild(tableOf(
          "<th>피처</th><th class='text-end'>평균 IC</th><th class='text-end'>t값</th>" +
          "<th class='text-end'>잔차 IC</th><th class='text-end'>잔차 t</th>" +
          "<th class='text-end'>양수 비율</th><th class='text-end'>일수</th>",
          ic.map((r) => {
            const tone = (v) => (v > 0 ? "text-danger" : v < 0 ? "text-primary" : "");
            const survives = r.resid_t != null && Math.abs(r.resid_t) > bonf;
            return el("tr", { class: Math.abs(r.t_stat) > bonf ? "" : "opacity-50" }, [
              el("td", {}, [r.feature, r.is_control
                ? el("span", { class: "badge text-bg-secondary ms-1",
                               title: "잔차화의 대조축 — 자기 자신에 회귀시킬 수 없다" }, "대조축")
                : null]),
              el("td", { class: "text-end " + tone(r.mean_ic) }, r.mean_ic.toFixed(4)),
              el("td", { class: "text-end" }, r.t_stat),
              el("td", { class: "text-end " + (survives ? "fw-semibold " : "") + tone(r.resid_ic),
                         title: r.is_control ? "대조축이라 계산하지 않음" : "" },
                 r.resid_ic == null ? "—" : r.resid_ic.toFixed(4)),
              el("td", { class: "text-end " + (survives ? "fw-semibold" : "text-secondary") },
                 r.resid_t == null ? "—" : r.resid_t),
              el("td", { class: "text-end" }, `${r.hit_rate}%`),
              el("td", { class: "text-end text-secondary" }, r.days),
            ]);
          })));
        // 판정 — 이 숫자가 관제센터 가중치 착수 여부를 정한다
        const survivors = ic.filter((r) => r.resid_t != null && Math.abs(r.resid_t) > bonf);
        esBody.appendChild(el("div", { class: "small mt-2" }, [
          el("span", { class: "badge me-2 " + (survivors.length >= 2 ? "text-bg-success" : "text-bg-secondary") },
             `잔차 생존 ${survivors.length}개`),
          el("span", { class: "text-secondary" }, survivors.length >= 2
            ? `${survivors.map((r) => r.feature).join(", ")} — 변동성으로 설명되지 않는 예측력이 남았다. 관제센터 가중치 설계의 근거가 된다.`
            : "변동성 잔차에서 살아남은 피처가 2개 미만이다 — 발굴 랭킹의 근거가 약하다. 유동성·체결가능성 게이트만 남기는 편이 정직하다."),
        ]));
      }
      // 국면 분리 — 알파인가 저베타 효과인가
      const icRow = (sec, label) => {
        const rows = (sec[label] || {}).ic || [];
        return Object.fromEntries(rows.map((r) => [r.feature, r]));
      };
      const regimeBlock = (title, note, sec) => {
        const labels = Object.keys(sec || {});
        if (labels.length < 2) return null;
        const feats = [...new Set(labels.flatMap((L) => (sec[L].ic || []).map((r) => r.feature)))];
        const byLabel = Object.fromEntries(labels.map((L) => [L, icRow(sec, L)]));
        return el("div", { class: "mt-3" }, [
          el("div", { class: "fw-semibold small mb-1" }, title),
          el("div", { class: "small text-secondary mb-1", html: note }),
          tableOf(
            `<th>피처</th>${labels.map((L) => `<th class='text-end'>${L}<br><span class='fw-normal text-secondary'>${sec[L].days}일 · 시장 ${sec[L].market_pct}%</span></th>`).join("")}<th>판정</th>`,
            feats.map((f) => {
              const vals = labels.map((L) => (byLabel[L][f] || {}).mean_ic);
              const solid = labels.every((L) => Math.abs(((byLabel[L][f] || {}).t_stat) || 0) > 2);
              const signs = vals.filter((v) => v != null).map((v) => Math.sign(v));
              const flipped = new Set(signs).size > 1;
              return el("tr", { class: solid ? "" : "opacity-50" }, [
                el("td", {}, f),
                ...vals.map((v) => el("td", { class: "text-end " + (v > 0 ? "text-danger" : v < 0 ? "text-primary" : "") },
                  v == null ? "—" : v.toFixed(4))),
                el("td", {}, !solid ? el("span", { class: "text-secondary small" }, "표본 약함")
                  : flipped ? badge("부호 뒤집힘", "warning") : badge("부호 유지", "success")),
              ]);
            })),
        ]);
      };
      const trend = regimeBlock(
        "국면별 재측정 — 사전에 알 수 있는 추세",
        "직전 20일 시장 누적 등락의 부호로 나눈다. <b>그날 이미 알 수 있는 정보</b>라 실전 규칙으로 쓸 수 있다.",
        d.by_trend);
      const fwd = regimeBlock(
        "알파 / 저베타 판별 — 익일 시장 방향으로 분해",
        "<b>사전에 알 수 없으므로 매매에 쓸 수 없다.</b> 목적은 하나 — 어떤 피처의 예측력이 진짜인지 가리는 것. " +
        "저변동성이 그냥 시장을 덜 따라간 것뿐이라면 <b>오르는 날엔 부호가 뒤집힌다</b>. 양쪽 다 같은 부호면 국면과 무관한 신호다.",
        d.by_fwd_dir);
      if (trend) esBody.appendChild(trend);
      if (fwd) esBody.appendChild(fwd);
      // 한계 — 숨기지 않는다
      esBody.appendChild(el("ul", { class: "small text-secondary mt-3 mb-0 ps-3" },
        (d.caveats || []).map((c) => el("li", {}, c))));
    };
    esScope.onchange = renderStudy;
    esTarget.onchange = renderStudy;
    const loadStudy = async () => {
      try { esData = await fetchJSON("/api/trading/research/event-study"); }
      catch (e) { return; }
      if (!changed("study", esData)) return;
      renderStudy();
    };
    esRun.onclick = async () => {
      esRun.disabled = true;
      esBody.innerHTML = "";
      esBody.appendChild(emptyRow("실행 중… 전종목 × 전 구간 재현이라 수 분 걸립니다 (별도 프로세스 — 매매는 계속 돕니다)"));
      try {
        esData = await postJSON("/api/trading/research/event-study/run");
        changed.invalidate("study");
        renderStudy();
      } catch (e) {
        esBody.innerHTML = "";
        esBody.appendChild(el("div", { class: "text-danger small" }, "실패: " + e.message));
      } finally { esRun.disabled = false; }
    };

    // ==================================================================
    // ⑤ 랭킹 방식 비교 — "어떤 순서로 줄을 세워야 실제 R 이 나오는가"
    // 이벤트 스터디는 순위가 맞는지(IC)만 잰다. IC 가 좋아도 손절·목표·비용을
    // 통과하면 R 이 안 나올 수 있다. 여기서는 방식별 실현 R 을 근사해 비교한다.
    // ==================================================================
    const METHOD_KO = {
      score: ["발굴 점수 (현행)", "야간 발굴 3규칙 점수 0~3"],
      atr: ["변동성 단독", "atr_pct — 최대상승 기준 최강 원시 피처"],
      composite_is: ["잔차 IC 합성 (전 구간 학습)", "in-sample 상한 — 이보다 잘 나올 수 없다"],
      composite_wf: ["잔차 IC 합성 (walk-forward)", "직전 학습창의 잔차 IC 로만 가중 — 진짜 성적"],
      score_gate_only: ["점수 게이트 통과분 무작위", "유동성 + 발굴 점수 이상 중 무작위 — 게이트 자체의 값"],
      liquid_only: ["유동성 통과분 무작위", "'랭킹 폐기, 게이트만' 안"],
      random: ["전종목 무작위 (대조군)", "이걸 못 이기면 랭킹의 존재 이유가 없다"],
    };
    const rkRun = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "지금 실행");
    const rkBody = el("div", {}, emptyRow("불러오는 중…"));
    rankC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-sort-down"></i> 방식별로 하루 상위 N 종목을 뽑아 <b>손절·목표 브래킷</b>을 일봉 고가·저가로 근사한 실현 R. 진짜 백테스트가 아니다 — 분봉은 일부 종목뿐이고 발굴 유니버스는 전종목이라 근사가 유일한 길이다. <b>절대 R 값이 아니라 방식 간 격차</b>를 읽는 표다.' })),
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [rkRun]),
      rkBody,
    );

    let rkData = null;
    const renderRanking = () => {
      const d = rkData;
      rkBody.innerHTML = "";
      if (!d || d.ok === false || !d.run_ts) {
        rkBody.appendChild(emptyRow(
          (d && d.error) || "아직 실행되지 않았습니다 — '지금 실행'을 누르세요 (방식 6종 × 손절폭, 수 분 소요)"));
        return;
      }
      const rows = d.rows || [];
      const stops = d.stops || [];
      rkBody.appendChild(el("div", { class: "small text-secondary mb-2" },
        `${(d.rows_used || 0).toLocaleString()}행 · ${d.days}거래일 · ${d.symbols}종목 · ` +
        `하루 상위 ${d.top_n}종목 · 목표 ${d.target_r}R · 왕복 비용 ${d.cost_pct}% · ` +
        `학습창 ${d.train_days}거래일 · 실행 ${String(d.run_ts).replace("T", " ").slice(0, 16)}`));
      const at = (m, s) => rows.find((r) => r.method === m && r.stop_pct === s);
      const tone = (v) => (v > 0 ? "text-danger" : v < 0 ? "text-primary" : "");
      rkBody.appendChild(tableOf(
        "<th>랭킹 방식</th>" +
        stops.map((s) => `<th class='text-end'>손절 ${s}%</th>`).join("") +
        "<th class='text-end'>대조군 대비</th>",
        (d.methods || []).map((m) => {
          const [label, note] = METHOD_KO[m] || [m, ""];
          const cells = stops.map((s) => {
            const r = at(m, s);
            if (!r || !r.reliable) {
              return el("td", { class: "text-end text-secondary", title: "표본 부족 — 수치를 믿지 않는다" }, "—");
            }
            const solid = Math.abs(r.t_stat) > 2;
            return el("td", {
              class: "text-end " + (solid ? "fw-semibold " : "") + tone(r.avg_r),
              title: `${r.trades}건 / ${r.days}일 · 승률 ${r.win_rate}% · t=${r.t_stat} · 누적 ${r.total_r}R`,
            }, r.avg_r.toFixed(3));
          });
          // 대조군 대비는 각 방식의 최고 손절폭 기준으로 하나만 보여준다
          const best = rows.filter((r) => r.method === m && r.reliable)
            .sort((a, b) => b.avg_r - a.avg_r)[0];
          const vs = best && best.vs_random;
          return el("tr", { class: m === "random" ? "table-light" : "" }, [
            el("td", {}, [el("div", {}, label),
                          el("div", { class: "text-secondary", style: "font-size:.72rem" }, note)]),
            ...cells,
            el("td", { class: "text-end " + tone(vs) },
               vs == null ? "—" : (vs > 0 ? "+" : "") + vs.toFixed(3) + "R"),
          ]);
        })));
      // --- 판정 — 실행 전에 못박은 규칙을 그대로 적용한다 ---
      const bestOf = (m) => rows.filter((r) => r.method === m && r.reliable)
        .sort((a, b) => b.avg_r - a.avg_r)[0];
      const wf = bestOf("composite_wf");
      const isw = bestOf("composite_is");
      const sc = bestOf("score");
      const lq = bestOf("liquid_only");
      const beats = (r) => r && r.vs_random != null && r.vs_random > 0;
      const lines = [];
      const anyBeats = (d.methods || []).filter((m) => m !== "random" && beats(bestOf(m)));
      if (!anyBeats.length) {
        lines.push(["danger", "어느 방식도 무작위 대조군을 못 이겼다 — 발굴 랭킹을 폐기하고 유동성·체결가능성 게이트만 남기는 것이 정직한 결론이다(판정 규칙 3)."]);
      } else {
        lines.push(["secondary", `대조군을 이긴 방식: ${anyBeats.map((m) => (METHOD_KO[m] || [m])[0]).join(", ")}`]);
      }
      if (wf && sc) {
        lines.push(wf.avg_r > sc.avg_r
          ? ["success", `walk-forward 합성 ${wf.avg_r.toFixed(3)}R > 현행 점수 ${sc.avg_r.toFixed(3)}R — 랭킹 교체 근거가 된다(판정 규칙 2).`]
          : ["secondary", `walk-forward 합성 ${wf.avg_r.toFixed(3)}R ≤ 현행 점수 ${sc.avg_r.toFixed(3)}R — 랭킹을 바꿀 근거가 없다.`]);
      }
      if (wf && isw) {
        const gap = isw.avg_r - wf.avg_r;
        lines.push([gap > Math.abs(wf.avg_r) ? "warning" : "secondary",
          `과적합 크기 = in-sample ${isw.avg_r.toFixed(3)}R − walk-forward ${wf.avg_r.toFixed(3)}R = ${gap.toFixed(3)}R. ` +
          "격차가 크면 위 교체 결론을 채택하지 않는다(판정 규칙 4)."]);
      }
      if (lq && beats(lq) && (!wf || lq.avg_r >= wf.avg_r)) {
        lines.push(["warning", "유동성 통과분 무작위가 어떤 랭킹보다 낫다 — 게이트가 값을 내고 순서는 안 낸다는 뜻이다."]);
      }
      // 점수 게이트 자체의 값 — 순서가 아니라 '풀을 좁히는 것'에 값이 있는가
      const gate = bestOf("score_gate_only");
      if (gate && lq) {
        const gap = gate.avg_r - lq.avg_r;
        lines.push([gap > 0 ? "success" : "secondary",
          `발굴 점수 ${d.min_score ?? 2}점 게이트: 통과분 무작위 ${gate.avg_r.toFixed(3)}R vs ` +
          `유동성만 ${lq.avg_r.toFixed(3)}R = ${gap > 0 ? "+" : ""}${gap.toFixed(3)}R. ` +
          (gap > 0
            ? "점수는 줄 세우기에는 못 써도 풀을 좁히는 데에는 값이 있다 — 게이트로 남긴다."
            : "점수 게이트가 풀을 좁혀도 나아지지 않는다 — 게이트도 버린다.")]);
      }
      if (d.best) {
        const [bl] = METHOD_KO[d.best.method] || [d.best.method];
        lines.push(["secondary",
          `최고 조합: ${bl} × 손절 ${d.best.stop_pct}% → ${d.best.avg_r}R (${d.best.trades}건, t=${d.best.t_stat}). ` +
          `가중치 학습 목적어는 ${d.learn_target} 였다 — 이 조합이 목적어 확정의 근거다(판정 규칙 5).`]);
      }
      rkBody.appendChild(el("div", { class: "small mt-3" }, [
        el("div", { class: "fw-semibold mb-1" }, "판정 (실행 전에 못박은 규칙)"),
        ...lines.map(([t, txt]) => el("div", { class: "mb-1" }, [
          el("span", { class: `badge me-2 text-bg-${t}` }, "·"),
          el("span", { class: t === "secondary" ? "text-secondary" : "" }, txt),
        ])),
      ]));
      rkBody.appendChild(el("ul", { class: "small text-secondary mt-3 mb-0 ps-3" },
        (d.caveats || []).map((c) => el("li", {}, c))));
    };
    const loadRanking = async () => {
      try { rkData = await fetchJSON("/api/trading/research/ranking"); }
      catch (e) { return; }
      if (!changed("ranking", rkData)) return;
      renderRanking();
    };
    rkRun.onclick = async () => {
      rkRun.disabled = true;
      rkBody.innerHTML = "";
      rkBody.appendChild(emptyRow("실행 중… 방식 6종 × 손절폭 재현이라 수 분 걸립니다 (별도 프로세스 — 매매는 계속 돕니다)"));
      try {
        rkData = await postJSON("/api/trading/research/ranking/run");
        changed.invalidate("ranking");
        renderRanking();
      } catch (e) {
        rkBody.innerHTML = "";
        rkBody.appendChild(el("div", { class: "text-danger small" }, "실패: " + e.message));
      } finally { rkRun.disabled = false; }
    };

    // ==================================================================
    // ⑥ 국면 판정 검증 — 야간 리포트가 결정론 대조군을 이기는가
    // 야간 리포트는 실제로 매매에 관여한다(인버스 ETF 매수 차단). 그런데
    // 예측을 남기지 않아 적중률을 잰 적이 없다. 오늘부터 쌓는다.
    // ==================================================================
    const SIGNAL_KO = {
      night_bias: ["야간 리포트", "외부 미국장 분석 — 검증 대상"],
      base_regime: ["전일 breadth", "60일선 상회 비율 — 결정론 대조군"],
      gap_bias: ["시가갭 중앙값", "감시목록 갭 — 결정론 대조군"],
      effective: ["합성 (실제 적용)", "위 셋을 합친 값 = 매매에 쓰인 국면"],
    };
    const REGIME_TONE = { 강세: "text-danger", 약세: "text-primary", 중립: "text-secondary" };
    const rgBody = el("div", {}, emptyRow("불러오는 중…"));
    regimeC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-cloud-moon"></i> 야간 리포트는 <b>실제로 매매에 관여한다</b> — 인버스 ETF 매수를 막는 기준선이다. 그런데 예측이 파일 1개에 덮어쓰기로만 남아 적중률을 잰 적이 없다. 이제 <b>결정론 대조군 2개와 같은 레코드</b>로 쌓는다. "맞았다"는 대조군을 이겨야 의미가 있다 — 상승일이 많으면 늘 강세라고만 해도 맞는다.' })),
      rgBody,
    );

    const renderRegime = (d) => {
      rgBody.innerHTML = "";
      const daily = d.daily || [];
      const sc = d.score || {};
      if (!daily.length) {
        rgBody.appendChild(emptyRow(
          "아직 이력이 없습니다 — 장중 엔진이 국면을 평가하면 오늘부터 쌓이기 시작합니다."));
        return;
      }
      const scored = Object.values(sc).filter((v) => v.calls > 0);
      rgBody.appendChild(el("div", { class: "small text-secondary mb-2" },
        `${daily[0].date} ~ ${daily[daily.length - 1].date} · ${daily.length}거래일 · ` +
        `판정 가능 기준 ${d.min_days}일` +
        (scored.some((v) => v.reliable) ? "" : " — 아직 표본이 모자랍니다")));
      // 신호별 적중률
      rgBody.appendChild(tableOf(
        "<th>신호</th><th class='text-end'>방향 건 날</th><th class='text-end'>중립</th>" +
        "<th class='text-end'>적중률</th><th>판정</th>",
        (d.signals || []).map((k) => {
          const v = sc[k] || {};
          const [label, note] = SIGNAL_KO[k] || [k, ""];
          const night = sc.night_bias || {};
          // 야간 리포트가 이 대조군을 이겼는가 — 판정의 실체
          const beats = k !== "night_bias" && v.hit_rate != null
            && night.hit_rate != null && night.hit_rate > v.hit_rate;
          return el("tr", { class: v.reliable ? "" : "opacity-50" }, [
            el("td", {}, [el("div", {}, label),
                          el("div", { class: "text-secondary", style: "font-size:.72rem" }, note)]),
            el("td", { class: "text-end" }, v.calls ?? 0),
            el("td", { class: "text-end text-secondary" }, v.neutral ?? 0),
            el("td", { class: "text-end fw-semibold" },
               v.hit_rate == null ? "—" : `${v.hit_rate}%`),
            el("td", {}, !v.reliable ? el("span", { class: "text-secondary small" }, "표본 부족")
              : k === "night_bias" ? badge("검증 대상", "primary")
              : beats ? badge("야간이 앞섬", "success") : badge("대조군이 앞섬", "warning")),
          ]);
        })));
      // 판정 — 미리 못박는다
      const n = sc.night_bias || {};
      const controls = ["base_regime", "gap_bias"].map((k) => sc[k] || {});
      const ready = n.reliable && controls.some((c) => c.reliable);
      rgBody.appendChild(el("div", { class: "small mt-2" }, [
        el("span", { class: "badge me-2 " + (ready ? "text-bg-info" : "text-bg-secondary") },
           ready ? "판정 가능" : `표본 축적 중 (${n.calls ?? 0}/${d.min_days})`),
        el("span", { class: "text-secondary" }, ready
          ? (controls.every((c) => c.hit_rate == null || n.hit_rate > c.hit_rate)
             ? "야간 리포트가 결정론 대조군을 모두 앞섭니다 — 기준선으로 계속 쓸 근거가 됩니다."
             : "야간 리포트가 결정론 대조군을 이기지 못합니다 — regime_gate.use_night_bias 를 끄는 것이 정직한 결론입니다.")
          : "방향을 건 날이 기준에 못 미칩니다. 중립은 적중 계산에서 뺍니다 — 방향을 안 건 것을 맞았다고 세면 '항상 중립'이 만점이 됩니다."),
      ]));
      // 일자별
      rgBody.appendChild(el("div", { class: "fw-semibold small mt-3 mb-1" }, "일자별 판정"));
      rgBody.appendChild(tableOf(
        "<th>날짜</th><th>야간</th><th>전일 breadth</th><th>시가갭</th>" +
        "<th>합성</th><th class='text-end'>실현 시장</th><th></th>",
        daily.slice(-30).reverse().map((r) => {
          const cell = (v) => el("td", { class: REGIME_TONE[v] || "" }, v || "—");
          const m = r.market_pct;
          const nightSign = { 강세: 1, 약세: -1 }[r.night_bias] || 0;
          const ok = m == null || !nightSign ? null : (nightSign > 0) === (m > 0);
          return el("tr", {}, [
            el("td", { class: "text-nowrap" }, r.date),
            cell(r.night_bias), cell(r.base_regime), cell(r.gap_bias), cell(r.effective),
            el("td", { class: "text-end " + (m > 0 ? "text-danger" : m < 0 ? "text-primary" : "") },
               m == null ? "—" : `${m}%`),
            el("td", {}, ok == null ? el("span", { class: "text-secondary" }, "—")
              : badge(ok ? "야간 적중" : "야간 빗나감", ok ? "success" : "secondary")),
          ]);
        })));
    };
    const loadRegime = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/regime/history"); } catch (e) { return; }
      if (!changed("regime", d)) return;
      renderRegime(d);
    };

    // ==================================================================
    // ⑦ 뉴스 영향 검증 — LLM 이 매기는 두 필드가 수익률과 상관이 있는가
    // impact_horizon 은 매번 만들면서 어떤 판정에도 안 쓰인다.
    // impact_direction 은 반대로 발굴 엔진이 악재를 거르는 데 쓴다.
    // ==================================================================
    const niRun = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" },
      "지금 실행");
    const niBody = el("div", {}, emptyRow("불러오는 중…"));
    newsC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-newspaper"></i> TNM 이 뉴스마다 매기는 <b>영향 지속기간</b>과 <b>방향</b>을 실제 수익률과 맞춰 본다. 지속기간은 지금 <b>어떤 판정에도 쓰이지 않고</b>(화면 뱃지 전용), 방향은 반대로 <b>발굴 엔진이 악재를 거르는 기준</b>이다 — 근거 없으면 그 필터가 잡음이라는 뜻이다. 진입은 뉴스 다음 날 시가 기준(장중 뉴스를 그 자리에서 잡았다고 치면 실현 불가능한 수익을 세게 된다).' })),
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [niRun]),
      niBody,
    );

    const renderNews = () => {
      const d = niData;
      niBody.innerHTML = "";
      if (!d || d.ok === false || !d.run_ts) {
        niBody.appendChild(emptyRow(
          (d && d.error) || "아직 실행되지 않았습니다 — '지금 실행'을 누르세요"));
        return;
      }
      niBody.appendChild(el("div", { class: "small text-secondary mb-2" },
        `${d.date_from} ~ ${d.date_to} · 분석 ${(d.analyses || 0).toLocaleString()}건 중 ` +
        `일봉 매칭 ${(d.matched || 0).toLocaleString()}건 · ${d.symbols}종목 · ` +
        `실행 ${String(d.run_ts).replace("T", " ").slice(0, 16)}`));
      const tone = (v) => (v > 0 ? "text-danger" : v < 0 ? "text-primary" : "");
      const table = (rows, ko, label) => tableOf(
        `<th>${label}</th><th class='text-end'>표본</th>` +
        (d.targets || []).map((t) => `<th class='text-end'>${t.replace("fwd_", "")}일 초과</th>`).join("") +
        "<th class='text-end'>t값(1일)</th>",
        rows.map((b) => el("tr", { class: b.reliable ? "" : "opacity-50" }, [
          el("td", {}, (ko && ko[b.key]) || b.key || "—"),
          el("td", { class: "text-end" }, b.n.toLocaleString()),
          ...(d.targets || []).map((t) => el("td", {
            class: "text-end " + tone(b["exc_" + t]),
          }, b["exc_" + t] == null ? "—" : `${b["exc_" + t]}%`)),
          el("td", { class: "text-end " + (Math.abs(b.t_fwd_1 || 0) > 2 ? "fw-semibold" : "text-secondary") },
             b.reliable ? b.t_fwd_1 : "표본부족"),
        ])));
      // 지속기간 — 크기가 아니라 곡선의 모양
      niBody.appendChild(el("div", { class: "fw-semibold small mb-1" }, "영향 지속기간별"));
      niBody.appendChild(el("div", { class: "small text-secondary mb-1" },
        "즉시(immediate)라면 1일에 몰리고 3·5일엔 안 늘어야 하고, 장기(long)라면 계속 쌓여야 한다. " +
        "세 줄의 모양이 서로 구분되지 않으면 이 필드는 정보를 담고 있지 않다."));
      niBody.appendChild(table(d.by_horizon || [], d.horizon_ko, "지속기간"));
      // 방향 — 발굴 엔진 필터의 근거
      niBody.appendChild(el("div", { class: "fw-semibold small mt-3 mb-1" }, "영향 방향별"));
      niBody.appendChild(el("div", { class: "small text-secondary mb-1" },
        "발굴 엔진의 news 어댑터가 <b>악재를 후보에서 제외</b>한다. 호재와 악재의 초과수익이 갈리지 않으면 그 필터에 근거가 없다.",
      ));
      niBody.appendChild(table(d.by_direction || [], d.direction_ko, "방향"));
      // 판정
      const hz = (d.by_horizon || []).filter((b) => b.reliable);
      const dir = Object.fromEntries((d.by_direction || []).map((b) => [b.key, b]));
      const pos = dir.positive, neg = dir.negative;
      const lines = [];
      if (hz.length >= 2) {
        const shapes = hz.map((b) => (b.exc_fwd_5 ?? 0) - (b.exc_fwd_1 ?? 0));
        const spread = Math.max(...shapes) - Math.min(...shapes);
        // 격차만 보면 안 된다 — 우연히 벌어진 격차도 격차다. 어느 버킷이든
        // 1일 초과수익이 0과 구분되지 않으면 곡선 자체를 논할 근거가 없다.
        const anySolid = hz.some((b) => Math.abs(b.t_fwd_1 || 0) > 2);
        lines.push([anySolid && spread > 1 ? "success" : "warning",
          `지속기간별 1일→5일 증가폭 차이 ${spread.toFixed(2)}%p · ` +
          `1일 초과수익이 0과 구분되는 버킷 ${hz.filter((b) => Math.abs(b.t_fwd_1 || 0) > 2).length}개 — ` +
          (anySolid && spread > 1
            ? "곡선 모양이 실제로 갈린다. 필드가 정보를 담고 있다."
            : !anySolid
              ? "어느 버킷도 1일 초과수익이 0과 구분되지 않는다. 격차가 벌어져 보여도 근거가 아니다 — LLM 스키마에서 빼는 편이 낫다(프롬프트가 짧아지고 재시도가 준다)."
              : "곡선이 구분되지 않는다. LLM 스키마에서 빼는 편이 낫다.")]);
      }
      if (pos && neg && pos.reliable && neg.reliable) {
        const gap = (pos.exc_fwd_1 ?? 0) - (neg.exc_fwd_1 ?? 0);
        lines.push([gap > 0 ? "success" : "danger",
          `호재 ${pos.exc_fwd_1}% vs 악재 ${neg.exc_fwd_1}% (차이 ${gap.toFixed(2)}%p) — ` +
          (gap > 0 ? "발굴 엔진의 악재 제외 필터에 근거가 있다."
                   : "호재가 악재보다 낫지 않다. 발굴 엔진의 악재 필터를 재검토해야 한다.")]);
      }
      if (lines.length) {
        niBody.appendChild(el("div", { class: "small mt-3" }, [
          el("div", { class: "fw-semibold mb-1" }, "판정"),
          ...lines.map(([t, txt]) => el("div", { class: "mb-1" }, [
            el("span", { class: `badge me-2 text-bg-${t}` }, "·"),
            el("span", {}, txt),
          ])),
        ]));
      }
      if ((d.by_category || []).length) {
        // 카테고리를 여러 개 훑으면 그중 하나쯤은 우연히 |t|>2 를 넘는다.
        // 문턱을 올리지 않으면 '살아남은 카테고리' 가 발견이 아니라 다중검정의 산물이다.
        const k = d.by_category.length;
        const bonf = d.bonferroni_t || 2;
        const survivors = d.by_category.filter((b) => b.reliable && Math.abs(b.t_fwd_1 || 0) > bonf);
        niBody.appendChild(el("div", { class: "fw-semibold small mt-3 mb-1" }, "카테고리별 (표본 상위)"));
        niBody.appendChild(el("div", { class: "small text-secondary mb-1" },
          `카테고리를 ${k}개 훑으므로 우연히 넘는 것을 걸러내려면 |t| > ${bonf}(본페로니)를 요구한다. ` +
          (survivors.length
            ? `통과: ${survivors.map((b) => `${b.key} (${b.exc_fwd_1 > 0 ? "+" : ""}${b.exc_fwd_1}%)`).join(", ")}`
            : "통과한 카테고리가 없다.")));
        niBody.appendChild(table(d.by_category, null, "카테고리"));
      }
      niBody.appendChild(el("ul", { class: "small text-secondary mt-3 mb-0 ps-3" },
        (d.caveats || []).map((c) => el("li", {}, c))));
    };
    let niData = null;
    const loadNews = async () => {
      try { niData = await fetchJSON("/api/trading/research/news-impact"); }
      catch (e) { return; }
      if (!changed("newsimpact", niData)) return;
      renderNews();
    };
    niRun.onclick = async () => {
      niRun.disabled = true;
      niBody.innerHTML = "";
      niBody.appendChild(emptyRow("실행 중… 분석 전량을 받아 일봉과 맞춥니다 (별도 프로세스 — 매매는 계속 돕니다)"));
      try {
        niData = await postJSON("/api/trading/research/news-impact/run");
        changed.invalidate("newsimpact");
        renderNews();
      } catch (e) {
        niBody.innerHTML = "";
        niBody.appendChild(el("div", { class: "text-danger small" }, "실패: " + e.message));
      } finally { niRun.disabled = false; }
    };

    // ==================================================================
    // ⑧ 데이터 확보 현황 — 위 검증을 믿을 만한지의 전제
    // ==================================================================
    const covHead = el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" });
    const covBody = el("div", {}, emptyRow("불러오는 중…"));
    dataC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '<i class="bi bi-database"></i> 백테스트 표본의 크기. <b>분봉 일수</b>가 3일 이상이어야 표본이 서고, <b>과거확보</b>는 신규 편입 시 연속조회로 끌어온 과거 2주치다 — 단발 조회는 최근 900봉(≈2.4거래일)뿐이라 이게 끝나야 편입 당일부터 검증이 가능하다.' })),
      covHead, covBody,
    );
    const loadCoverage = async () => {
      let d;
      try { d = await fetchJSON("/api/trading/backtest/coverage"); } catch (e) { return; }
      if (!changed("coverage", d)) return;
      const rows = d.symbols || [];
      covHead.innerHTML = "";
      covBody.innerHTML = "";
      if (!rows.length) {
        covBody.appendChild(emptyRow("감시목록이 비어 있습니다"));
        return;
      }
      const ready = rows.filter((r) => r.days >= 3).length;
      const pend = d.deep_pending || 0;
      covHead.append(
        el("span", { class: "small text-secondary" }, "감시목록"),
        el("span", { class: "badge text-bg-light text-dark" }, `전체 ${rows.length}`),
        badge(`표본 형성 ${ready}`, ready ? "success" : "secondary"),
        badge(pend ? `과거확보 대기 ${pend}` : "과거확보 완료", pend ? "warning" : "success"),
        el("span", { class: "small text-secondary" },
          pend ? "사이클마다 순차 처리 — 장중 상한에 밀린 종목은 마감 후 회수됩니다"
               : "전 종목 연속조회 완료"),
      );
      covBody.appendChild(tableOf(
        "<th>종목</th><th class='text-end'>분봉 일수</th><th>과거확보</th><th></th>",
        rows.map((r) => {
          const run = el("button", { class: "btn btn-sm btn-outline-secondary py-0" }, "백테스트");
          run.onclick = () => { btInput.value = r.code; btTf.value = "1m"; runBacktest(); };
          return el("tr", {}, [
            el("td", {}, `${r.name} (${r.code})`),
            el("td", { class: "text-end " + (r.days >= 3 ? "text-success fw-semibold" : "text-secondary") }, `${r.days}일`),
            el("td", { class: r.deep ? "text-success" : "text-secondary",
                       html: r.deep ? '<i class="bi bi-check-lg"></i>' : "대기" }),
            el("td", {}, run),
          ]);
        })));
    };

    // 감시목록 카드의 '백테스트' 버튼에서 넘어온 종목 자동 실행
    const pending = sessionStorage.getItem("backtest:symbol");
    if (pending) {
      sessionStorage.removeItem("backtest:symbol");
      btInput.value = pending;
      btTf.value = "1m";
      runBacktest();
    }

    await Promise.all([loadPerformance(), loadCoverage(), loadBacktestReport(),
                       loadStudy(), loadRanking(), loadRegime(), loadNews()]);
    ctx.addTimer(setInterval(() => { loadPerformance(); loadCoverage(); }, 30_000));
    ctx.addTimer(setInterval(() => {
      loadBacktestReport(); loadStudy(); loadRanking();
      loadRegime(); loadNews();
    }, 300_000));
  },
};
