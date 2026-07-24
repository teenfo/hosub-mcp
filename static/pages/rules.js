import { fetchJSON, el, card, badge } from "../app.js";
import { postJSON, pct, makeChanged } from "./tradelib.js";

// 매매 기법 페이지 (트레이딩 그룹): 엔진에 등록된 규칙(테크닉)의 셋업·파라미터·
// 활성 상태를 실전 성과(성과 로그)·백테스트 결과와 함께 보여준다. 읽기 전용 —
// 파라미터 변경은 config.yaml, 새 기법 추가는 rules.py 레지스트리 참조.

const RULE_LABEL = {
  orb: "시초가 범위 돌파 (ORB)",
  gap: "갭 플레이",
  momentum: "모멘텀 돌파",
  pullback: "눌림목",
  rsi_dip: "RSI 급락 눌림",
  gap_fill: "갭필 평균회귀",
  vwap_reclaim: "VWAP 되찾기",
  range_break_retest: "박스 돌파 리테스트",
  bounce_fade: "반등 페이드",
  breakdown_retest: "지지 붕괴 리테스트",
};
const PARAM_LABEL = {
  range_start: "범위 시작", range_end: "범위 끝", target_r: "목표(R배수)",
  min_gap_pct: "최소 갭 %", range_wait_until: "범위 대기까지",
  trail_long_pct: "트레일(롱) %", trail_short_pct: "트레일(숏) %",
  lookback: "돌파 판단 봉수", stop_lookback: "손절 저점 봉수",
  atr_stop_mult: "ATR 손절 배수", atr_period: "ATR 기간",
  near_pct: "이평 근접 %", rsi_hot: "RSI 하한", rsi_max: "RSI 상한",
  candle_confirm: "캔들 확인", require_downtrend: "일봉 하락 게이트",
  require_volume_dry: "거래량 소진 조건", support_lookback: "지지선 봉수",
  retest_tolerance_pct: "리테스트 허용 %", require_volume: "거래량 확인",
  vol_confirm_ratio: "거래량 배수", min_bars: "최소 봉수",
  below_lookback: "VWAP 하회 관찰 봉수", min_below_bars: "하회 체류 최소 봉수",
  vol_ratio: "거래량 배수(0=끔)", range_lookback: "박스 산출 봉수",
  regimes: "활성 국면(자동 연동)", min_gap_pct: "갭하락 최소 %",
  confirm_bars: "반전 확인 봉수", min_rr: "최소 손익비", max_range_pct: "범위 상한 %",
  min_range_pct: "범위 하한 %", rsi_period: "RSI 기간", oversold: "과매도 기준",
  entry_before: "진입 마감시각(시간대 필터)",
};
const sideBadgeOf = (side) =>
  side === "long" ? badge("롱 전용", "success")
    : side === "short" ? badge("숏 전용", "primary")
      : badge("롱/숏", "secondary");

