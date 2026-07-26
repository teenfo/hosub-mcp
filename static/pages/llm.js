import { fetchJSON, el, badge } from "../app.js";

// LLM 게이트웨이 페이지: 백엔드 상태 + 역할(모델) 목록 + 간단 테스트 실행.
// 실제 추론은 Mac(Ollama)에서 돌고, hosub 는 역할 → 모델 라우팅만 한다.

async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  return res.json();
}

export default {
  id: "llm",
  title: "LLM",
  icon: "bi-robot",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    // --- 백엔드 상태 카드 ---
    const backendCol = el("div", { class: "col-12 col-xl-5" });
    const backendCard = el("div", { class: "card shadow-sm h-100" }, [
      el("div", { class: "card-header d-flex align-items-center" }, [
        el("span", { html: '<i class="bi bi-hdd-network"></i> 백엔드' }),
        el("button", {
          class: "btn btn-sm btn-outline-secondary ms-auto", type: "button",
          id: "llm-refresh", title: "새로 고침",
          html: '<i class="bi bi-arrow-clockwise"></i>',
        }),
      ]),
    ]);
    const backendBody = el("div", { class: "card-body" });
    backendCard.appendChild(backendBody);
    backendCol.appendChild(backendCard);
    row.appendChild(backendCol);

    // --- 역할 카드 ---
    const rolesCol = el("div", { class: "col-12 col-xl-7" });
    const rolesCard = el("div", { class: "card shadow-sm h-100" }, [
      el("div", { class: "card-header" }, el("span", { html: '<i class="bi bi-diagram-3"></i> 역할 → 모델' })),
    ]);
    const rolesBody = el("div", { class: "card-body" });
    rolesCard.appendChild(rolesBody);
    rolesCol.appendChild(rolesCard);
    row.appendChild(rolesCol);

    // --- 모델 설치 요청 카드 (게이트웨이가 만든 승인 대기 목록) ---
    // 조회 전용 대시보드의 의도된 예외: 여기서만 상태를 바꾼다.
    const reqCol = el("div", { class: "col-12 d-none" });
    const reqCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2" }, [
        el("span", { html: '<i class="bi bi-box-arrow-down"></i> 모델 설치 요청' }),
        el("span", { class: "badge text-bg-warning d-none", id: "llm-req-count" }),
        el("span", { class: "small text-secondary ms-auto" },
          "외부 서비스가 아직 맥에 없는 모델을 요청하면 여기에 쌓입니다"),
      ]),
    ]);
    const reqBody = el("div", { class: "card-body" });
    reqCard.appendChild(reqBody);
    reqCol.appendChild(reqCard);
    row.appendChild(reqCol);

    // --- 테스트 실행 카드 ---
    const tryCol = el("div", { class: "col-12" });
    const tryCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header" }, el("span", { html: '<i class="bi bi-play-circle"></i> 실행해 보기' })),
    ]);
    const tryBody = el("div", { class: "card-body" });
    const roleSelect = el("select", { class: "form-select form-select-sm", id: "llm-role", style: "max-width:220px" });
    const promptBox = el("textarea", {
      class: "form-control", id: "llm-prompt", rows: "4",
      placeholder: "예) 아래 로그에서 문제 원인을 알려줘 ...",
    });
    const runBtn = el("button", { class: "btn btn-primary btn-sm", type: "button", id: "llm-run" },
      "실행");
    const outBox = el("div", { class: "mt-3" });
    tryBody.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-2 flex-wrap" }, [
      el("span", { class: "small text-secondary" }, "역할"), roleSelect, runBtn,
    ]));
    tryBody.appendChild(promptBox);
    tryBody.appendChild(outBox);
    tryCard.appendChild(tryBody);
    tryCol.appendChild(tryCard);
    row.appendChild(tryCol);

    const spinner = (msg) =>
      el("div", { class: "d-flex align-items-center gap-2 text-secondary small" }, [
        el("span", { class: "spinner-border spinner-border-sm" }), msg,
      ]);

    const MR_STYLE = {
      pending: ["승인 대기", "warning"],
      approved: ["설치 예정", "info"],
      pulling: ["설치 중", "info"],
      ready: ["설치됨", "success"],
      rejected: ["거부됨", "secondary"],
      failed: ["설치 실패", "danger"],
    };

    const decide = async (model, action, btn) => {
      const verb = action === "approve" ? "설치" : "거부";
      if (!window.confirm(`모델 '${model}' 을(를) ${verb}할까요?`)) return;
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';
      try {
        const r = await postJSON("/api/llm/models/decide", { model, action });
        if (r.status !== "ok") {
          window.alert(r.error || r.reason || "요청을 처리하지 못했습니다.");
        }
      } finally {
        await loadRequests();
      }
    };

    const loadRequests = async () => {
      const d = await fetchJSON("/api/llm/models");
      const list = d.requests || [];
      // 게이트웨이가 아직 없고 요청도 없으면 카드를 숨긴다
      if (d.status === "unconfigured" && !list.length) {
        reqCol.classList.add("d-none");
        return;
      }
      reqCol.classList.remove("d-none");
      reqBody.innerHTML = "";

      const countBadge = reqCard.querySelector("#llm-req-count");
      const pending = list.filter((r) => r.status === "pending").length;
      countBadge.textContent = pending ? `승인 대기 ${pending}` : "";
      countBadge.classList.toggle("d-none", !pending);

      if (d.status === "error" || d.status === "unconfigured") {
        reqBody.appendChild(el("div", { class: "alert alert-warning mb-0" }, [
          el("div", {}, d.error || d.reason || "게이트웨이에 연결할 수 없습니다."),
          d.hint ? el("div", { class: "small text-secondary mt-1" }, d.hint) : null,
        ]));
        return;
      }
      if (!list.length) {
        reqBody.appendChild(el("div", { class: "text-secondary small" },
          "설치 요청이 없습니다."));
        return;
      }

      const rows = list.map((r) => {
        const [label, tone] = MR_STYLE[r.status] || [r.status, "secondary"];
        const statusCell = el("td", {}, [badge(label, tone)]);
        if (r.status === "pulling") {
          statusCell.appendChild(el("div", {
            class: "progress mt-1", style: "height:6px; min-width:90px",
          }, el("div", {
            class: "progress-bar progress-bar-striped progress-bar-animated",
            style: `width:${r.progress || 0}%`,
          })));
        } else if (r.error) {
          statusCell.appendChild(el("div", { class: "small text-danger mt-1" }, r.error));
        }

        const actions = el("td", { class: "text-end" });
        if (d.can_decide && (r.status === "pending" || r.status === "failed")) {
          const ok = el("button", { class: "btn btn-sm btn-primary me-1", type: "button" },
            r.status === "failed" ? "다시 설치" : "승인");
          ok.addEventListener("click", () => decide(r.model, "approve", ok));
          actions.appendChild(ok);
        }
        if (d.can_decide && r.status === "pending") {
          const no = el("button", { class: "btn btn-sm btn-outline-secondary", type: "button" }, "거부");
          no.addEventListener("click", () => decide(r.model, "reject", no));
          actions.appendChild(no);
        }
        if (d.can_decide && r.status === "rejected") {
          const re = el("button", { class: "btn btn-sm btn-outline-primary", type: "button" }, "승인으로 변경");
          re.addEventListener("click", () => decide(r.model, "approve", re));
          actions.appendChild(re);
        }

        return el("tr", {}, [
          el("td", { class: "mono small" }, r.model),
          el("td", { class: "small text-secondary" }, (r.roles || []).join(", ") || "-"),
          el("td", { class: "small text-secondary" }, r.requested_by || "-"),
          el("td", { class: "small text-secondary" },
            r.est_size_gb ? `~${r.est_size_gb}GB` : "-"),
          statusCell,
          actions,
        ]);
      });

      const thead = el("thead", {}, el("tr", {}, [
        el("th", {}, "모델"), el("th", {}, "역할"), el("th", {}, "요청 서비스"),
        el("th", {}, "크기"), el("th", {}, "상태"), el("th", {}, ""),
      ]));
      reqBody.appendChild(el("div", { class: "table-responsive" },
        el("table", { class: "table table-sm table-hover align-middle mb-0" },
          [thead, el("tbody", {}, rows)])));
    };

    let firstLoad = true;
    const load = async () => {
      // 백엔드가 꺼져 있으면 응답까지 몇 초 걸리므로 최초에는 로딩 표시
      if (firstLoad) {
        backendBody.appendChild(spinner("백엔드 확인 중…"));
        rolesBody.appendChild(spinner("불러오는 중…"));
        firstLoad = false;
      }
      const d = await fetchJSON("/api/llm/status");
      backendBody.innerHTML = "";
      rolesBody.innerHTML = "";

      if (!d.backends || !d.backends.length) {
        const msg = d.hint || "LLM 레지스트리가 비어 있습니다.";
        backendBody.appendChild(el("div", { class: "text-secondary small" }, msg));
        rolesBody.appendChild(el("div", { class: "text-secondary small" }, msg));
        return;
      }

      // 백엔드
      for (const b of d.backends) {
        const box = el("div", { class: "mb-3" }, [
          el("div", { class: "d-flex align-items-center gap-2" }, [
            el("span", { class: "fw-medium" }, b.name),
            b.online ? badge("online", "success") : badge("offline", "danger"),
          ]),
          el("div", { class: "mono text-secondary" }, b.base_url),
          b.online
            ? el("div", { class: "small text-secondary mt-1" }, `보유 모델 ${b.models.length}개`)
            : el("div", { class: "small text-danger mt-1" }, b.error || ""),
        ]);
        if (b.online && b.models.length) {
          const list = el("div", { class: "d-flex flex-wrap gap-1 mt-1" },
            b.models.map((m) => el("span", { class: "badge text-bg-secondary" }, m)));
          box.appendChild(list);
        }
        backendBody.appendChild(box);
      }

      // 역할
      if (!d.roles || !d.roles.length) {
        rolesBody.appendChild(el("div", { class: "text-secondary small" }, "정의된 역할이 없습니다."));
      } else {
        const rows = d.roles.map((r) =>
          el("tr", {}, [
            el("td", {}, [
              el("div", { class: "fw-medium" }, r.name),
              el("div", { class: "small text-secondary" }, r.description || ""),
            ]),
            el("td", { class: "mono small" }, r.model),
            el("td", { class: "small text-secondary" }, r.backend),
            el("td", {}, r.model_available ? badge("준비됨", "success") : badge("모델 없음", "warning")),
          ])
        );
        const thead = el("thead", {}, el("tr", {}, [
          el("th", {}, "역할"), el("th", {}, "모델"), el("th", {}, "백엔드"), el("th", {}, "상태"),
        ]));
        rolesBody.appendChild(el("div", { class: "table-responsive" },
          el("table", { class: "table table-sm table-hover align-middle mb-0" },
            [thead, el("tbody", {}, rows)])));

        // 역할 셀렉트 채우기 (선택 유지)
        const cur = roleSelect.value;
        roleSelect.innerHTML = "";
        for (const r of d.roles) roleSelect.appendChild(el("option", { value: r.name }, r.name));
        if (cur && d.roles.some((r) => r.name === cur)) roleSelect.value = cur;
        else if (d.fallback_role) roleSelect.value = d.fallback_role;
      }
    };

    backendCard.querySelector("#llm-refresh").addEventListener("click", () =>
      Promise.all([load(), loadRequests()]));

    runBtn.addEventListener("click", async () => {
      const prompt = promptBox.value.trim();
      if (!prompt) return;
      runBtn.disabled = true;
      runBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 실행 중';
      outBox.innerHTML = "";
      try {
        const r = await postJSON("/api/llm/generate", { role: roleSelect.value, prompt });
        if (r.status === "ok") {
          const meta = [r.model, r.total_duration_ms ? `${(r.total_duration_ms / 1000).toFixed(1)}s` : null]
            .filter(Boolean).join(" · ");
          outBox.appendChild(el("div", { class: "small text-secondary mb-1" }, meta));
          outBox.appendChild(el("pre", {
            class: "border rounded p-3 mb-0",
            style: "white-space:pre-wrap; word-break:break-word; max-height:420px; overflow:auto",
          }, r.response || ""));
        } else {
          outBox.appendChild(el("div", { class: "alert alert-warning mb-0" }, [
            el("div", { class: "fw-medium" }, r.reason || "실행 실패"),
            el("div", { class: "mono small mt-1" }, r.error || ""),
            r.hint ? el("div", { class: "small mt-2 text-secondary" }, r.hint) : null,
          ]));
        }
      } catch (e) {
        if (e.message !== "unauthorized") {
          outBox.appendChild(el("div", { class: "alert alert-danger mb-0" }, e.message));
        }
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = "실행";
      }
    });

    await Promise.all([load(), loadRequests()]);
    ctx.addTimer(setInterval(load, 30000));
    // 설치 진행률이 보이도록 요청 목록은 조금 더 자주
    ctx.addTimer(setInterval(loadRequests, 10000));
  },
};
