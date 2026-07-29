import { fetchJSON, el, card } from "../app.js";
import { postJSON, makeChanged, stockHTML } from "./tradelib.js";

// 뉴스 모니터 (TNM): 수집·분석 현황 + 판정 목록 + 상세/라벨링(Shadow 검증).
// 점수·판정은 룰 기반(결정론) — LLM 은 분류·요약만 담당 (매매판단 아님).

const DIR_LABEL = { positive: "긍정", negative: "부정", neutral: "중립", unclear: "불명" };
const HORIZON_LABEL = { immediate: "즉시", short: "단기", long: "장기", unclear: "불명" };
const NOVELTY_LABEL = { new: "신규", follow_up: "후속", duplicate: "재탕" };

// 소스 배지 — 같은 목록에 공시·뉴스·리포트가 섞여 있어 구분이 없으면 무엇을
// 보고 있는지 알 수 없다. 성격이 다르므로 색도 다르게 준다.
//   공시   사실 확정 (DART 원문)          → 가장 무겁다
//   리포트 애널리스트 분석 (목표가·의견)
//   뉴스   보도 (구글 RSS · 네이버)
const SOURCE_BADGE = {
  dart: ["공시", "text-bg-danger", "DART 전자공시 원문"],
  research: ["리포트", "text-bg-primary", "증권사 리서치 (네이버 금융·한경컨센서스)"],
  rss: ["뉴스", "text-bg-secondary", "구글 뉴스 RSS"],
  naver: ["뉴스", "text-bg-secondary", "네이버 뉴스 API"],
};

function sourceBadge(src) {
  const [label, cls, hint] = SOURCE_BADGE[src] || [src || "-", "text-bg-light", ""];
  return `<span class="badge ${cls}" title="${hint}">${label}</span>`;
}

function scoreBadge(score) {
  const s = score ?? 0;
  const cls = s >= 60 ? "text-bg-danger" : s >= 40 ? "text-bg-warning" : "text-bg-secondary";
  return `<span class="badge ${cls}">${s}</span>`;
}