export default {
  id: "rules",
  title: "매매 기법",
  icon: "bi-journal-code",
  group: "트레이딩",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);
    const sumC = card("공통 안전장치 · 게이트", null, { wide: true, icon: "bi-shield-lock" });
    sumC.col.className = "col-12";
    row.appendChild(sumC.col);
    const rulesRow = el("div", { class: "row g-3 col-12 m-0 p-0" });
    row.appendChild(rulesRow);

    const load = async () => {
      let r, perf, risk, bt, sw;
      try { r = await fetchJSON("/api/trading/rules"); } catch (e) { return; }
      try { perf = await fetchJSON("/api/trading/performance"); } catch (e) { perf = null; }
      try { risk = await fetchJSON("/api/trading/risk"); } catch (e) { risk = null; }
      try { bt = await fetchJSON("/api/trading/backtest/report/latest"); } catch (e) { bt = null; }
      try { sw = await fetchJSON("/api/trading/backtest/sweep/latest"); } catch (e) { sw = null; }
      if (!changed("rules", [r, perf && perf.stats, risk, bt && bt.summary, sw && sw.run_ts])) return;

      // --- 공통 안전장치 요약 ---
      sumC.body.innerHTML = "";
      const chip = (label, value, tone) => el("div", { class: "col-6 col-md-3 col-xl-2" },
        el("div", { class: "border rounded p-2" }, [
          el("div", { class: "text-secondary small" }, label),
          el("div", { class: "fw-semibold small " + (tone || "") }, value),
        ]));
      sumC.body.appendChild(el("div", { class: "row g-2" }, [
        chip("매매 방향", r.long_only ? "롱 전용 (현물)" : "롱+숏"),
        chip("손절폭 상한", (r.max_stop_pct || "-") + "% (초과 신호 폐기)"),
        risk ? chip("거래당 리스크", risk.risk_per_trade_pct + "% (수량 사이징)") : null,
        risk ? chip("일일 목표/한도", `+${risk.daily_target_pct}% / -${risk.daily_loss_limit_pct}%`) : null,
        risk && risk.regime ? chip("시장 유효국면", risk.regime + (risk.regime === "강세" ? " · 인버스 보류" : " · 인버스 허용"),
          risk.regime === "강세" ? "text-danger" : risk.regime === "약세" ? "text-primary" : "") : null,
        chip("활성 기법", r.rules.filter((x) => x.enabled).length + " / " + r.rules.length),
      ]));
      sumC.body.appendChild(el("div", { class: "small text-secondary mt-2" },
        "모든 기법에 공통 적용: 잔고 동기화 → 일일 가드 → 국면 게이트(인버스) → 롱 전용 → 리스크 사이징. 신호는 금액 제한 없이 기록되고, 주문 생성만 게이트를 거칩니다."));

      // --- 기법 카드들 ---
      rulesRow.innerHTML = "";
      const perfByRule = (perf && perf.stats && perf.stats.by_rule) || {};
      const btByRule = (bt && bt.summary && bt.summary.by_rule) || {};
      for (const rule of r.rules) {
        const c = el("div", { class: "col-12 col-xl-6" });
        const cardEl = el("div", { class: "card shadow-sm h-100" + (rule.enabled ? "" : " opacity-50") });
        // ON/OFF 토글 — 서버에 영속화(재시작 유지). 다음 엔진 사이클(60초)부터 반영.
        const tgl = el("button", {
          class: "btn btn-sm ms-auto " + (rule.enabled ? "btn-outline-danger" : "btn-outline-success"),
          type: "button",
        }, rule.enabled ? "끄기" : "켜기");
        tgl.onclick = async () => {
          const onMsg = `[${RULE_LABEL[rule.name] || rule.name}] 기법을 켤까요? 다음 신호 사이클부터 실제 매매 신호를 만듭니다.`;
          const offMsg = `[${RULE_LABEL[rule.name] || rule.name}] 기법을 끌까요? 신규 신호가 더 이상 생성되지 않습니다(기존 포지션은 유지).`;
          if (!confirm(rule.enabled ? offMsg : onMsg)) return;
          tgl.disabled = true;
          try {
            await postJSON(`/api/trading/rules/${rule.name}/toggle`, { enabled: !rule.enabled });
            changed.invalidate("rules");
            await load();
          } catch (e) { alert("실패: " + e.message); tgl.disabled = false; }
        };
        cardEl.appendChild(el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
          el("span", { class: "fw-semibold" }, RULE_LABEL[rule.name] || rule.name),
          el("code", { class: "small" }, rule.name),
          sideBadgeOf(rule.side),
          rule.enabled
            ? (rule.regime_blocked
                ? badge(`국면 대기 (현재 ${rule.cur_regime})`, "warning")
                : badge("가동 중", "success"))
            : badge("비활성", "secondary"),
          tgl,
        ]));
        const body = el("div", { class: "card-body small" });
        body.appendChild(el("div", { class: "text-secondary mb-2" }, rule.desc || ""));
        // 파라미터 표
        const params = Object.entries(rule.config).filter(([k]) => k !== "enabled");
        if (params.length) {
          const t = el("table", { class: "table table-sm mb-2 small" });
          const tb = el("tbody");
          for (const [k, v] of params) {
            tb.appendChild(el("tr", {}, [
              el("td", { class: "text-secondary", style: "width:45%" }, PARAM_LABEL[k] || k),
              el("td", {}, String(v)),
            ]));
          }
          t.appendChild(tb);
          body.appendChild(el("div", { class: "table-responsive" }, t));
        }
        // 실전/백테스트 성과 (있을 때만)
        const live = perfByRule[rule.name];
        const btr = btByRule[rule.name];
        const swr = sw && sw.rules && sw.rules[rule.name];
        const perfBits = [];
        if (live) perfBits.push(`실전: ${live.trades}건 · 승률 ${live.win_rate}% · 기대값 ${pct(live.expectancy_pct)}`);
        if (btr != null) perfBits.push(`백테스트 건당손익: ${typeof btr === "object" ? JSON.stringify(btr) : btr}%`);
        if (swr) perfBits.push(swr.trades
          ? `주간스윕(${(sw.run_ts || "").slice(5, 10)}): ${swr.trades}건 · 승률 ${swr.win_rate}% · 평균R ${swr.avg_r >= 0 ? "+" : ""}${swr.avg_r}`
          : `주간스윕: 표본 없음`);
        body.appendChild(el("div", { class: perfBits.length ? "text-body" : "text-secondary" },
          perfBits.length ? "📈 " + perfBits.join(" · ") : "성과 데이터 아직 없음 — 체결이 쌓이면 표시됩니다"));
        cardEl.appendChild(body);
        c.appendChild(cardEl);
        rulesRow.appendChild(c);
      }
      const swBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "기법 스윕 지금 실행");
      swBtn.onclick = async () => {
        if (!confirm("전 기법 격리 백테스트(수 분 소요)를 지금 실행할까요? 주문 없음.")) return;
        swBtn.disabled = true;
        try { const res = await postJSON("/api/trading/backtest/sweep/run"); alert(res.message || "시작됨"); }
        catch (e) { alert("실패: " + e.message); }
        finally { swBtn.disabled = false; }
      };
      rulesRow.appendChild(el("div", { class: "col-12 d-flex gap-2 align-items-center flex-wrap small text-secondary" }, [
        swBtn,
        el("span", {}, (sw && sw.run_ts) ? `최근 스윕 ${sw.run_ts.replace("T", " ").slice(0, 16)} · ${sw.symbols}종목 · 매주 토 09시 자동` : "스윕 결과 없음 — 매주 토 09시 자동 실행"),
      ]));
      rulesRow.appendChild(el("div", { class: "col-12 small text-secondary" },
        "⚙️ 파라미터 변경: trading/config.yaml rules 섹션 · 새 기법 추가: app/signals/rules.py 레지스트리(@register) — 함수 + 데코레이터 + config 블록이면 끝. 주간 스윕이 매주 성적표를 갱신합니다."));
    };

    await load();
    ctx.addTimer(setInterval(load, 30_000));
  },
};
