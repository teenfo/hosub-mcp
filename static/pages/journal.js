import { fetchJSON, el, card } from "../app.js";
import { postJSON, fmt, won, pct, makeChanged, stockHTML } from "./tradelib.js";

// 매매일지 (트레이딩 그룹): 하루치 매매 경과를 누적 보관하고 성공·실패 요인을 정리.
// 수치·요인은 서버가 결정론으로 계산하고, LLM 은 그 사실을 문장으로 엮기만 한다.

const plCls = (n) => (n > 0 ? "text-danger" : n < 0 ? "text-primary" : "");
const REASON_KO = { stop: "손절", target: "목표", eod: "마감정리",
                    timeout: "시간초과", manual: "수동" };

const metric = (label, value, cls) => el("div", { class: "col-6 col-md-3 col-xl-2" },
  el("div", { class: "border rounded p-2 h-100" }, [
    el("div", { class: "small text-secondary" }, label),
    el("div", { class: "fw-semibold " + (cls || "") }, value),
  ]));

/** 하루치 일지 본문을 target 에 그린다 — 일지 페이지와 매매 데스크의 일지
 *  모달이 같은 렌더러를 쓴다(같은 데이터를 두 벌로 그리면 반드시 어긋난다). */
export function renderJournalDay(target, d) {
  target.innerHTML = "";
  const t = d.trades || {};
  const sig = d.signals || {};
  target.appendChild(el("div", { class: "d-flex gap-2 align-items-center flex-wrap mb-2" }, [
    el("span", { class: "fw-semibold" }, d.date),
    d.final
      ? el("span", { class: "badge text-bg-light text-dark" },
          "확정 " + String(d.generated || "").slice(5, 16).replace("T", " "))
      : el("span", { class: "badge text-bg-warning",
          title: "장 마감 전이라 그때그때 다시 계산한 값입니다" }, "진행 중"),
  ]));
  target.appendChild(el("div", { class: "row g-2 mb-3" }, [
    metric("청산", `${t.trades || 0}건`),
    metric("승 / 패", `${t.wins || 0} / ${t.losses || 0}`),
    metric("승률", t.trades ? `${t.win_rate}%` : "—"),
    metric("실현손익", t.trades ? won(t.pnl_krw) : "—", plCls(t.pnl_krw)),
    metric("건당 기대값", t.trades ? pct(t.expectancy_pct) : "—"),
    metric("신호 / 미발주", `${sig.total || 0} / ${sig.blocked || 0}`),
  ]));

  // LLM 요약
  const sum = d.summary || {};
  if (sum.text) {
    target.appendChild(el("div", { class: "card mb-3" }, [
      el("div", { class: "card-header py-1 small fw-semibold",
        html: `<i class="bi bi-chat-left-text"></i> 요약 <span class="text-secondary fw-normal">(${sum.role || "llm"} · 참고용)</span>` }),
      el("div", { class: "card-body py-2 small", style: "white-space:pre-wrap" }, sum.text),
    ]));
  } else if (sum.pending) {
    // 장중에는 요약을 만들지 않는다 — 오전까지의 손익을 하루의 결론처럼 쓰게 된다.
    target.appendChild(el("div", { class: "alert alert-info py-2 px-3 small mb-3" },
      [el("i", { class: "bi bi-hourglass-split" }),
       " 요약 대기 — " + (sum.reason || "장 마감 후 작성됩니다") +
       ". 아래 집계·관찰 사실은 지금까지의 실제 기록입니다."]));
  } else if (sum.error || sum.reason) {
    target.appendChild(el("div", { class: "alert alert-warning py-2 px-3 small mb-3" },
      "요약 없음 — " + (sum.error || sum.reason) + " (아래 사실 정리는 그대로 유효합니다)"));
  }

  // 관찰 사실 (결정론)
  const facts = d.facts || [];
  if (facts.length) {
    target.appendChild(el("div", { class: "mb-3" }, [
      el("div", { class: "fw-semibold small mb-1",
        html: '<i class="bi bi-clipboard-check"></i> 관찰 사실 (체결 기록에서 계산)' }),
      // '[관측]' 로 시작하는 줄은 아직 검증되지 않은 관측치다 — 판단에
      // 쓰이는 사실과 같은 무게로 읽히지 않도록 흐리게 둔다.
      el("ul", { class: "small mb-0" }, facts.map((f) =>
        el("li", { class: String(f).startsWith("[관측]") ? "text-muted" : "" }, f))),
    ]));
  }

  // 청산 내역
  const ps = d.positions || [];
  if (ps.length) {
    const tb = el("tbody");
    for (const p of ps) {
      tb.appendChild(el("tr", {}, [
        el("td", { class: "small text-secondary text-nowrap" },
          String(p.opened || "").slice(11, 16) + "→" + String(p.closed || "").slice(11, 16)),
        el("td", { class: "text-nowrap", html: stockHTML(p.symbol, p.name) }),
        el("td", {}, p.rule),
        el("td", { class: "text-end" }, fmt(p.qty)),
        el("td", { class: "text-end small",
          title: p.exit_fill_confirmed
            ? "실제 체결가" + (p.model_exit && p.model_exit !== p.exit
                ? ` (이론가 ${fmt(p.model_exit)})` : "")
            : "이론가(손절선·목표가) — 실체결 미수신" },
          [`${fmt(p.entry)} → ${fmt(p.exit)}`,
           p.exit_fill_confirmed ? null
             : el("span", { class: "text-secondary" }, " ~")]),
        // 결과(익절/손절)는 **실제 손익 부호**로 정한다 — 사유가 손절이어도
        // 이익이면 익절이다. 기계적 사유는 그 뒤에 작게 남긴다: 목표에 닿은
        // 이익과 올라간 손절선에 닿은 이익은 다음에 볼 곳이 다르다.
        el("td", { class: "text-nowrap" }, [
          el("span", {
            class: "badge text-bg-" + (p.outcome === "익절" ? "danger"
              : p.outcome === "손절" ? "primary" : "secondary"),
            title: "실제 손익 부호로 정한 결과" },
            p.outcome || "—"),
          el("span", { class: "text-secondary ms-1", style: "font-size:.72rem",
            title: "청산이 실제로 발동된 기계적 사유" },
            REASON_KO[p.exit_reason] || p.exit_reason || ""),
        ]),
        el("td", { class: "text-end fw-semibold " + plCls(p.pnl_pct) },
          `${pct(Number(p.pnl_pct || 0).toFixed(2))} (${won(p.pnl_krw)})`),
      ]));
    }
    const tbl = el("table", { class: "table table-sm small mb-0" });
    tbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>규칙</th>" +
      "<th class='text-end'>수량</th><th class='text-end'>진입→청산</th>" +
      "<th>결과 <span class='fw-normal text-secondary'>· 사유</span></th>" +
      "<th class='text-end'>손익</th></tr>" }));
    tbl.appendChild(tb);
    target.appendChild(el("div", { class: "mb-3" }, [
      el("div", { class: "fw-semibold small mb-1" }, "청산 내역"),
      el("div", { class: "table-responsive" }, tbl),
    ]));
  }

  // 미발주 사유 분포 — 자금 배분 설정의 직접적 결과라 따로 보여준다
  const notes = sig.by_note || {};
  const noteKeys = Object.keys(notes);
  if (noteKeys.length) {
    target.appendChild(el("div", { class: "mb-3" }, [
      el("div", { class: "fw-semibold small mb-1",
        html: '<i class="bi bi-funnel"></i> 미발주 사유 <span class="text-secondary fw-normal">— 자금·한도 설정을 조정할 근거</span>' }),
      el("div", { class: "d-flex gap-2 flex-wrap" },
        noteKeys.sort((a, b) => notes[b] - notes[a]).map((k) =>
          el("span", { class: "badge text-bg-" +
            (["잔고 부족", "비중 상한", "포지션 한도"].includes(k) ? "warning" : "light") +
            (["잔고 부족", "비중 상한", "포지션 한도"].includes(k) ? "" : " text-dark") },
            `${k} ${notes[k]}`))),
    ]));
  }

  // 신호 전체 (우선순위 순서 확인용)
  const rows = sig.rows || [];
  if (rows.length) {
    const tb = el("tbody");
    for (const s of [...rows].sort((a, b) => (b.priority || 0) - (a.priority || 0))) {
      tb.appendChild(el("tr", { class: s.actionable ? "" : "text-secondary" }, [
        el("td", { class: "small text-nowrap" }, String(s.ts || "").slice(11, 16)),
        el("td", { class: "text-nowrap", html: stockHTML(s.symbol, s.name) }),
        el("td", {}, s.rule),
        el("td", { class: "text-end" }, s.priority != null ? Math.round(s.priority) : "—"),
        el("td", { class: "text-end" }, s.qty ? `${s.qty}주` : "—"),
        el("td", {}, s.actionable
          ? el("span", { class: "badge text-bg-success" }, "발주")
          : el("span", { class: "badge text-bg-secondary" }, "미발주")),
        el("td", { class: "small" }, s.note || s.reason || ""),
      ]));
    }
    const tbl = el("table", { class: "table table-sm small mb-0" });
    tbl.appendChild(el("thead", { html: "<tr><th>시각</th><th>종목</th><th>규칙</th>" +
      "<th class='text-end'>우선순위</th><th class='text-end'>수량</th><th>결과</th><th>비고</th></tr>" }));
    tbl.appendChild(tb);
    target.appendChild(el("details", {}, [
      el("summary", { class: "small fw-semibold" }, `신호 전체 ${rows.length}건 (우선순위 순)`),
      el("div", { class: "table-responsive mt-2" }, tbl),
    ]));
  }
}