export default {
  id: "tnm",
  title: "뉴스 모니터",
  icon: "bi-newspaper",
  group: "TNM",
  async render(container, ctx) {
    const changed = makeChanged();
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // --- 상태 바 ---
    const statusC = card("파이프라인 상태", null, { wide: true, icon: "bi-activity" });
    statusC.col.className = "col-12";
    row.appendChild(statusC.col);
    const statusBody = el("div", { class: "small" });
    statusC.body.appendChild(statusBody);

    const loadStatus = async () => {
      let d;
      try { d = await fetchJSON("/api/tnm/status"); } catch (e) { return; }
      if (!changed("status", d)) return;
      statusBody.innerHTML = "";
      const q = d.queue || {};
      const badges = [
        d.shadow_mode ? '<span class="badge text-bg-info">Shadow 모드 — 알림 미발송</span>' : '<span class="badge text-bg-danger">실운영</span>',
        d.db_ready ? '<span class="badge text-bg-success">DB</span>' : '<span class="badge text-bg-danger">DB 미준비</span>',
        d.ollama ? `<span class="badge text-bg-success">Ollama ${d.ollama.replace("http://", "")}</span>` : '<span class="badge text-bg-secondary">Ollama 미연결</span>',
        d.dart_enabled ? '<span class="badge text-bg-success">DART</span>' : '<span class="badge text-bg-secondary">DART 키 없음</span>',
      ].join(" ");
      statusBody.appendChild(el("div", { class: "mb-2", html: badges }));
      // 숫자를 클릭하면 아래 목록 필터로 바로 조회 (판정 완료 항목만 목록化 가능)
      const chip = (label, status) => {
        const a = el("a", { href: "#", class: "text-decoration-none",
                            title: "클릭하면 목록에 필터 적용" }, label);
        a.onclick = (ev) => {
          ev.preventDefault();
          fStatus.value = status;
          // 다른 필터를 비운다. 칩이 '재탕 2,496' 이라고 약속했는데 기본
          // 최소점수(70)가 그대로 걸려 있으면 목록이 그보다 훨씬 적게 나온다 —
          // 숫자와 목록이 어긋나면 어느 쪽이 맞는지 알 수 없다.
          fScore.value = "";
          fSource.value = "";
          changed.invalidate("items");
          loadItems();
        };
        return a;
      };
      const plain = (label) => el("span", {
        title: "아직 판정 전이라 목록에 없음 — 처리되면 줄어듭니다" }, label);
      const parts = [
        chip(`수집 ${q.raw_total ?? "-"}건`, ""),
        plain(`임베딩 대기 ${q.embed_pending ?? "-"}`),
        plain(`판정 대기 ${q.dedup_pending ?? "-"}`),
        plain(`분류 대기 ${q.classify_pending ?? "-"}`),
        chip(`재탕 ${q.duplicates ?? "-"}`, "skipped_duplicate"),
        chip(`분류 실패 ${q.llm_failed ?? "-"}`, "llm_failed"),
      ];
      const line = el("div", { class: "text-secondary" });
      parts.forEach((p, i) => {
        if (i) line.appendChild(document.createTextNode(" · "));
        line.appendChild(p);
      });
      statusBody.appendChild(line);
    };

    // --- 판정 목록 + 상세 ---
    const listC = card("뉴스·공시 판정", null, { wide: true, icon: "bi-newspaper" });
    listC.col.className = "col-12 col-xxl-7";
    row.appendChild(listC.col);
    const detailC = card("상세 · 라벨링", null, { wide: true, icon: "bi-card-text" });
    detailC.col.className = "col-12 col-xxl-5";
    row.appendChild(detailC.col);

    const fDate = el("input", { type: "date", class: "form-control form-control-sm", style: "max-width:150px" });
    const fTicker = el("input", { class: "form-control form-control-sm", placeholder: "종목코드", style: "max-width:110px" });
    // 기본 최소점수 70. 정렬이 발행 최신순인데 뉴스가 물량으로 압도해서
    // (실측 뉴스 11,902 · 공시 567 · 리포트 49) 걸지 않으면 첫 화면 100건이
    // 전부 뉴스가 된다. 70을 걸면 뉴스 62 · 공시 29 · 리포트 9로 섞인다.
    // 그 아래 구간은 대부분 재탕 기사와 시황해설이다. 비우면 전체가 나온다.
    const DEFAULT_MIN_SCORE = "70";
    const fScore = el("input", { type: "number", class: "form-control form-control-sm",
                                 placeholder: "최소점수", value: DEFAULT_MIN_SCORE,
                                 title: "기본 70 — 비우면 전체", style: "max-width:100px" });
    const fStatus = el("select", { class: "form-select form-select-sm", style: "max-width:130px" }, [
      el("option", { value: "ok", selected: "selected" }, "정상 (기본)"),
      el("option", { value: "" }, "전체 상태"),
      el("option", { value: "llm_failed" }, "분류 실패"),
      el("option", { value: "skipped_duplicate" }, "재탕"),
    ]);
    const fSource = el("select", { class: "form-select form-select-sm", style: "max-width:130px" }, [
      el("option", { value: "" }, "전체 소스"),
      el("option", { value: "dart" }, "공시 (DART)"),
      el("option", { value: "news" }, "뉴스"),
      el("option", { value: "research" }, "증권사 리포트"),
    ]);
    const fBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "조회");
    const listBody = el("div", { class: "small mt-2" });
    listC.body.append(
      el("div", { class: "d-flex gap-2 flex-wrap" },
        [fDate, fTicker, fScore, fStatus, fSource, fBtn]),
      listBody);

    const detailBody = el("div", { class: "small" },
      el("div", { class: "text-secondary" }, "목록에서 항목을 선택하세요"));
    detailC.body.appendChild(detailBody);

    const showDetail = async (id) => {
      let d;
      try { d = await fetchJSON(`/api/tnm/items/${id}`); } catch (e) { return; }
      const it = d.item;
      detailBody.innerHTML = "";
      const sd = it.score_detail || {};
      detailBody.append(
        el("div", { class: "fw-semibold mb-1",
                    html: `${stockHTML(it.ticker, it.name)} · ${it.title}` }),
        el("div", { class: "mb-2", html:
          `${sourceBadge(it.source)} ${scoreBadge(it.score)} ` +
          `<span class="badge text-bg-light">${it.category || it.status}</span> ` +
          `<span class="badge text-bg-light">${DIR_LABEL[it.impact_direction] || "-"}(${HORIZON_LABEL[it.impact_horizon] || "-"})</span> ` +
          `<span class="badge text-bg-light">${NOVELTY_LABEL[it.novelty] || it.novelty || "-"}</span>` +
          (it.warn_hallucination ? ' <span class="badge text-bg-warning">수치 검증 필요</span>' : "") +
          (it.status === "llm_failed" ? ' <span class="badge text-bg-danger">분류 실패 — 원문 보존됨</span>' : "") }),
        el("div", { class: "mb-2" }, it.summary || "(요약 없음)"),
        it.reason ? el("div", { class: "text-secondary mb-2" }, "근거: " + it.reason) : el("span"),
        el("div", { class: "mb-2" }, el("a", { href: it.url, target: "_blank", rel: "noopener" }, "원문 보기 ↗")),
        el("div", { class: "text-secondary mb-2" },
          `점수 산식: 소스 ${sd.w_source ?? "-"} × 카테고리 ${sd.w_category ?? "-"} × 신규성 ${sd.w_novelty ?? "-"} × 확신 ${sd.confidence ?? "-"}` +
          (sd.non_material_factor && sd.non_material_factor !== 1 ? ` × 비중대 ${sd.non_material_factor}` : "") +
          ` · 모델 ${it.model_name || "-"} · ${it.latency_ms ?? "-"}ms`),
      );
      const note = el("input", { class: "form-control form-control-sm", placeholder: "메모(선택)", style: "max-width:220px" });
      const mkLabel = (verdict, text, cls) => {
        const b = el("button", { class: `btn btn-sm ${cls}`, type: "button" }, text);
        b.onclick = async () => {
          try {
            await postJSON(`/api/tnm/items/${id}/label`, { verdict, note: note.value });
            b.textContent = text + " ✓";
            changed.invalidate("items"); await loadItems();
          } catch (e) { alert("실패: " + e.message); }
        };
        return b;
      };
      detailBody.append(
        el("div", { class: "d-flex gap-2 align-items-center mt-2" }, [
          el("span", { class: "text-secondary" }, "사람 라벨:"),
          mkLabel("important", "중요", it.human_verdict === "important" ? "btn-danger" : "btn-outline-danger"),
          mkLabel("noise", "불필요", it.human_verdict === "noise" ? "btn-secondary" : "btn-outline-secondary"),
          note,
        ]),
        el("div", { class: "text-secondary small mt-1" },
          "Shadow 기간의 라벨이 임계값 검증(재현율 0.9·정밀도 0.6)의 근거가 됩니다"));
    };

    const loadItems = async () => {
      const params = new URLSearchParams();
      if (fDate.value) params.set("date", fDate.value);
      if (fTicker.value.trim()) params.set("ticker", fTicker.value.trim());
      if (fScore.value) params.set("min_score", fScore.value);
      if (fStatus.value) params.set("status", fStatus.value);
      if (fSource.value) params.set("source", fSource.value);
      params.set("limit", "100");
      let d;
      try { d = await fetchJSON("/api/tnm/items?" + params.toString()); } catch (e) { return; }
      if (!changed("items", d)) return;
      listBody.innerHTML = "";
      const items = d.items || [];
      if (!items.length) {
        listBody.appendChild(el("div", { class: "text-secondary" },
          "판정 항목 없음 — 분류는 Mac(Ollama) 연결 후 자동으로 쌓입니다"));
        return;
      }
      // 소스 구성을 목록 위에 먼저 보여준다 — 지금 무엇을 보고 있는지가
      // 표를 훑기 전에 드러나야 한다(리포트가 섞이면서 특히).
      const mix = items.reduce((a, it) => {
        const key = it.source === "naver" ? "rss" : it.source;
        a[key] = (a[key] || 0) + 1;
        return a;
      }, {});
      listBody.appendChild(el("div", { class: "mb-2", html:
        Object.entries(mix).sort((a, b) => b[1] - a[1])
          .map(([s, n]) => `${sourceBadge(s)} <span class="text-secondary me-2">${n}</span>`)
          .join(" ") +
        // 걸려 있는 필터를 화면에 적는다 — 안 보이면 "왜 이것밖에 없지" 가 된다
        (fScore.value
          ? `<span class="text-secondary">· 최소점수 ${fScore.value} 이상만 (비우면 전체)</span>`
          : "") }));
      const t = el("table", { class: "table table-sm table-hover align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>발행</th><th>소스</th><th>종목</th><th>점수</th><th>분류</th><th>제목</th><th>라벨</th></tr>" }));
      const tb = el("tbody");
      for (const it of items) {
        const tr = el("tr", { style: "cursor:pointer", html:
          `<td class="text-secondary text-nowrap">${(it.published_at || "").slice(5, 16).replace("T", " ")}</td>` +
          `<td>${sourceBadge(it.source)}</td>` +
          `<td class="text-nowrap">${stockHTML(it.ticker, it.name)}</td>` +
          `<td>${scoreBadge(it.score)}</td>` +
          `<td>${it.status === "ok" ? (it.category || "-") : (it.status === "llm_failed" ? "실패" : "재탕")}</td>` +
          `<td>${it.title}</td>` +
          `<td>${it.human_verdict === "important" ? "🔴 중요" : it.human_verdict === "noise" ? "⚪ 불필요" : ""}</td>` });
        tr.onclick = () => showDetail(it.id);
        tb.appendChild(tr);
      }
      t.appendChild(tb);
      listBody.appendChild(el("div", { class: "table-responsive", style: "max-height:520px; overflow-y:auto" }, t));
    };
    fBtn.onclick = () => { changed.invalidate("items"); loadItems(); };

    await Promise.all([loadStatus(), loadItems()]);
    ctx.addTimer(setInterval(() => { loadStatus(); loadItems(); }, 30_000));
  },
};
