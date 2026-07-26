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
      keyBody.append(
        el("div", { class: "d-flex gap-2 align-items-center" }, [dartIn, saveBtn]),
        el("div", { class: "text-secondary small mt-1" },
          "키는 서버 .env(권한 600)에만 저장되고 화면에는 마스킹되어 표시됩니다. Shadow 모드 해제(실알림)는 검증 후 설정 파일로 전환합니다."));
    };

    await Promise.all([loadWatch(), loadKeys()]);
    ctx.addTimer(setInterval(loadWatch, 60_000));
  },
};
