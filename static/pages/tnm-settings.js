import { fetchJSON, el, card, badge } from "../app.js";
import { postJSON, makeChanged, makeGearModal, stockHTML } from "./tradelib.js";

// TNM 설정 — 파이프라인 상태 · 관심종목 · Shadow 지표. API 키는 기어 모달로 뺐다.
//
//   [파이프라인 상태]  수집 → 임베딩 → 중복제거 → 분류 → 알림 → 편입 각 단계
//        ↓            한 단계가 막히면 뒤가 전부 조용해진다 — 여기부터 본다
//   [관심종목]        뉴스·공시 수집 대상. 종목별 임계값·일일상한
//   [Shadow 지표]     실운영(실알림) 전환 판정
//   (⚙ 연동 설정)     API 키 7종 · 운영 트리거 4종 — 자주 보지 않으니 모달로
//
// 종전에는 키 카드가 상시 노출되면서 정작 파이프라인 상태는 어디에도 없었고,
// 입력칸은 DART 키 하나뿐이었다.

const ORIGIN_BADGE = {
  trading: '<span class="badge text-bg-primary">감시목록</span>',
  holding: '<span class="badge text-bg-danger">보유</span>',
  manual: '<span class="badge text-bg-success">수동</span>',
  dart: '<span class="badge text-bg-warning" title="감시목록 밖 공시에서 발굴된 종목">공시발굴</span>',
  research: '<span class="badge text-bg-info" title="증권사 리포트에서 발굴된 종목">리포트발굴</span>',
};
// 파이프라인 단계 — /api/status 의 키 순서가 곧 데이터가 흐르는 순서다.
const STAGES = [
  ["collect", "수집", "DART 공시 · 뉴스 RSS · 증권사 리포트"],
  ["embed", "임베딩", "본문 벡터화"],
  ["dedup", "중복제거", "같은 사건의 재탕 걸러내기"],
  ["classify", "분류", "LLM 카테고리·방향·점수"],
  ["notify", "알림", "임계 초과분 슬랙 발송"],
  ["promote", "편입", "고점수 종목 감시목록 승격"],
];
const TIER_KO = { trade: "매매(30분)", collect: "수집(2시간)", other: "관측(12시간)" };

