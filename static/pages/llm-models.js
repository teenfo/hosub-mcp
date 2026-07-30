import { fetchJSON, el, badge } from "../app.js";

// LLM 모델 페이지: 맥에 설치된 모델(용량·사용 이력·삭제) + 카탈로그 검색·설치.
//
// llm.js 와 나눈 이유: 한 페이지에 카드 5개는 너무 많고, 이쪽은 상태 조회가
// 아니라 **변경 작업**이라 갱신 주기도 성격도 다르다.
//
// 모든 변경은 게이트웨이의 /v1/admin/* 을 거친다. 그 경로는 Caddy 가 공개
// 노출에서 404 로 잘라내므로 서버 안(대시보드)에서만 닿는다.

async function send(path, { method = "POST", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  return res.json();
}

const gb = (v) => (v || v === 0 ? `${Number(v).toFixed(1)}GB` : "-");

function agoText(iso) {
  if (!iso) return "사용 기록 없음";
  const days = (Date.now() - new Date(iso)) / 86400000;
  if (days < 1) return "오늘";
  if (days < 30) return `${Math.floor(days)}일 전`;
  return "30일 이상";
}

export default {
  id: "llm-models",
  title: "LLM 모델",
  icon: "bi-boxes",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // --- 설치된 모델 ---
    const instCol = el("div", { class: "col-12" });
    const instCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2" }, [
        el("span", { html: '<i class="bi bi-hdd"></i> 맥에 설치된 모델' }),
        el("span", { class: "badge text-bg-secondary d-none", id: "mdl-total" }),
        el("button", {
          class: "btn btn-sm btn-outline-secondary ms-auto", type: "button",
          id: "mdl-refresh", title: "새로 고침",
          html: '<i class="bi bi-arrow-clockwise"></i>',
        }),
      ]),
    ]);
    const instBody = el("div", { class: "card-body" });
    instCard.appendChild(instBody);
    instCol.appendChild(instCard);
    row.appendChild(instCol);

    // --- 카탈로그 검색 · 설치 ---
    const catCol = el("div", { class: "col-12" });
    const catCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-search"></i> 모델 찾기 · 설치' }),
        el("span", { class: "small text-secondary ms-auto" },
          "목록에 없으면 이름을 직접 입력하세요 (예: qwen3:4b)"),
      ]),
    ]);
    const catBody = el("div", { class: "card-body" });
    const q = el("input", {
      class: "form-control form-control-sm", id: "mdl-q", type: "search",
      placeholder: "모델 이름·설명 검색", style: "max-width:260px",
    });
    const manual = el("input", {
      class: "form-control form-control-sm", id: "mdl-manual",
      placeholder: "직접 입력: qwen3:4b", style: "max-width:240px",
    });
    const manualBtn = el("button",
      { class: "btn btn-sm btn-outline-primary", type: "button" }, "이 이름으로 설치");
    catBody.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-3 flex-wrap" },
      [q, el("span", { class: "vr d-none d-md-block" }), manual, manualBtn]));
    const catList = el("div");
    catBody.appendChild(catList);
    catCard.appendChild(catBody);
    catCol.appendChild(catCard);
    row.appendChild(catCol);

    // --- 통합 가이드 (소비자에게 전달하는 URL) ---
    //
    // 소비 프로젝트(roxlogy 등)는 레포가 아니라 이 URL 을 참조한다. 계약이 두 곳에
    // 있으면 반드시 어긋나므로, 문서를 복사해 주지 말고 URL 을 주는 것이 맞다.
    const docCol = el("div", { class: "col-12" });
    const docCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-link-45deg"></i> 통합 가이드 URL' }),
        el("span", { class: "badge text-bg-secondary d-none", id: "doc-size" }),
        el("span", { class: "small text-secondary ms-auto" },
          "소비 프로젝트에 이 URL 을 주면 항상 최신 계약을 받습니다"),
      ]),
    ]);
    const docBody = el("div", { class: "card-body" });
    docCard.appendChild(docBody);
    docCol.appendChild(docCard);
    row.appendChild(docCol);

    // --- A/B 비교 ---
    const abCol = el("div", { class: "col-12" });
    const abCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-speedometer2"></i> 모델 비교' }),
        el("span", { class: "small text-secondary ms-auto" },
          "같은 프롬프트를 두 모델에 돌려 tok/s 를 잰다 — 워밍업 후 측정"),
      ]),
    ]);
    const abBody = el("div", { class: "card-body" });
    const abA = el("select", { class: "form-select form-select-sm", style: "max-width:200px" });
    const abB = el("select", { class: "form-select form-select-sm", style: "max-width:200px" });
    const abPrompt = el("textarea", {
      class: "form-control form-control-sm mb-2", rows: "3",
      placeholder: "두 모델에 똑같이 보낼 프롬프트",
    });
    const abSystem = el("input", {
      class: "form-control form-control-sm mb-2",
      placeholder: "system (선택) — 양쪽 동일하게 적용됩니다",
    });
    const abRun = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "비교 실행");
    const abOut = el("div", { class: "mt-3" });
    abBody.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-2 flex-wrap" }, [
      el("span", { class: "small text-secondary" }, "A"), abA,
      el("span", { class: "small text-secondary" }, "B"), abB, abRun,
    ]));
    abBody.appendChild(abSystem);
    abBody.appendChild(abPrompt);
    abBody.appendChild(abOut);
    abCard.appendChild(abBody);
    abCol.appendChild(abCard);
    row.appendChild(abCol);

    const alertBox = (tone, title, detail) =>
      el("div", { class: `alert alert-${tone} mb-0` }, [
        el("div", { class: "fw-medium" }, title),
        detail ? el("div", { class: "small mt-1" }, detail) : null,
      ]);

    const spinner = (msg) =>
      el("div", { class: "d-flex align-items-center gap-2 text-secondary small" }, [
        el("span", { class: "spinner-border spinner-border-sm" }), msg,
      ]);

    // --- 삭제 ---
    const removeModel = async (m, btn) => {
      const warn = m.roles.length
        ? `\n\n⚠️ 이 모델을 쓰는 역할이 있습니다: ${m.roles.join(", ")}`
        : "";
      if (!window.confirm(
        `'${m.name}' 을(를) 맥에서 지울까요?\n`
        + `되돌리려면 ${gb(m.size_gb)} 를 다시 내려받아야 합니다.${warn}`)) return;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
      try {
        const r = await send(`/api/llm/models/delete?model=${encodeURIComponent(m.name)}`,
          { method: "DELETE" });
        if (r.status !== "ok" && r.status !== "deleted") {
          window.alert(r.detail || r.error || r.reason || "삭제하지 못했습니다.");
        }
      } finally {
        await loadInstalled();
      }
    };

    // --- 설치 ---
    const installModel = async (name, btn) => {
      if (!window.confirm(`'${name}' 을(를) 맥에 설치할까요?\n`
        + "다운로드는 백그라운드로 진행되며 진행률은 LLM 페이지에서 볼 수 있습니다.")) return;
      if (btn) {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
      }
      try {
        const r = await send("/api/llm/models/install", { body: { model: name } });
        if (r.status === "already_installed") {
          window.alert(`'${name}' 은(는) 이미 설치되어 있습니다.`);
        } else if (r.status !== "ok" && r.status !== "approved") {
          window.alert(r.detail || r.error || r.reason || "설치 요청에 실패했습니다.");
        }
      } finally {
        await Promise.all([loadInstalled(), loadCatalog()]);
      }
    };

    // --- 설치된 모델 표 ---
    const loadInstalled = async () => {
      const d = await fetchJSON("/api/llm/installed");
      instBody.innerHTML = "";
      const totalBadge = instCard.querySelector("#mdl-total");

      if (d.status === "error" || d.status === "unconfigured") {
        totalBadge.classList.add("d-none");
        instBody.appendChild(alertBox("warning", "게이트웨이에 연결할 수 없습니다",
          d.error || d.reason || d.hint));
        return;
      }
      const list = d.models || [];
      totalBadge.textContent = `${list.length}개 · ${gb(d.total_size_gb)}`;
      totalBadge.classList.remove("d-none");

      if (d.backend && d.backend.online === false) {
        instBody.appendChild(alertBox("warning", "맥이 응답하지 않습니다 — 마지막으로 본 목록입니다",
          d.backend.error));
      }
      if (!list.length) {
        instBody.appendChild(el("div", { class: "text-secondary small" },
          "설치된 모델이 없습니다."));
        return;
      }

      const rows = list.map((m) => {
        const actions = el("td", { class: "text-end" });
        if (m.blockers.length) {
          const why = m.blockers.map((b) => b.message).join(" ");
          const embed = m.blockers.some((b) => (b.embed_roles || []).length);
          actions.appendChild(el("span", {
            class: "small text-secondary", title: why,
          }, embed ? "임베딩 사용 중" : "사용 중"));
        } else {
          const del = el("button",
            { class: "btn btn-sm btn-outline-danger", type: "button" }, "삭제");
          del.addEventListener("click", () => removeModel(m, del));
          actions.appendChild(del);
        }

        const meta = [m.parameter_size, m.quantization].filter(Boolean).join(" · ");
        return el("tr", {}, [
          el("td", { class: "mono small" }, [
            el("div", {}, m.name),
            meta ? el("div", { class: "text-secondary" }, meta) : null,
          ]),
          el("td", { class: "small" }, gb(m.size_gb)),
          el("td", { class: "small text-secondary" },
            m.roles.length ? m.roles.join(", ") : "-"),
          el("td", { class: "small text-secondary" },
            `${m.calls_30d || 0}회 · ${agoText(m.last_used)}`),
          actions,
        ]);
      });

      const thead = el("thead", {}, el("tr", {}, [
        el("th", {}, "모델"), el("th", {}, "용량"), el("th", {}, "쓰는 역할"),
        el("th", {}, "최근 30일"), el("th", {}, ""),
      ]));
      instBody.appendChild(el("div", { class: "table-responsive" },
        el("table", { class: "table table-sm table-hover align-middle mb-0" },
          [thead, el("tbody", {}, rows)])));
      instBody.appendChild(el("div", { class: "small text-secondary mt-2" },
        "쓰는 역할이 남아 있으면 지울 수 없습니다 — 지워도 다음 요청에서 "
        + "곧바로 재설치 대기가 걸리기 때문입니다. 역할의 모델을 먼저 바꾸세요."));
      fillAbSelects(list.map((m) => m.name));
    };

    // --- 카탈로그 ---
    const loadCatalog = async () => {
      const query = encodeURIComponent(q.value.trim());
      const d = await fetchJSON(`/api/llm/catalog?q=${query}`);
      catList.innerHTML = "";

      if (d.status === "error" || d.status === "unconfigured") {
        catList.appendChild(alertBox("warning", "카탈로그를 불러올 수 없습니다",
          d.error || d.reason));
        return;
      }
      if (d.error) {
        catList.appendChild(alertBox("warning", "카탈로그 파일에 오류가 있습니다",
          `${d.error} — 직전 정상본을 보여 줍니다.`));
      }
      const installed = d.installed || [];
      const budget = d.mem_budget_gb || 0;
      const models = d.models || [];
      if (!models.length) {
        catList.appendChild(el("div", { class: "text-secondary small" },
          "검색 결과가 없습니다. 이름을 직접 입력해 설치할 수 있습니다."));
        return;
      }

      for (const m of models) {
        const tags = el("div", { class: "d-flex flex-wrap gap-2 mt-2" });
        for (const t of m.tags) {
          const have = installed.some((i) => i === t.tag || i.startsWith(t.tag + ":"));
          const tooBig = budget && t.size_gb && t.size_gb > budget;
          const btn = el("button", {
            class: `btn btn-sm ${have ? "btn-outline-secondary" : "btn-outline-primary"}`,
            type: "button",
            disabled: have || tooBig ? "disabled" : null,
            title: tooBig ? `메모리 예산 ${budget}GB 초과` : "",
          }, `${t.tag} · ${gb(t.size_gb)}${have ? " ✓" : ""}`);
          if (!have && !tooBig) btn.addEventListener("click", () => installModel(t.tag, btn));
          tags.appendChild(btn);
        }
        catList.appendChild(el("div", { class: "border rounded p-2 mb-2" }, [
          el("div", { class: "d-flex align-items-center gap-2 flex-wrap" }, [
            el("span", { class: "fw-medium mono" }, m.name),
            m.kinds.includes("embed") ? badge("임베딩", "info") : null,
            el("span", { class: "small text-secondary" }, m.description || ""),
          ]),
          tags,
        ]));
      }
    };

    // --- 통합 가이드 렌더 ---
    const copyBtn = (text, label = "복사") => {
      const b = el("button",
        { class: "btn btn-sm btn-outline-secondary", type: "button" }, label);
      b.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          b.textContent = "복사됨";
        } catch (e) {
          b.textContent = "복사 실패";
        }
        setTimeout(() => { b.textContent = label; }, 1500);
      });
      return b;
    };

    const urlRow = (label, url, note) =>
      el("div", { class: "mb-3" }, [
        el("div", { class: "small text-secondary mb-1" }, label),
        el("div", { class: "d-flex gap-2 align-items-center flex-wrap" }, [
          el("code", {
            class: "mono border rounded px-2 py-1 flex-grow-1",
            style: "word-break:break-all",
          }, url),
          copyBtn(url),
        ]),
        note ? el("div", { class: "form-text small" }, note) : null,
      ]);

    const renderDocCard = () => {
      docBody.innerHTML = "";

      // 공개 URL 은 이 대시보드가 열려 있는 origin 에서 유도한다 — Caddy 가
      // 대시보드와 /llm 을 같은 호스트로 서빙하므로 공개 주소로 볼 때 정확하다.
      // (127.0.0.1:8701 로 직접 열었다면 그 주소가 나오므로 아래에 명시한다)
      const publicUrl = `${window.location.origin}/llm/v1/integration`;
      const isLocal = /^https?:\/\/(127\.0\.0\.1|localhost)/.test(window.location.origin);

      docBody.appendChild(urlRow(
        "소비자에게 전달할 URL (Bearer 토큰 필요)", publicUrl,
        isLocal
          ? "⚠️ 지금 대시보드를 로컬 주소로 열었기 때문에 위 주소도 로컬입니다. "
            + "외부에 줄 주소는 공인 도메인(https://…/llm/v1/integration)입니다."
          : "이 대시보드와 같은 호스트 기준입니다."));

      docBody.appendChild(urlRow(
        "서버 내부용 (hosub·TNM·trading)", "http://127.0.0.1:8603/v1/integration",
        "Caddy 를 거치지 않습니다. 관리 API(/v1/admin/*)도 이 주소로만 닿습니다."));

      const curl = `curl -H "Authorization: Bearer $LLMGW_TOKEN" \\\n     ${publicUrl}`;
      docBody.appendChild(el("div", { class: "mb-3" }, [
        el("div", { class: "small text-secondary mb-1" }, "전달용 명령"),
        el("pre", {
          class: "border rounded p-2 mb-1 small mono",
          style: "white-space:pre-wrap; word-break:break-all",
        }, curl),
        el("div", { class: "d-flex gap-2 align-items-center flex-wrap" }, [
          copyBtn(curl, "명령 복사"),
          el("span", { class: "form-text small mb-0" },
            "토큰 값은 표시하지 않습니다 — .env 의 서비스별 토큰을 쓰세요."),
        ]),
      ]));

      // 문서 원문 — 접힌 상태로 두고 눌렀을 때만 가져온다.
      // 20KB 가 넘어 기본 노출하면 페이지를 잡아먹는다.
      const viewBtn = el("button",
        { class: "btn btn-sm btn-primary", type: "button" }, "문서 보기");
      const docOut = el("div", { class: "mt-2" });
      docBody.appendChild(el("div", { class: "d-flex gap-2 flex-wrap" }, [viewBtn]));
      docBody.appendChild(docOut);

      let shown = false;
      viewBtn.addEventListener("click", async () => {
        if (shown) {
          docOut.innerHTML = "";
          shown = false;
          viewBtn.textContent = "문서 보기";
          return;
        }
        viewBtn.disabled = true;
        docOut.innerHTML = "";
        docOut.appendChild(spinner("게이트웨이에서 문서를 가져오는 중…"));
        try {
          const d = await fetchJSON("/api/llm/integration");
          docOut.innerHTML = "";
          if (d.status !== "ok") {
            docOut.appendChild(alertBox("warning", "문서를 가져오지 못했습니다",
              d.error || d.reason || d.hint));
            return;
          }
          const sizeBadge = docCard.querySelector("#doc-size");
          sizeBadge.textContent = `${(d.bytes / 1024).toFixed(1)}KB`;
          sizeBadge.classList.remove("d-none");
          // 표·코드펜스가 많은 문서라 마크다운 렌더 대신 원문을 그대로 보여준다.
          // 반쯤 깨진 표보다 원문이 읽기 쉽고, 그대로 복사해 쓸 수도 있다.
          docOut.appendChild(el("pre", {
            class: "border rounded p-3 mb-0 small mono",
            style: "white-space:pre-wrap; word-break:break-word; "
              + "max-height:520px; overflow:auto",
          }, d.markdown || ""));
          docOut.appendChild(el("div", { class: "mt-2" },
            copyBtn(d.markdown || "", "문서 전체 복사")));
          shown = true;
          viewBtn.textContent = "접기";
        } catch (e) {
          docOut.innerHTML = "";
          if (e.message !== "unauthorized") {
            docOut.appendChild(alertBox("danger", "문서 조회 실패", e.message));
          }
        } finally {
          viewBtn.disabled = false;
        }
      });
    };

    // --- A/B 비교 실행 ---
    const fillAbSelects = (names) => {
      for (const [sel, def] of [[abA, 0], [abB, 1]]) {
        const cur = sel.value;
        sel.innerHTML = "";
        for (const n of names) sel.appendChild(el("option", { value: n }, n));
        if (cur && names.includes(cur)) sel.value = cur;
        else if (names[def]) sel.value = names[def];
      }
    };

    const sideCard = (label, s) => {
      const m = s.metrics || {};
      const stats = s.status === "succeeded"
        ? [
            m.tokens_per_sec ? `${m.tokens_per_sec} tok/s` : null,
            m.eval_count ? `${m.eval_count} 토큰` : null,
            m.total_duration_ms ? `${(m.total_duration_ms / 1000).toFixed(1)}s` : null,
          ].filter(Boolean).join(" · ")
        : (s.error || "실행 중…");
      return el("div", { class: "col-12 col-lg-6" },
        el("div", { class: "border rounded p-2 h-100" }, [
          el("div", { class: "d-flex align-items-center gap-2 flex-wrap" }, [
            el("span", { class: "badge text-bg-secondary" }, label),
            el("span", { class: "mono fw-medium" }, s.model),
            // 콜드/웜은 따로 표기한다 — 로드 시간을 속도에 섞으면 비교가 무의미해진다
            m.cold ? badge(`콜드 로드 ${(m.load_duration_ms / 1000).toFixed(1)}s`, "warning")
                   : (s.status === "succeeded" ? badge("웜", "success") : null),
          ]),
          el("div", { class: "small text-secondary mt-1" }, stats),
          el("pre", {
            class: "border rounded p-2 mt-2 mb-0 small",
            style: "white-space:pre-wrap; word-break:break-word; max-height:240px; overflow:auto",
          }, s.response || ""),
        ]));
    };

    const showComparison = (d) => {
      abOut.innerHTML = "";
      if (!d.sides) {
        abOut.appendChild(alertBox("warning", "비교를 시작하지 못했습니다",
          d.detail || d.error || d.reason));
        return;
      }
      if (!d.done) {
        abOut.appendChild(spinner(
          "각 모델을 워밍업한 뒤 측정합니다 — 실사용 잡보다 뒤에서 실행됩니다"));
      }
      const a = d.sides.a, b = d.sides.b;
      abOut.appendChild(el("div", { class: "row g-2 mt-1" },
        [sideCard("A", a), sideCard("B", b)]));
      if (d.done) {
        const ta = (a.metrics || {}).tokens_per_sec;
        const tb = (b.metrics || {}).tokens_per_sec;
        if (ta && tb) {
          const [fast, slow] = ta >= tb ? [a, b] : [b, a];
          const ratio = (Math.max(ta, tb) / Math.min(ta, tb)).toFixed(2);
          abOut.appendChild(el("div", { class: "alert alert-info mt-2 mb-0 small" },
            `${fast.model} 이(가) ${slow.model} 보다 ${ratio}배 빠릅니다 `
            + "(모델 로드 시간 제외, 출력 품질은 직접 비교하세요)."));
        }
      }
    };

    abRun.addEventListener("click", async () => {
      const prompt = abPrompt.value.trim();
      if (!prompt) return;
      if (abA.value === abB.value) {
        window.alert("서로 다른 모델을 골라 주세요.");
        return;
      }
      abRun.disabled = true;
      abRun.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 실행 중';
      abOut.innerHTML = "";
      try {
        const body = { prompt, models: [abA.value, abB.value] };
        if (abSystem.value.trim()) body.system = abSystem.value;
        let d = await send("/api/llm/compare", { body });
        showComparison(d);
        const runId = (d.run || {}).id;
        for (let i = 0; runId && !d.done && i < 300; i++) {
          await new Promise((r) => setTimeout(r, 2000));
          d = await fetchJSON(`/api/llm/compare/${runId}`);
          showComparison(d);
        }
      } catch (e) {
        if (e.message !== "unauthorized") {
          abOut.appendChild(alertBox("danger", "비교 실패", e.message));
        }
      } finally {
        abRun.disabled = false;
        abRun.textContent = "비교 실행";
      }
    });

    manualBtn.addEventListener("click", () => {
      const name = manual.value.trim();
      if (!name) return;
      installModel(name, manualBtn);
    });
    manual.addEventListener("keydown", (e) => {
      if (e.key === "Enter") manualBtn.click();
    });

    let timer = null;
    q.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(loadCatalog, 250);
    });

    instCard.querySelector("#mdl-refresh").addEventListener("click", () =>
      Promise.all([loadInstalled(), loadCatalog()]));

    renderDocCard();   // 정적이라 폴링하지 않는다
    instBody.appendChild(spinner("맥에서 모델 목록을 읽는 중…"));
    await Promise.all([loadInstalled(), loadCatalog()]);
    // 설치가 끝나면 목록에 나타나야 하므로 주기 갱신(카탈로그는 정적이라 제외)
    ctx.addTimer(setInterval(loadInstalled, 30000));
  },
};
