import { fetchJSON, el, card } from "../app.js";
import { postJSON, makeChanged } from "./tradelib.js";

// TNM 설정: 관심종목(자동동기+수동편집) · API 키 상태.
// 관심종목은 트레이딩 감시목록·보유계좌가 30분마다 자동 동기화되고,
// 여기서 수동 추가/제외·종목별 임계값/일일상한을 편집한다.

const ORIGIN_BADGE = {
  trading: '<span class="badge text-bg-primary">감시목록</span>',
  holding: '<span class="badge text-bg-danger">보유</span>',
  manual: '<span class="badge text-bg-success">수동</span>',
};

export default {
  id: "tnm-settings",
  title: "TNM 설정",
  icon: "bi-gear",
  group: "TNM",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // --- 관심종목 ---
    const watchC = card("관심종목 (뉴스·공시 수집 대상)", null, { wide: true, icon: "bi-bookmark-star" });
    watchC.col.className = "col-12";
    row.appendChild(watchC.col);
    const addTicker = el("input", { class: "form-control form-control-sm", placeholder: "종목코드 6자리", style: "max-width:130px" });
    const addName = el("input", { class: "form-control form-control-sm", placeholder: "종목명", style: "max-width:160px" });
    const addBtn = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "수동 추가");
    const syncBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "지금 동기화");
    const watchBody = el("div", { class: "small mt-2" });
    watchC.body.append(
      el("div", { class: "small text-secondary mb-2" },
        "감시목록·보유는 자동 동기(30분). 제외한 종목은 동기화가 되살리지 않고, 수동 추가는 동기화가 건드리지 않습니다."),
      el("div", { class: "d-flex gap-2 flex-wrap" }, [addTicker, addName, addBtn, syncBtn]),
      watchBody);

    const loadWatch = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/watch"); } catch (e) { return; }
      if (!changed("watch", d)) return;
      watchBody.innerHTML = "";
      const entries = d.entries || [];
      const active = entries.filter((e) => e.is_active);
      const excluded = entries.filter((e) => e.is_excluded);
      watchBody.appendChild(el("div", { class: "text-secondary mb-1" },
        `활성 ${active.length} · 제외 ${excluded.length} · 전체 ${entries.length}`));
      const t = el("table", { class: "table table-sm align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>종목</th><th>출처</th><th>상태</th><th>임계값</th><th>일일상한</th><th></th></tr>" }));
      const tb = el("tbody");
      for (const e of entries) {
        const tr = el("tr", { class: e.is_active ? "" : "opacity-50" });
        tr.appendChild(el("td", {}, `${e.name} (${e.ticker})`));
        tr.appendChild(el("td", { html: ORIGIN_BADGE[e.origin] || e.origin }));
        tr.appendChild(el("td", {}, e.is_excluded ? "제외" : e.is_active ? "활성" : "비활성"));
        const th = el("input", { type: "number", class: "form-control form-control-sm", value: e.score_threshold, style: "width:75px" });
        const cap = el("input", { type: "number", class: "form-control form-control-sm", value: e.daily_alert_cap, style: "width:70px" });
        const save = el("button", { class: "btn btn-sm btn-outline-secondary py-0", type: "button" }, "저장");
        save.onclick = async () => {
          try {
            await postJSON(`/api/tnm/watch/${e.ticker}/settings`,
              { score_threshold: Number(th.value), daily_alert_cap: Number(cap.value) });
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

    // --- Shadow 지표 (주별 정밀도/재현율 — 실운영 전환 판정) ---
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

    // --- API 키 상태 ---
    const keyC = card("연동 상태 · API 키", null, { wide: true, icon: "bi-key" });
    keyC.col.className = "col-12";
    row.appendChild(keyC.col);
    const keyBody = el("div", { class: "small" });
    keyC.body.appendChild(keyBody);
    const loadKeys = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/settings"); } catch (e) { return; }
      if (!changed("keys", d)) return;
      keyBody.innerHTML = "";
      const stat = (k, v, ok) => el("div", { class: "col-6 col-md-3" },
        el("div", { class: "border rounded p-2" }, [
          el("div", { class: "text-secondary small" }, k),
          el("div", { class: "fw-semibold " + (ok ? "text-success" : "text-secondary") }, v),
        ]));
      keyBody.appendChild(el("div", { class: "row g-2 mb-2" }, [
        stat("DART 키", d.dart_key || "미설정", !!d.dart_key),
        stat("네이버 API", d.naver_enabled ? "활성" : "미설정 (구글 RSS 만)", d.naver_enabled),
        stat("Ollama (Mac)", d.ollama_url || "미설정", !!d.ollama_url),
        stat("슬랙", d.slack_token ? (d.slack_channel || "채널 미설정") : "미설정", !!d.slack_token),
      ]));
      const dartIn = el("input", { class: "form-control form-control-sm", placeholder: "DART API 키 입력", style: "max-width:300px" });
      const saveBtn = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "키 저장");
      saveBtn.onclick = async () => {
        if (!dartIn.value.trim()) return;
        try {
          await postJSON("/api/tnm/settings", { dart_api_key: dartIn.value.trim() });
          dartIn.value = ""; changed.invalidate("keys"); await loadKeys();
          alert("저장됨 — 다음 수집 주기부터 DART 공시가 수집됩니다");
        } catch (e) { alert("실패: " + e.message); }
      };
      const slackTest = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "슬랙 테스트 발송");
      slackTest.onclick = async () => {
        slackTest.disabled = true;
        try { await postJSON("/api/tnm/notify/test"); alert("발송 성공 — 슬랙 채널을 확인하세요"); }
        catch (e) { alert("실패: " + e.message); }
        finally { slackTest.disabled = false; }
      };
      keyBody.append(
        el("div", { class: "d-flex gap-2 align-items-center" }, [dartIn, saveBtn, slackTest]),
        el("div", { class: "text-secondary small mt-1" },
          "키는 서버 .env(권한 600)에만 저장되고 화면에는 마스킹되어 표시됩니다. Shadow 모드 해제(실알림)는 검증 후 설정 파일로 전환합니다."));
    };

    await Promise.all([loadWatch(), loadKeys(), loadMetrics()]);
    ctx.addTimer(setInterval(() => { loadWatch(); loadMetrics(); }, 60_000));
  },
};
