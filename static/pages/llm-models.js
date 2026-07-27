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

    instBody.appendChild(spinner("맥에서 모델 목록을 읽는 중…"));
    await Promise.all([loadInstalled(), loadCatalog()]);
    // 설치가 끝나면 목록에 나타나야 하므로 주기 갱신(카탈로그는 정적이라 제외)
    ctx.addTimer(setInterval(loadInstalled, 30000));
  },
};
