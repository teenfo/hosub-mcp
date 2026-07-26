import { fetchJSON, el, card } from "../app.js";
import { postJSON, makeChanged } from "./tradelib.js";

// 뉴스 모니터 (TNM): 수집·분석 현황 + 판정 목록 + 상세/라벨링(Shadow 검증).
// 점수·판정은 룰 기반(결정론) — LLM 은 분류·요약만 담당 (매매판단 아님).

const DIR_LABEL = { positive: "긍정", negative: "부정", neutral: "중립", unclear: "불명" };
const HORIZON_LABEL = { immediate: "즉시", short: "단기", long: "장기" };
const NOVELTY_LABEL = { new: "신규", follow_up: "후속", duplicate: "재탕" };

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
    const fScore = el("input", { type: "number", class: "form-control form-control-sm", placeholder: "최소점수", style: "max-width:100px" });
    const fStatus = el("select", { class: "form-select form-select-sm", style: "max-width:130px" }, [
      el("option", { value: "ok", selected: "selected" }, "정상 (기본)"),
      el("option", { value: "" }, "전체 상태"),
      el("option", { value: "llm_failed" }, "분류 실패"),
      el("option", { value: "skipped_duplicate" }, "재탕"),
    ]);
    const fBtn = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "조회");
    const listBody = el("div", { class: "small mt-2" });
    listC.body.append(
      el("div", { class: "d-flex gap-2 flex-wrap" }, [fDate, fTicker, fScore, fStatus, fBtn]),
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
        el("div", { class: "fw-semibold mb-1" }, `${it.name} (${it.ticker}) · ${it.title}`),
        el("div", { class: "mb-2", html:
          `${scoreBadge(it.score)} <span class="badge text-bg-light">${it.category || it.status}</span> ` +
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
      const t = el("table", { class: "table table-sm table-hover align-middle mb-0 small" });
      t.appendChild(el("thead", { html: "<tr><th>발행</th><th>종목</th><th>점수</th><th>분류</th><th>제목</th><th>라벨</th></tr>" }));
      const tb = el("tbody");
      for (const it of items) {
        const tr = el("tr", { style: "cursor:pointer", html:
          `<td class="text-secondary">${(it.published_at || "").slice(5, 16).replace("T", " ")}</td>` +
          `<td>${it.name}</td><td>${scoreBadge(it.score)}</td>` +
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
