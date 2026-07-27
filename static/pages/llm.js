import { fetchJSON, el, badge } from "../app.js";

// LLM 게이트웨이 페이지: 백엔드 상태 + 역할(모델) 목록 + 간단 테스트 실행.
// 실제 추론은 Mac(Ollama)에서 돌고, hosub 는 역할 → 모델 라우팅만 한다.

async function postJSON(path, body, method = "POST") {
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
      el("div", { class: "card-header d-flex align-items-center gap-2" }, [
        el("span", { html: '<i class="bi bi-diagram-3"></i> 역할 → 모델' }),
        el("span", { class: "badge text-bg-warning d-none", id: "llm-ovr-count",
                     title: "roles.yaml 과 다른 역할이 있습니다" }),
        el("button", {
          class: "btn btn-sm btn-outline-primary ms-auto", type: "button",
          id: "llm-role-new",
        }, "역할 추가"),
      ]),
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
    // system 은 역할 기본값을 덮어쓴다. 비워 두면 **키를 안 보내** 역할 기본값이 쓰이고,
    // "비우기" 를 켜면 빈 문자열을 보내 시스템 프롬프트 없이 돌린다 —
    // 게이트웨이는 system 이 null 일 때만 역할 기본값을 쓰기 때문에 둘이 다르다.
    const systemBox = el("textarea", {
      class: "form-control form-control-sm mb-2", id: "llm-system", rows: "2",
      placeholder: "system (선택) — 비우면 역할 기본 프롬프트를 씁니다",
    });
    const noSystem = el("input", {
      class: "form-check-input", type: "checkbox", id: "llm-nosys",
    });
    const runBtn = el("button", { class: "btn btn-primary btn-sm", type: "button", id: "llm-run" },
      "실행");
    const outBox = el("div", { class: "mt-3" });
    tryBody.appendChild(el("div", { class: "d-flex gap-2 align-items-center mb-2 flex-wrap" }, [
      el("span", { class: "small text-secondary" }, "역할"), roleSelect, runBtn,
      el("div", { class: "form-check form-check-inline ms-2 small" }, [
        noSystem,
        el("label", { class: "form-check-label", for: "llm-nosys" },
          "시스템 프롬프트 없이"),
      ]),
    ]));
    tryBody.appendChild(systemBox);
    tryBody.appendChild(promptBox);
    tryBody.appendChild(outBox);
    tryCard.appendChild(tryBody);
    tryCol.appendChild(tryCard);
    row.appendChild(tryCol);

    const spinner = (msg) =>
      el("div", { class: "d-flex align-items-center gap-2 text-secondary small" }, [
        el("span", { class: "spinner-border spinner-border-sm" }), msg,
      ]);

    // --- 역할 편집 모달 ---
    //
    // 인라인 편집이 아니라 모달인 이유: load() 가 30초마다 rolesBody 를 통째로
    // 비우므로 인라인 위젯은 편집 도중 사라진다. 모달은 재렌더 대상 밖이다.
    // 추가로 editing 가드로 표 자체의 깜빡임도 막는다.
    let editing = false;
    const modalEl = el("div", {
      class: "modal fade", tabindex: "-1", id: "llm-role-modal",
    });
    modalEl.innerHTML = `
      <div class="modal-dialog modal-lg">
        <div class="modal-content">
          <div class="modal-header">
            <h5 class="modal-title">역할 편집</h5>
            <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
          </div>
          <div class="modal-body"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-outline-danger me-auto d-none" id="rm-revert"></button>
            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">닫기</button>
            <button type="button" class="btn btn-primary" id="rm-save">저장</button>
          </div>
        </div>
      </div>`;
    container.appendChild(modalEl);
    const modal = new window.bootstrap.Modal(modalEl);
    const mBody = modalEl.querySelector(".modal-body");
    const mTitle = modalEl.querySelector(".modal-title");
    const mSave = modalEl.querySelector("#rm-save");
    const mRevert = modalEl.querySelector("#rm-revert");
    modalEl.addEventListener("hidden.bs.modal", () => { editing = false; });

    let current = null;   // 편집 중인 역할 뷰 (null = 신규)

    const field = (label, input, help) =>
      el("div", { class: "mb-3" }, [
        el("label", { class: "form-label small text-secondary" }, label),
        input,
        help ? el("div", { class: "form-text small" }, help) : null,
      ]);

    const openRoleModal = (view, models) => {
      editing = true;
      current = view;
      const isNew = !view;
      const v = view || { name: "", model: "", lane: "batch", timeout: 180,
                          options: {}, origin: "db", overridden_fields: [],
                          base: null, max_prompt_chars: null };
      mTitle.textContent = isNew ? "역할 추가" : `역할 편집 — ${v.name}`;
      mBody.innerHTML = "";

      const nameIn = el("input", {
        class: "form-control form-control-sm", value: v.name,
        placeholder: "소문자·숫자·밑줄 (예: summarize_kr)",
        disabled: isNew ? null : "disabled",
      });
      // datalist 로 설치된 모델을 제안하되 직접 입력도 허용한다
      const dlId = "llm-model-list";
      const dl = el("datalist", { id: dlId });
      for (const m of models) dl.appendChild(el("option", { value: m }));
      const modelIn = el("input", {
        class: "form-control form-control-sm", value: v.model, list: dlId,
        placeholder: "qwen2.5:14b",
      });
      const laneIn = el("select", { class: "form-select form-select-sm" });
      for (const l of ["interactive", "batch"]) {
        const o = el("option", { value: l }, l);
        if (l === v.lane) o.setAttribute("selected", "selected");
        laneIn.appendChild(o);
      }
      const timeoutIn = el("input", {
        class: "form-control form-control-sm", type: "number", min: "1",
        max: "3600", value: String(v.timeout),
      });
      const optsIn = el("textarea", {
        class: "form-control form-control-sm mono", rows: "3",
      });
      optsIn.value = JSON.stringify(v.options || {}, null, 2);

      mBody.appendChild(dl);
      mBody.appendChild(field("역할 이름", nameIn,
        isNew ? "새 역할은 hosub(대시보드·MCP)만 쓸 수 있습니다. 외부 서비스에 "
              + "열려면 services.yaml 에 PR 이 필요합니다."
              : "역할 이름은 소비자와의 계약이라 바꿀 수 없습니다."));
      mBody.appendChild(field("모델", modelIn,
        "설치되지 않은 모델을 고르면 저장 후 설치를 제안합니다."));
      mBody.appendChild(el("div", { class: "row g-3" }, [
        el("div", { class: "col-6" }, field("레인", laneIn)),
        el("div", { class: "col-6" }, field("타임아웃(초)", timeoutIn)),
      ]));
      mBody.appendChild(field("options (JSON)", optsIn,
        "예: {\"temperature\": 0.2}"));

      if (!isNew && v.base) {
        const changed = v.overridden_fields || [];
        mBody.appendChild(el("div", { class: "alert alert-secondary small mb-0" }, [
          el("div", { class: "fw-medium mb-1" }, "roles.yaml 기본값"),
          el("div", { class: "mono" },
            `model=${v.base.model} · lane=${v.base.lane} · timeout=${v.base.timeout}s`),
          changed.length
            ? el("div", { class: "mt-1" }, `현재 바뀐 항목: ${changed.join(", ")}`)
            : el("div", { class: "mt-1 text-secondary" }, "기본값 그대로입니다."),
        ]));
      }
      if (!isNew && v.yaml_snippet) {
        const copy = el("button",
          { class: "btn btn-sm btn-outline-secondary mt-2", type: "button" },
          "roles.yaml 스니펫 복사");
        copy.addEventListener("click", async () => {
          await navigator.clipboard.writeText(v.yaml_snippet);
          copy.textContent = "복사됨";
          setTimeout(() => { copy.textContent = "roles.yaml 스니펫 복사"; }, 1500);
        });
        mBody.appendChild(copy);
        mBody.appendChild(el("div", { class: "form-text small" },
          "오버라이드는 게이트웨이 DB 에만 있습니다. 이 스니펫을 roles.yaml 에 "
          + "반영해 두지 않으면 DB 를 백업본으로 되돌릴 때 조용히 사라집니다."));
      }

      // 되돌리기 / 삭제
      // 되돌릴 게 있을 때만 보인다: 신규 생성 화면이거나, 기본값 그대로인 YAML 역할이면 숨긴다
      const revertable = !isNew
        && (v.origin === "db" || (v.overridden_fields || []).length > 0);
      mRevert.classList.toggle("d-none", !revertable);
      mRevert.textContent = v.origin === "db" ? "역할 삭제" : "기본값으로 되돌리기";
      mRevert.onclick = async () => {
        const msg = v.origin === "db"
          ? `역할 '${v.name}' 을(를) 삭제할까요?`
          : `'${v.name}' 을(를) roles.yaml 기본값(${v.base && v.base.model})으로 되돌릴까요?`;
        if (!window.confirm(msg)) return;
        mRevert.disabled = true;
        try {
          const r = await postJSON(
            `/api/llm/roles?role=${encodeURIComponent(v.name)}`, null, "DELETE");
          if (r.status !== "ok") {
            window.alert(r.detail || r.error || r.reason || "되돌리지 못했습니다.");
            return;
          }
          modal.hide();
          await load();
        } finally {
          mRevert.disabled = false;
        }
      };

      mSave.onclick = async () => {
        let options;
        try {
          options = JSON.parse(optsIn.value || "{}");
        } catch (e) {
          window.alert("options 가 올바른 JSON 이 아닙니다.");
          return;
        }
        const name = isNew ? nameIn.value.trim() : v.name;
        const fields = {
          model: modelIn.value.trim(),
          lane: laneIn.value,
          timeout: Number(timeoutIn.value),
          options,
        };
        mSave.disabled = true;
        try {
          const r = await postJSON("/api/llm/roles", { role: name, fields });
          if (r.status !== "ok") {
            window.alert(r.detail || r.error || r.reason || "저장하지 못했습니다.");
            return;
          }
          // "바꿨는데 아무 일도 안 일어남" 을 막는다
          if (r.install_required) {
            const m = r.install_required;
            if (window.confirm(
              `'${m.model}' 이(가) 맥에 없습니다 (~${m.est_size_gb}GB).\n지금 설치할까요?`)) {
              await postJSON("/api/llm/models/install", { model: m.model });
            }
          }
          modal.hide();
          await load();
        } finally {
          mSave.disabled = false;
        }
      };
      modal.show();
    };

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
    let knownModels = [];
    const load = async () => {
      // 편집 중에는 표를 다시 그리지 않는다 — 30초마다 흔들리면 못 쓴다
      if (editing) return;
      // 백엔드가 꺼져 있으면 응답까지 몇 초 걸리므로 최초에는 로딩 표시
      if (firstLoad) {
        backendBody.appendChild(spinner("백엔드 확인 중…"));
        rolesBody.appendChild(spinner("불러오는 중…"));
        firstLoad = false;
      }
      const d = await fetchJSON("/api/llm/status");
      backendBody.innerHTML = "";
      rolesBody.innerHTML = "";

      // 게이트웨이 자체에 못 닿는 경우 (컨테이너 다운·토큰 미설정)
      if (d.status === "error" || d.status === "unconfigured") {
        const box = el("div", { class: "alert alert-warning mb-0" }, [
          el("div", { class: "fw-medium" }, "게이트웨이에 연결할 수 없습니다"),
          el("div", { class: "mono small mt-1" }, d.error || d.reason || ""),
          d.hint ? el("div", { class: "small text-secondary mt-2" }, d.hint) : null,
        ]);
        backendBody.appendChild(box);
        rolesBody.appendChild(el("div", { class: "text-secondary small" },
          "게이트웨이가 떠야 역할 목록을 볼 수 있습니다."));
        return;
      }

      // --- 백엔드(맥) + 레인 큐 ---
      const b = d.backend || {};
      backendBody.appendChild(el("div", {}, [
        el("div", { class: "d-flex align-items-center gap-2" }, [
          el("span", { class: "fw-medium" }, "맥 스튜디오"),
          b.online ? badge("online", "success") : badge("offline", "danger"),
        ]),
        el("div", { class: "mono text-secondary" }, b.base_url || ""),
        b.online
          ? el("div", { class: "small text-secondary mt-1" },
              `보유 모델 ${(b.models || []).length}개`
              + (b.loaded_model ? ` · 최근 사용 ${b.loaded_model}` : ""))
          : el("div", { class: "small text-danger mt-1" }, b.error || ""),
        (b.models || []).length
          ? el("div", { class: "d-flex flex-wrap gap-1 mt-1" },
              b.models.map((m) => el("span", { class: "badge text-bg-secondary" }, m)))
          : null,
      ]));

      // 레인별 대기/실행 — 잡이 밀리는지 한눈에
      const lanes = d.lanes || {};
      const running = d.running || {};
      backendBody.appendChild(el("div", { class: "mt-3" }, [
        el("div", { class: "small text-secondary mb-1" },
          `레인 (메모리 예산 ${d.mem_budget_gb ?? "?"}GB)`),
        ...Object.keys(lanes).map((name) => {
          const l = lanes[name];
          const cur = running[name];
          return el("div", { class: "d-flex align-items-center gap-2 small" }, [
            el("span", { class: "mono", style: "min-width:88px" }, name),
            l.running ? badge(`실행 ${l.running}`, "primary") : badge("유휴", "secondary"),
            l.queued ? badge(`대기 ${l.queued}`, "warning") : null,
            cur ? el("span", { class: "text-secondary mono" }, cur.model) : null,
          ]);
        }),
      ]));

      // 사용량 — 어느 서비스가 얼마나 썼는지
      if ((d.usage || []).length) {
        backendBody.appendChild(el("div", { class: "mt-3" }, [
          el("div", { class: "small text-secondary mb-1" }, "사용량 (서비스별 누적)"),
          ...d.usage.map((u) =>
            el("div", { class: "d-flex justify-content-between small" }, [
              el("span", { class: "mono" }, u.service),
              el("span", { class: "text-secondary" },
                `${u.calls}회 · ${u.tokens || 0} 토큰 · 성공 ${u.ok}/${u.calls}`),
            ])),
        ]));
      }

      // --- 역할 ---
      // /api/llm/roles(관리)는 기본값 대비 차이와 스니펫까지 준다.
      // 실패해도 상태 카드는 이미 그려졌으므로 역할 영역만 경고로 대체한다.
      let admin = null;
      try {
        admin = await fetchJSON("/api/llm/roles");
      } catch (e) {
        admin = { status: "error", error: e.message };
      }
      const list = (admin && admin.roles) || d.roles || [];
      const models = ((d.backend || {}).models) || [];
      knownModels = models;

      const ovrBadge = rolesCard.querySelector("#llm-ovr-count");
      const ovrCount = ((admin || {}).overrides || {}).count || 0;
      ovrBadge.textContent = ovrCount ? `roles.yaml 과 다름 ${ovrCount}` : "";
      ovrBadge.classList.toggle("d-none", !ovrCount);
      const invalid = ((admin || {}).overrides || {}).invalid || [];
      if (invalid.length) {
        rolesBody.appendChild(el("div", { class: "alert alert-warning py-2 small" },
          `무시된 오버라이드 ${invalid.length}건: `
          + invalid.map((i) => `${i.role}(${i.error})`).join(", ")));
      }

      if (!list.length) {
        rolesBody.appendChild(el("div", { class: "text-secondary small" }, "정의된 역할이 없습니다."));
      } else {
        const canEdit = Boolean(admin && admin.roles);
        const rows = list.map((r) => {
          const ready = r.model_ready !== undefined ? r.model_ready : r.model_available;
          const changed = (r.overridden_fields || []).length || r.origin === "db";
          const actions = el("td", { class: "text-end" });
          if (canEdit) {
            const b = el("button",
              { class: "btn btn-sm btn-outline-secondary", type: "button" }, "편집");
            b.addEventListener("click", () => openRoleModal(r, models));
            actions.appendChild(b);
          }
          return el("tr", {}, [
            el("td", { class: "fw-medium" }, [
              el("div", {}, r.name),
              r.origin === "db"
                ? el("span", { class: "badge text-bg-info" }, "추가된 역할")
                : (changed ? el("span", { class: "badge text-bg-warning" }, "변경됨") : null),
            ]),
            el("td", { class: "mono small" }, r.model),
            el("td", { class: "small text-secondary" }, r.lane),
            el("td", { class: "small text-secondary" }, `${r.timeout}s`),
            el("td", {}, ready ? badge("준비됨", "success") : badge("설치 필요", "warning")),
            actions,
          ]);
        });
        const thead = el("thead", {}, el("tr", {}, [
          el("th", {}, "역할"), el("th", {}, "모델"), el("th", {}, "레인"),
          el("th", {}, "타임아웃"), el("th", {}, "상태"), el("th", {}, ""),
        ]));
        rolesBody.appendChild(el("div", { class: "table-responsive" },
          el("table", { class: "table table-sm table-hover align-middle mb-0" },
            [thead, el("tbody", {}, rows)])));
        if (ovrCount) {
          rolesBody.appendChild(el("div", { class: "small text-secondary mt-2" },
            "변경된 역할은 게이트웨이 DB 에만 저장됩니다 — 편집 창의 "
            + "\"roles.yaml 스니펫 복사\" 로 레포에도 반영해 두세요."));
        }

        // 역할 셀렉트 채우기 (선택 유지)
        const cur = roleSelect.value;
        roleSelect.innerHTML = "";
        for (const r of list) roleSelect.appendChild(el("option", { value: r.name }, r.name));
        if (cur && list.some((r) => r.name === cur)) roleSelect.value = cur;
        else if (list.some((r) => r.name === "general")) roleSelect.value = "general";
      }
    };

    backendCard.querySelector("#llm-refresh").addEventListener("click", () =>
      Promise.all([load(), loadRequests()]));

    rolesCard.querySelector("#llm-role-new").addEventListener("click", () =>
      openRoleModal(null, knownModels));

    runBtn.addEventListener("click", async () => {
      const prompt = promptBox.value.trim();
      if (!prompt) return;
      runBtn.disabled = true;
      runBtn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> 실행 중';
      outBox.innerHTML = "";
      // 폴링이 만료돼도 결과를 되찾을 수 있게 하는 버튼
      const resumeBtn = (jobId) => {
        if (!jobId) return null;
        const b = el("button", { class: "btn btn-sm btn-outline-primary mt-2", type: "button" },
          "결과 다시 확인");
        b.addEventListener("click", async () => {
          b.disabled = true;
          try {
            showResult(await fetchJSON(`/api/llm/jobs/${jobId}`));
          } finally {
            b.disabled = false;
          }
        });
        return b;
      };

      const showResult = (r) => {
        outBox.innerHTML = "";
        if (r.status === "ok") {
          const secs = r.started_at && r.finished_at
            ? (new Date(r.finished_at) - new Date(r.started_at)) / 1000 : null;
          outBox.appendChild(el("div", { class: "small text-secondary mb-1" },
            [r.model, r.lane, secs ? `${secs.toFixed(1)}s` : null].filter(Boolean).join(" · ")));
          outBox.appendChild(el("pre", {
            class: "border rounded p-3 mb-0",
            style: "white-space:pre-wrap; word-break:break-word; max-height:420px; overflow:auto",
          }, r.response || ""));
        } else if (r.status === "pending") {
          // 폴링 창(5분)을 넘긴 경우. 잡은 계속 돌고 있으므로 job_id 를 반드시
          // 보여준다 — 이걸 잃으면 결과를 되찾을 방법이 없다.
          outBox.appendChild(el("div", { class: "alert alert-info mb-0" }, [
            el("div", { class: "fw-medium" }, "아직 실행 중입니다 — 잡은 계속 돌고 있습니다"),
            el("div", { class: "small mt-1" }, [
              el("span", { class: "text-secondary" }, "job_id "),
              el("code", { class: "mono" }, r.job_id || "?"),
            ]),
            el("div", { class: "small text-secondary mt-1" },
              "모델 설치 승인을 기다리는 중일 수도 있습니다(위 카드 확인)."),
            resumeBtn(r.job_id),
          ]));
        } else {
          outBox.appendChild(el("div", { class: "alert alert-warning mb-0" }, [
            el("div", { class: "fw-medium" }, r.reason || r.error || "실행 실패"),
            r.hint ? el("div", { class: "small mt-2 text-secondary" }, r.hint) : null,
          ]));
        }
      };

      try {
        // system 을 안 보내면 역할 기본값, ""를 보내면 시스템 프롬프트 없음.
        // 게이트웨이가 null 일 때만 기본값을 쓰므로 둘을 구분해야 한다.
        const body = { role: roleSelect.value, prompt };
        if (noSystem.checked) body.system = "";
        else if (systemBox.value.trim()) body.system = systemBox.value;
        let r = await postJSON("/api/llm/generate", body);
        // 잡이 아직 안 끝났으면 폴링한다. 모델 미설치로 승인을 기다리는 중일 수도 있다.
        for (let i = 0; r.status === "pending" && i < 150; i++) {
          outBox.innerHTML = "";
          outBox.appendChild(el("div", { class: "d-flex align-items-center gap-2 text-secondary small" }, [
            el("span", { class: "spinner-border spinner-border-sm" }),
            r.queue_position ? `대기 중 (앞에 ${r.queue_position}개)` : "실행 중",
            el("span", { class: "mono" }, r.model || ""),
          ]));
          await new Promise((res) => setTimeout(res, 2000));
          r = await fetchJSON(`/api/llm/jobs/${r.job_id}`);
        }
        showResult(r);
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
