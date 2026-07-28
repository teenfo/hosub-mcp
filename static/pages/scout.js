import { fetchJSON, el, card, badge } from "../app.js";
import { makeLayoutEditable } from "../layout.js";
import { postJSON, fmt, makeChanged, makeTabs } from "./tradelib.js";

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
// shadow 동안은 '발굴·감시' 페이지가 실제 감시목록의 주인이고, 이 페이지는
// 엔진의 판단만 보여 준다. 두 페이지를 나란히 두는 것이 관찰의 요점이다.

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

export default {
  id: "scout",
  title: "발굴 엔진",
  icon: "bi-diagram-3",
  group: "트레이딩",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const stateC = card("엔진 상태 · 소스", null, { wide: true, icon: "bi-diagram-3" });
    const queueC = card("후보 큐", null, { wide: true, icon: "bi-list-ol" });
    const diffC = card("판단 대조 (엔진 ↔ 실제 감시목록)", null, { wide: true, icon: "bi-arrow-left-right" });
    const srcC = card("소스별 원시 결과 (패키지 단위)", null, { wide: true, icon: "bi-boxes" });
    const histC = card("승격·강등 이력", null, { wide: true, icon: "bi-clock-history" });
    const CARDS = [["state", stateC], ["queue", queueC],
                   ["diff", diffC], ["source", srcC], ["hist", histC]];
    CARDS.forEach(([id, c], i) => {
      c.col.dataset.cardId = id;
      c.col.dataset.cardIndex = i;
      c.col.className = "col-12 col-xl-12";
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
            el("td", {}, !s.enabled ? badge("꺼짐", "secondary")
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
              el("div", {}, `${c.name} (${c.code})`),
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
                el("div", {}, `${r.name} (${r.code})`),
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
      diffTabs.set("pending", pending.length ? decisionRows(pending, false)
        : emptyRow("지금 바꿀 것이 없습니다 — 후보와 감시목록이 일치합니다"), pending.length);
      const wl = d.watchlist || [];
      diffTabs.set("actual", wl.length ? tableOf(
        "<th>종목</th><th>tier</th><th>보호</th>",
        wl.map((w) => el("tr", {}, [
          el("td", {}, `${w.name} (${w.code})`),
          el("td", {}, badge(TIER_KO[w.tier] || w.tier, TIER_TONE[w.tier] || "light")),
          el("td", {}, w.protected
            ? el("span", { class: "badge text-bg-light text-dark",
                           title: "보유 중이거나 사용자가 직접 지정" }, "강등 제외")
            : el("span", { class: "text-secondary" }, "—")),
        ]))) : emptyRow("감시목록이 비어 있습니다"), wl.length);

      const hist = d.decisions || [];
      histBody.innerHTML = "";
      histBody.appendChild(hist.length ? decisionRows(hist, true)
        : emptyRow("아직 결정 이력이 없습니다 — 소스가 첫 수집을 마치면 쌓이기 시작합니다"));
    };

    runBtn.onclick = async () => {
      runBtn.disabled = true;
      try { await postJSON("/api/trading/scout/run"); changed.invalidate("scout"); await load(); }
      catch (e) { alert("실패: " + e.message); }
      finally { runBtn.disabled = false; }
    };

    await load();
    ctx.addTimer(setInterval(load, 20_000));
  },
};