export default {
  id: "tnm-settings",
  title: "TNM 설정",
  icon: "bi-gear",
  group: "TNM",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const gear = makeGearModal(container, "TNM 연동 설정", { label: "연동 설정" });

    // ==================================================================
    // ① 파이프라인 상태 — 어디서 막혔는가
    // ==================================================================
    const statC = card("파이프라인 상태", null, { wide: true, icon: "bi-diagram-2" });
    statC.col.className = "col-12";
    row.appendChild(statC.col);
    const statBody = el("div", { class: "small" },
      el("div", { class: "text-secondary" }, "불러오는 중…"));
    statC.body.append(
      el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [
        el("span", { class: "small text-secondary flex-grow-1", html:
          '뉴스·공시가 <b>수집 → 임베딩 → 중복제거 → 분류 → 알림 → 편입</b> 순으로 흐른다. 한 단계가 막히면 뒤가 전부 조용해지므로, 알림이 안 올 때 여기부터 본다.' }),
        gear.button,
      ]),
      statBody);

    const loadStatus = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/status"); } catch (e) { return; }
      if (!changed("status", d)) return;
      statBody.innerHTML = "";
      statBody.appendChild(el("div", { class: "d-flex gap-2 flex-wrap align-items-center mb-2" }, [
        d.db_ready ? badge("DB 정상", "success") : badge("DB 미준비", "danger"),
        d.ollama ? badge("LLM 연결", "success") : badge("LLM 끊김", "warning"),
        d.dart_enabled ? badge("DART 키", "success") : badge("DART 키 없음", "secondary"),
        d.naver_enabled ? badge("네이버 API", "success") : badge("네이버 미설정 (구글 RSS만)", "secondary"),
        d.shadow_mode
          ? el("span", { class: "badge text-bg-warning",
                         title: "분석은 하되 슬랙 발송은 하지 않는다" }, "Shadow (알림 미발송)")
          : badge("실운영 (알림 발송)", "danger"),
      ]));
      if (d.db_error) {
        statBody.appendChild(el("div", { class: "text-danger mb-2" }, "DB 오류: " + d.db_error));
      }
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>단계</th><th>최근 실행</th><th>결과</th></tr>" }));
      const tb = el("tbody");
      for (const [key, label, desc] of STAGES) {
        const s = d[key] || {};
        // status() 구조가 단계마다 달라(문자열/사전) 공통 키만 골라 읽는다
        const last = s.last_run && typeof s.last_run === "object"
          ? Object.values(s.last_run).sort().pop() : s.last_run;
        const res = s.last_result && typeof s.last_result === "object"
          ? Object.entries(s.last_result).map(([k, v]) =>
              typeof v === "object" ? `${k}: ${JSON.stringify(v)}` : `${k}=${v}`).join(" · ")
          : (s.last_result != null ? String(s.last_result) : "");
        tb.appendChild(el("tr", {}, [
          el("td", {}, [el("div", {}, label),
                        el("div", { class: "text-secondary", style: "font-size:.72rem" }, desc)]),
          el("td", { class: "text-secondary text-nowrap" },
             last ? String(last).replace("T", " ").slice(5, 19) : "—"),
          el("td", { class: "text-secondary", style: "word-break:break-all" },
             res.slice(0, 240) || (s.running ? "실행 중" : "—")),
        ]));
      }
      t.appendChild(tb);
      statBody.appendChild(el("div", { class: "table-responsive" }, t));
      if (d.queue) {
        statBody.appendChild(el("div", { class: "small mt-2" }, [
          el("span", { class: "text-secondary me-2" }, "대기열"),
          ...Object.entries(d.queue).map(([k, v]) => el("span", {
            class: "badge me-1 text-bg-light " + (v ? "text-dark" : "text-secondary"),
          }, `${k} ${v}`)),
        ]));
      }
      if (d.queue_error) {
        statBody.appendChild(el("div", { class: "text-danger small mt-1" },
                                "대기열 조회 실패: " + d.queue_error));
      }
    };

    // ==================================================================
    // ② 관심종목
    // ==================================================================
    const watchC = card("관심종목 (뉴스·공시 수집 대상)", null, { wide: true, icon: "bi-bookmark-star" });
    watchC.col.className = "col-12";
    row.appendChild(watchC.col);
    const addTicker = el("input", { class: "form-control form-control-sm", placeholder: "종목코드 6자리", style: "max-width:130px" });
    const addName = el("input", { class: "form-control form-control-sm", placeholder: "종목명", style: "max-width:160px" });
    const addBtn = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "수동 추가");
    const syncBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "지금 동기화");
    const watchBody = el("div", { class: "small mt-2" });
    watchC.body.append(
      el("div", { class: "small text-secondary mb-2", html:
        "감시목록·보유는 자동 동기(30분). 제외한 종목은 동기화가 되살리지 않고, 수동 추가는 동기화가 건드리지 않습니다. " +
        "<b>공시발굴</b>은 감시목록 밖 DART 공시에서 찾아낸 종목입니다." }),
      el("div", { class: "d-flex gap-2 flex-wrap" }, [addTicker, addName, addBtn, syncBtn]),
      watchBody);

    /** 표 안을 편집 중이면 다시 그리지 않는다.
     *
     * 폴링(60초)이 표를 통째로 다시 만들어 **입력하던 임계값이 지워졌다.**
     * 저장을 누르기 전에 화면이 갱신되면 사용자가 친 값이 사라진다. */
    const editing = () => watchBody.contains(document.activeElement)
      || !!watchBody.querySelector("[data-dirty='1']");

    const loadWatch = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/watch"); } catch (e) { return; }
      if (!changed("watch", d)) return;
      if (editing()) { changed.invalidate("watch"); return; }   // 다음 주기에 다시 그린다
      watchBody.innerHTML = "";
      const entries = d.entries || [];
      const active = entries.filter((e) => e.is_active);
      const excluded = entries.filter((e) => e.is_excluded);
      const byOrigin = entries.reduce((a, e) => (a[e.origin] = (a[e.origin] || 0) + 1, a), {});
      watchBody.appendChild(el("div", { class: "text-secondary mb-1" },
        `활성 ${active.length} · 제외 ${excluded.length} · 전체 ${entries.length}` +
        (Object.keys(byOrigin).length
          ? " — " + Object.entries(byOrigin).map(([k, v]) => `${k} ${v}`).join(" · ") : "")));
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>종목</th><th>출처</th><th>주기</th><th>상태</th><th>임계값</th><th>일일상한</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const e of entries) {
        const tr = el("tr", { class: e.is_active ? "" : "opacity-50" });
        tr.appendChild(el("td", { class: "text-nowrap", html: stockHTML(e.ticker, e.name) }));
        tr.appendChild(el("td", { html: ORIGIN_BADGE[e.origin] || e.origin }));
        tr.appendChild(el("td", { class: "text-secondary text-nowrap" },
                                TIER_KO[e.tier] || e.tier || "—"));
        tr.appendChild(el("td", {}, e.is_excluded ? "제외" : e.is_active ? "활성" : "비활성"));
        const th = el("input", { type: "number", class: "form-control form-control-sm", value: e.score_threshold, style: "width:75px" });
        const cap = el("input", { type: "number", class: "form-control form-control-sm", value: e.daily_alert_cap, style: "width:70px" });
        // 값을 건드린 순간부터 폴링이 이 표를 다시 그리지 못하게 한다
        const markDirty = (ev) => { ev.target.dataset.dirty = "1"; };
        th.oninput = markDirty;
        cap.oninput = markDirty;
        const save = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "저장");
        save.onclick = async () => {
          try {
            await postJSON(`/api/tnm/watch/${e.ticker}/settings`,
              { score_threshold: Number(th.value), daily_alert_cap: Number(cap.value) });
            delete th.dataset.dirty; delete cap.dataset.dirty;
            save.textContent = "저장 ✓"; setTimeout(() => (save.textContent = "저장"), 1500);
          } catch (err) { alert("실패: " + err.message); }
        };
        const tgl = el("button", { class: "btn btn-sm btn-outline-danger py-0 ms-1", type: "button" },
          e.is_excluded ? "복원" : "제외");
        tgl.onclick = async () => {
          try {
            await postJSON(`/api/tnm/watch/${e.ticker}/${e.is_excluded ? "include" : "exclude"}`);
            changed.invalidate("watch"); await loadWatch();
          } catch (err) { alert("실패: " + err.message); }
        };
        tr.appendChild(el("td", {}, th));
        tr.appendChild(el("td", {}, cap));
        tr.appendChild(el("td", {}, [save, tgl]));
        tb.appendChild(tr);
      }
      t.appendChild(tb);
      watchBody.appendChild(el("div", { class: "table-responsive", style: "max-height:480px; overflow-y:auto" }, t));
    };
    addBtn.onclick = async () => {
      const code = addTicker.value.trim();
      if (!/^\d{6}$/.test(code)) { alert("6자리 종목코드를 입력하세요"); return; }
      try {
        await postJSON("/api/tnm/watch", { ticker: code, name: addName.value.trim() });
        addTicker.value = ""; addName.value = "";
        changed.invalidate("watch"); await loadWatch();
      } catch (e) { alert("실패: " + e.message); }
    };
    syncBtn.onclick = async () => {
      syncBtn.disabled = true;
      try { await postJSON("/api/tnm/watch/sync"); changed.invalidate("watch"); await loadWatch(); }
      catch (e) { alert("실패: " + e.message); }
      finally { syncBtn.disabled = false; }
    };

    // ==================================================================
    // ③ 증권사 리서치 — 수집 대상 관리 + 최근 리포트
    // ==================================================================
    const brkC = card("증권사 리서치", null, { wide: true, icon: "bi-file-earmark-bar-graph" });
    brkC.col.className = "col-12";
    row.appendChild(brkC.col);
    const brkAddName = el("input", { class: "form-control form-control-sm", placeholder: "증권사명", style: "max-width:180px" });
    const brkAddKind = el("select", { class: "form-select form-select-sm", style: "max-width:110px" }, [
      el("option", { value: "domestic" }, "국내사"),
      el("option", { value: "foreign" }, "해외사"),
    ]);
    const brkAddAlias = el("input", { class: "form-control form-control-sm", placeholder: "별칭 (쉼표 구분, 선택)", style: "max-width:230px" });
    const brkAddBtn = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "추가");
    const brkRunBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "지금 수집");
    const brkBody = el("div", { class: "small mt-2" });
    const repBody = el("div", { class: "small mt-3" });
    brkC.body.append(
      el("div", { class: "small text-secondary mb-2", html:
        "국내사는 <b>네이버 금융 리서치 · 한경컨센서스</b>에서 리포트 목록(제목·목표주가·투자의견·PDF)을 그대로 받습니다. " +
        "<b>해외사는 리포트 원문이 공개돼 있지 않아</b> 국내 언론이 인용한 기사만 모읍니다 — 원문이 아니라 2차 정보입니다. " +
        "끈 증권사의 리포트는 적재하지 않고, 목록에 없는 이름은 <b>미등록</b>으로 표시됩니다." }),
      el("div", { class: "d-flex gap-2 flex-wrap" },
        [brkAddName, brkAddKind, brkAddAlias, brkAddBtn, brkRunBtn]),
      brkBody, repBody);

    const brkEditing = () => brkBody.contains(document.activeElement);

    /** 조회 실패를 화면에 남긴다.
     *
     * 종전에는 catch 하고 조용히 return 해서, 프록시 화이트리스트가 막혀
     * 404 가 나는데도 카드가 그냥 **비어 보였다**. 목록이 없는 것인지 못
     * 가져온 것인지 구분이 안 되면 원인을 찾는 데 시간이 걸린다. */
    const failed = (body, what, e) => {
      body.innerHTML = "";
      body.appendChild(el("div", { class: "text-danger" },
        `${what} 조회 실패: ${e.message}`));
    };

    const loadBrokers = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/brokers"); }
      catch (e) { failed(brkBody, "증권사 목록", e); return; }
      if (!changed("brokers", d)) return;
      if (brkEditing()) { changed.invalidate("brokers"); return; }
      brkBody.innerHTML = "";
      const list = d.brokers || [];
      const on = list.filter((b) => b.enabled);
      const stats = d.stats || {};
      brkBody.appendChild(el("div", { class: "text-secondary mb-1" },
        `수집 ${on.length} / 등록 ${list.length}` +
        ` — 국내 ${list.filter((b) => b.kind === "domestic").length}` +
        ` · 해외 ${list.filter((b) => b.kind === "foreign").length}` +
        (stats.total != null ? ` · 최근 7일 리포트 ${stats.total}건` : "")));
      if (stats.unregistered && stats.unregistered.length) {
        brkBody.appendChild(el("div", { class: "alert alert-warning py-1 px-2 mb-2 small" },
          "미등록 증권사: " + stats.unregistered.join(", ") +
          " — 위 입력칸으로 추가하면 이름이 통일됩니다"));
      }
      const counts = Object.fromEntries((stats.by_broker || []).map((b) => [b.broker, b.count]));
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>증권사</th><th>구분</th><th>수집</th><th>최근 7일</th><th>누적</th><th>최종 관측</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const b of list) {
        const tr = el("tr", { class: b.enabled ? "" : "opacity-50" });
        tr.appendChild(el("td", {}, [
          el("div", {}, b.name),
          b.aliases.length
            ? el("div", { class: "text-secondary", style: "font-size:.72rem" }, b.aliases.join(" · "))
            : null,
          b.note ? el("div", { class: "text-warning", style: "font-size:.72rem" }, b.note) : null,
        ]));
        tr.appendChild(el("td", { html: b.kind === "foreign"
          ? '<span class="badge text-bg-secondary">해외</span>'
          : '<span class="badge text-bg-light text-dark">국내</span>' }));
        const sw = el("input", { type: "checkbox", class: "form-check-input", role: "switch" });
        sw.checked = !!b.enabled;
        sw.onchange = async () => {
          sw.disabled = true;
          try {
            await postJSON(`/api/tnm/brokers/${encodeURIComponent(b.name)}`,
                           { enabled: sw.checked });
            changed.invalidate("brokers"); await loadBrokers();
          } catch (err) { sw.checked = !sw.checked; alert("실패: " + err.message); }
          finally { sw.disabled = false; }
        };
        tr.appendChild(el("td", {}, el("div", { class: "form-check form-switch mb-0" }, sw)));
        tr.appendChild(el("td", { class: "text-secondary" }, String(counts[b.name] ?? 0)));
        tr.appendChild(el("td", { class: "text-secondary" }, String(b.reports ?? 0)));
        tr.appendChild(el("td", { class: "text-secondary text-nowrap" },
          b.last_seen_at ? String(b.last_seen_at).replace("T", " ").slice(5, 16) : "—"));
        const del = el("button", { class: "btn btn-sm btn-outline-danger py-0", type: "button" }, "제거");
        del.onclick = async () => {
          if (!confirm(`${b.name} 을 목록에서 제거합니다. 이미 수집된 리포트는 남습니다.`)) return;
          try {
            await postJSON(`/api/tnm/brokers/${encodeURIComponent(b.name)}/delete`);
            changed.invalidate("brokers"); await loadBrokers();
          } catch (err) { alert("실패: " + err.message); }
        };
        tr.appendChild(el("td", {}, del));
        tb.appendChild(tr);
      }
      t.appendChild(tb);
      brkBody.appendChild(el("div", { class: "table-responsive", style: "max-height:420px; overflow-y:auto" }, t));
    };

    const CAT_KO = { company: "종목", industry: "산업", market: "시황",
                     economy: "경제", invest: "투자정보" };
    const loadReports = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/reports?days=7&limit=60"); }
      catch (e) { failed(repBody, "리포트", e); return; }
      if (!changed("reports", d)) return;
      repBody.innerHTML = "";
      const rows = d.reports || [];
      repBody.appendChild(el("div", { class: "fw-semibold mb-1" },
        `최근 리포트 (7일 · ${rows.length}건)`));
      if (!rows.length) {
        repBody.appendChild(el("div", { class: "text-secondary" },
          "아직 없음 — [지금 수집]을 누르거나 다음 주기(60분)를 기다리세요"));
        return;
      }
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>날짜</th><th>분류</th><th>종목</th><th>제목</th><th>증권사</th><th>목표가</th><th>의견</th></tr>" }));
      const tb = el("tbody");
      for (const r of rows) {
        const link = r.pdf_url || r.url;
        tb.appendChild(el("tr", {}, [
          el("td", { class: "text-secondary text-nowrap" }, String(r.published_at).slice(5, 10)),
          el("td", { class: "text-secondary" }, CAT_KO[r.category] || r.category),
          el("td", { class: "text-nowrap" }, r.stock_name ? `${r.stock_name}` : "—"),
          el("td", { html: `<a href="${link}" target="_blank" rel="noopener">${
            String(r.title).replace(/[<>&]/g, "")}</a>` +
            (r.source === "news" ? ' <span class="badge text-bg-secondary">언론인용</span>' : "") }),
          el("td", { class: "text-nowrap" }, r.broker),
          el("td", { class: "text-end text-nowrap" },
             r.target_price ? Number(r.target_price).toLocaleString() : "—"),
          el("td", { class: "text-secondary" }, r.opinion || "—"),
        ]));
      }
      t.appendChild(tb);
      repBody.appendChild(el("div", { class: "table-responsive", style: "max-height:420px; overflow-y:auto" }, t));
    };

    brkAddBtn.onclick = async () => {
      const name = brkAddName.value.trim();
      if (!name) { alert("증권사명을 입력하세요"); return; }
      const aliases = brkAddAlias.value.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        await postJSON("/api/tnm/brokers",
                       { name, kind: brkAddKind.value, aliases });
        brkAddName.value = ""; brkAddAlias.value = "";
        changed.invalidate("brokers"); await loadBrokers();
      } catch (e) { alert("실패: " + e.message); }
    };
    brkRunBtn.onclick = async () => {
      brkRunBtn.disabled = true;
      brkRunBtn.textContent = "수집 중…";
      try {
        const r = await postJSON("/api/tnm/research/run");
        changed.invalidate("brokers"); changed.invalidate("reports");
        await Promise.all([loadBrokers(), loadReports()]);
        alert(`수집 완료 — 조회 ${r.fetched ?? 0} · 저장 ${r.inserted ?? 0}` +
              ` · 분석큐 ${r.ingest_inserted ?? 0}` +
              (r.ingest_registered ? ` (신규 종목 ${r.ingest_registered})` : "") +
              (r.ingest_deferred ? ` · 대기 ${r.ingest_deferred}` : "") +
              ` · 제외(끔) ${r.disabled_skip ?? 0}`);
      } catch (e) { alert("실패: " + e.message); }
      finally { brkRunBtn.disabled = false; brkRunBtn.textContent = "지금 수집"; }
    };

    // ==================================================================
    // ④ Shadow 지표 (주별 정밀도/재현율 — 실운영 전환 판정)
    // ==================================================================
    const metC = card("Shadow 검증 지표", null, { wide: true, icon: "bi-clipboard-check" });
    metC.col.className = "col-12";
    row.appendChild(metC.col);
    const metBody = el("div", { class: "small" });
    metC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        "뉴스 모니터에서 단 [중요]/[불필요] 라벨 기준 — 재현율 ≥ 0.9 그리고 정밀도 ≥ 0.6 을 만족하면 실운영(알림 발송) 전환"),
      metBody);
    const loadMetrics = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/metrics?weeks=4"); } catch (e) { return; }
      if (!changed("metrics", d)) return;
      metBody.innerHTML = "";
      const weeks = d.weeks || [];
      if (!weeks.length) {
        metBody.appendChild(el("div", { class: "text-secondary" },
          "아직 라벨 없음 — 뉴스 모니터에서 판정에 라벨을 달면 주별 지표가 계산됩니다"));
        return;
      }
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>주(월요일)</th><th>라벨</th><th>중요</th><th>재현율</th><th>정밀도</th><th>판정</th></tr>" }));
      const tb = el("tbody");
      for (const w of weeks) {
        tb.appendChild(el("tr", {
          html: `<td>${w.week}</td><td>${w.labeled}</td><td>${w.important}</td>` +
            `<td>${w.recall ?? "-"}</td><td>${w.precision ?? "-"}</td>` +
            `<td>${w.pass ? '<span class="badge text-bg-success">기준 충족</span>'
                          : '<span class="badge text-bg-secondary">미충족/표본 부족</span>'}</td>`,
        }));
      }
      t.appendChild(tb);
      metBody.appendChild(el("div", { class: "table-responsive" }, t));
    };

    // ==================================================================
    // ④ 연동 설정 (기어 모달) — API 키 7종 + 운영 트리거 4종
    // 종전에는 DART 키 하나만 입력할 수 있었고 나머지는 서버 .env 를 직접
    // 고쳐야 했다. 백엔드는 이미 7종을 다 받고 있었다.
    // ==================================================================
    const KEYS = [
      ["dart_api_key", "DART API 키", "공시 수집. 없으면 뉴스만 모은다", "password"],
      ["naver_client_id", "네이버 Client ID", "없으면 구글 RSS 만 쓴다", "text"],
      ["naver_client_secret", "네이버 Client Secret", "", "password"],
      ["ollama_url", "LLM 게이트웨이 URL", "분류·임베딩 경유 주소", "text"],
      ["ollama_fallback_url", "LLM 대체 URL", "주 경로 장애 시", "text"],
      ["slack_bot_token", "슬랙 봇 토큰", "알림 발송", "password"],
      ["slack_channel", "슬랙 채널", "예: #alerts", "text"],
    ];
    const keyStat = el("div", { class: "row g-2 mb-3" });
    const keyInputs = {};
    const keyMsg = el("div", { class: "small mt-2" });
    const keySave = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "입력한 값 저장");
    keySave.onclick = async () => {
      const payload = {};
      for (const [k] of KEYS) {
        const v = keyInputs[k].value.trim();
        if (v) payload[k] = v;
      }
      if (!Object.keys(payload).length) {
        keyMsg.innerHTML = '<span class="text-secondary">입력된 값이 없습니다 — 바꿀 항목만 채우세요</span>';
        return;
      }
      try {
        await postJSON("/api/tnm/settings", payload);
        for (const k of Object.keys(payload)) keyInputs[k].value = "";
        changed.invalidate("keys"); await loadKeys();
        keyMsg.innerHTML = `<span class="text-success">${Object.keys(payload).length}개 저장됨 — 다음 수집 주기부터 반영됩니다</span>`;
      } catch (e) { keyMsg.innerHTML = `<span class="text-danger">실패: ${e.message}</span>`; }
    };

    // 운영 트리거 — 주기를 기다리지 않고 지금 돌린다
    const TRIGGERS = [
      ["collect/run", "수집 지금 실행", "DART·뉴스를 즉시 한 바퀴", "outline-primary"],
      ["promote/run", "편입 지금 실행", "고점수 종목을 감시목록으로", "outline-primary"],
      ["reclassify_failed", "실패분 재분류", "llm_failed 레코드를 다시 큐에", "outline-warning"],
      ["notify/test", "슬랙 테스트 발송", "채널 연결 확인", "outline-secondary"],
    ];
    const trigMsg = el("div", { class: "small mt-2" });
    const trigRow = el("div", { class: "d-flex gap-2 flex-wrap" }, TRIGGERS.map(
      ([path, label, desc, tone]) => {
        const b = el("button", { class: `btn btn-sm btn-${tone}`, type: "button", title: desc }, label);
        b.onclick = async () => {
          b.disabled = true;
          trigMsg.textContent = `${label}… 실행 중`;
          try {
            const r = await postJSON(`/api/tnm/${path}`);
            trigMsg.innerHTML = `<span class="text-success">${label}: ${r.message || JSON.stringify(r)}</span>`;
            changed.invalidate("status"); await loadStatus();
          } catch (e) { trigMsg.innerHTML = `<span class="text-danger">${label} 실패: ${e.message}</span>`; }
          finally { b.disabled = false; }
        };
        return b;
      }));

    gear.body.append(
      el("div", { class: "fw-semibold small mb-1" }, "연동 상태"),
      keyStat,
      el("div", { class: "fw-semibold small mb-1" }, "API 키 · 주소"),
      el("div", { class: "small text-secondary mb-2", html:
        "바꿀 항목만 채우고 저장하세요. 빈 칸은 <b>변경 없음</b>입니다. " +
        "키는 서버 .env(권한 600)에만 저장되고 화면에는 마스킹되어 표시됩니다." }),
      el("div", { class: "row g-2" }, KEYS.map(([k, label, hint, type]) => {
        const inp = el("input", { class: "form-control form-control-sm", type,
                                  placeholder: "변경할 때만 입력" });
        keyInputs[k] = inp;
        return el("div", { class: "col-12 col-md-6" }, [
          el("label", { class: "form-label small mb-1" }, label),
          inp,
          hint ? el("div", { class: "text-secondary", style: "font-size:.72rem" }, hint) : null,
        ]);
      })),
      el("div", { class: "mt-2" }, keySave), keyMsg,
      el("hr", { class: "my-3" }),
      el("div", { class: "fw-semibold small mb-1" }, "운영 트리거"),
      el("div", { class: "small text-secondary mb-2" },
        "주기를 기다리지 않고 지금 한 바퀴 돌린다. 결과는 파이프라인 상태 카드에 반영된다."),
      trigRow, trigMsg,
    );

    const loadKeys = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/settings"); } catch (e) { return; }
      if (!changed("keys", d)) return;
      keyStat.innerHTML = "";
      const stat = (k, v, ok) => el("div", { class: "col-6 col-md-4" },
        el("div", { class: "border rounded p-2 h-100" }, [
          el("div", { class: "text-secondary small" }, k),
          el("div", { class: "fw-semibold small " + (ok ? "text-success" : "text-secondary") }, v),
        ]));
      keyStat.append(
        stat("DART 키", d.dart_key || "미설정", !!d.dart_key),
        stat("네이버 API", d.naver_enabled ? "활성" : "미설정 (구글 RSS 만)", d.naver_enabled),
        stat("LLM 게이트웨이", d.llm_gateway || d.ollama_url || "미설정",
             !!(d.llm_gateway || d.ollama_url)),
        stat("LLM 대체", d.ollama_fallback_url || "없음", !!d.ollama_fallback_url),
        stat("슬랙", d.slack_token ? (d.slack_channel || "채널 미설정") : "미설정", !!d.slack_token),
        stat("DB", d.db_configured ? "설정됨" : "미설정", d.db_configured),
      );
    };

    await Promise.all([loadStatus(), loadWatch(), loadBrokers(), loadReports(),
                       loadKeys(), loadMetrics()]);
    ctx.addTimer(setInterval(() => {
      loadStatus(); loadWatch(); loadBrokers(); loadReports(); loadMetrics();
    }, 60_000));
  },
};