export default {
  id: "journal",
  title: "매매일지",
  icon: "bi-journal-text",
  group: "트레이딩",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const trendC = card("누적 경과", null, { wide: true, icon: "bi-graph-up" });
    trendC.col.className = "col-12";
    const dayC = card("하루 일지", null, { wide: true, icon: "bi-journal-text" });
    dayC.col.className = "col-12";
    row.append(trendC.col, dayC.col);

    // --- 누적 (일자별 목록 + 기간 합계) ---
    const daysSel = el("select", { class: "form-select form-select-sm w-auto" },
      [[7, "최근 7일"], [30, "최근 30일"], [90, "최근 90일"], [365, "최근 1년"]]
        .map(([v, t]) => el("option", { value: v }, t)));
    daysSel.value = "30";
    const trendBody = el("div");
    trendC.body.append(
      el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" }, [
        el("span", { class: "small text-secondary" }, "기간"), daysSel,
        el("span", { class: "small text-secondary ms-auto" },
          "일자를 클릭하면 그날 일지를 봅니다"),
      ]),
      trendBody,
    );

    // --- 하루 일지 ---
    const dateIn = el("input", { type: "date", class: "form-control form-control-sm w-auto" });
    const todayBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "오늘");
    const runBtn = el("button", { class: "btn btn-sm btn-outline-primary", type: "button" },
      [el("i", { class: "bi bi-arrow-clockwise" }), " 다시 작성"]);
    const runMsg = el("span", { class: "small" });
    const dayBody = el("div");
    dayC.body.append(
      el("div", { class: "d-flex align-items-center gap-2 flex-wrap mb-2" },
        [dateIn, todayBtn, runBtn, runMsg]),
      el("div", { class: "small text-secondary mb-2" },
        el("span", { html: '체결·신호 기록에서 <b>서버가 계산한 사실</b>이 근거입니다. 요약 문장만 로컬 LLM(llm-gateway)이 씁니다 — 게이트웨이가 꺼져 있어도 나머지는 그대로 나옵니다. <b>요약은 장 마감 후에만</b> 작성됩니다(장중 요약은 오전까지의 손익을 하루의 결론처럼 쓰게 되므로). 장중에도 <b>다시 작성</b>으로 집계·관찰 사실은 언제든 갱신할 수 있습니다.' })),
      dayBody,
    );

    const today = () => new Date().toLocaleDateString("sv-SE");   // YYYY-MM-DD (KST 로컬)
    let curDate = today();

    const renderTrend = (d) => {
      trendBody.innerHTML = "";
      const c = d.cumulative || {};
      if (!c.trades) {
        trendBody.appendChild(el("div", { class: "text-secondary small" },
          `기간 내 청산 거래 없음 (일지 ${c.days || 0}일치)`));
      } else {
        trendBody.appendChild(el("div", { class: "row g-2 mb-3" }, [
          metric("매매일", `${c.trading_days}일 / ${c.days}일`),
          metric("거래", `${c.trades}건`),
          metric("승률", `${c.win_rate}%`),
          metric("누적 손익", won(c.pnl_krw), plCls(c.pnl_krw)),
          metric("건당 기대값", pct(c.expectancy_pct)),
          metric("최고 / 최저일", `${(c.best_day || "").slice(5)} / ${(c.worst_day || "").slice(5)}`),
        ]));
      }
      const rows = d.rows || [];
      if (!rows.length) {
        trendBody.appendChild(el("div", { class: "text-secondary small" },
          "저장된 일지가 없습니다. 아래 '다시 작성'으로 오늘치를 만들 수 있습니다."));
        return;
      }
      const t = el("table", { class: "table table-sm table-hover small mb-0" });
      t.appendChild(el("thead", { html: "<tr><th>날짜</th><th class='text-end'>거래</th>" +
        "<th class='text-end'>승률</th><th class='text-end'>실현손익</th>" +
        "<th class='text-end'>기대값</th><th class='text-end'>신호(미발주)</th><th>요약</th></tr>" }));
      const tb = el("tbody");
      for (const r of rows) {
        const tr = el("tr", { style: "cursor:pointer",
          class: r.date === curDate ? "table-active" : "" }, [
          el("td", { class: "text-nowrap fw-semibold" }, r.date),
          el("td", { class: "text-end" }, r.trades ? `${r.trades}건` : "—"),
          el("td", { class: "text-end" }, r.trades ? `${r.win_rate}%` : "—"),
          el("td", { class: "text-end " + plCls(r.pnl_krw) }, r.trades ? won(r.pnl_krw) : "—"),
          el("td", { class: "text-end" }, r.trades ? pct(r.expectancy_pct) : "—"),
          el("td", { class: "text-end" }, `${r.signals}(${r.blocked})`),
          el("td", {}, r.has_summary
            ? el("span", { class: "badge text-bg-success" }, "작성됨")
            : el("span", { class: "badge text-bg-" + (r.final ? "secondary" : "warning") },
                r.final ? "없음" : "진행 중")),
        ]);
        tr.onclick = () => { curDate = r.date; dateIn.value = r.date; loadDay(); };
        tb.appendChild(tr);
      }
      t.appendChild(tb);
      trendBody.appendChild(el("div", { class: "table-responsive" }, t));
    };

    const renderDay = (d) => renderJournalDay(dayBody, d);

    const loadDay = async () => {
      let d;
      try {
        d = await fetchJSON("/api/trading/journal?date=" + encodeURIComponent(curDate));
      } catch (e) {
        dayBody.innerHTML = "";
        dayBody.appendChild(el("div", { class: "text-danger small" },
          "일지를 불러올 수 없습니다: " + e.message));
        return;
      }
      if (changed("day:" + curDate, d)) renderDay(d);
    };

    const loadTrend = async () => {
      let d;
      try {
        d = await fetchJSON("/api/trading/journal/history?days=" + daysSel.value);
      } catch (e) {
        trendBody.innerHTML = "";
        trendBody.appendChild(el("div", { class: "text-danger small" },
          "누적 조회 실패: " + e.message));
        return;
      }
      renderTrend(d);
    };

    daysSel.onchange = loadTrend;
    dateIn.onchange = () => { curDate = dateIn.value || today(); loadDay(); };
    todayBtn.onclick = () => { curDate = today(); dateIn.value = curDate; loadDay(); };
    runBtn.onclick = async () => {
      runBtn.disabled = true;
      runMsg.className = "small text-secondary";
      runMsg.textContent = "작성 중… (요약 생성에 최대 1~2분)";
      try {
        const d = await postJSON("/api/trading/journal/run", { date: curDate });
        renderDay(d);
        await loadTrend();
        const sum = d.summary || {};
        runMsg.className = "small " + (sum.ok ? "text-success"
                                     : sum.pending ? "text-secondary" : "text-warning");
        runMsg.textContent = sum.ok ? "작성 완료"
          : sum.pending ? "집계 갱신 완료 — " + (sum.reason || "요약은 장 마감 후")
          : "작성 완료 (요약은 실패 — 사실 정리는 유효)";
      } catch (e) {
        runMsg.className = "small text-danger";
        runMsg.textContent = "실패: " + e.message;
      } finally { runBtn.disabled = false; }
    };

    dateIn.value = curDate;
    dateIn.max = curDate;
    await Promise.all([loadTrend(), loadDay()]);
    ctx.addTimer(setInterval(loadDay, 60_000));   // 진행 중인 오늘치만 완만히 갱신
  },
};
