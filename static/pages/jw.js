import { fetchJSON, el, badge } from "../app.js";
import { postJSON } from "./tradelib.js";

// jw-mcp (JW.org 콘텐츠 MCP, 127.0.0.1:8604) 운영 화면.
//
// hosub 와 **별개 프로세스·별개 저장소**다. 자체 OAuth 2.1 로 사용자를 직접
// 인증하므로, 이 화면의 본래 목적은 **승인 대기 사용자를 처리하는 것**이다.
//
// 요약 카드는 서버가 주는 sections 를 그대로 그린다 — jw-mcp 가 지표를
// 추가·삭제·재정렬해도 이 파일은 그대로다. 특정 섹션 제목의 존재를 가정하지 않는다
// (스펙 3.1·11절).
//
// 폴링은 60초. 모든 지표가 로컬 SQLite 조회라 가볍지만 실시간성이 필요 없다.

const POLL_MS = 60_000;

const TONE_CLASS = {
  ok: "text-success",
  warn: "text-warning",
  danger: "text-danger",
  muted: "text-secondary",
};

const STATUS_BADGE = {
  active: ["활성", "success"],
  pending: ["승인 대기", "warning"],
  disabled: ["비활성", "secondary"],
};

// 타임스탬프는 **epoch 초(소수 포함)** 다 — 밀리초가 아니다(스펙 3.2).
function ago(epochSec) {
  if (!epochSec) return "-";
  const s = Math.max(0, Date.now() / 1000 - epochSec);
  if (s < 60) return "방금";
  if (s < 3600) return `${Math.floor(s / 60)}분 전`;
  if (s < 86400) return `${Math.floor(s / 3600)}시간 전`;
  return `${Math.floor(s / 86400)}일 전`;
}

function when(epochSec) {
  if (!epochSec) return "-";
  return new Date(epochSec * 1000).toLocaleString("ko-KR", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}

export default {
  id: "jw",
  title: "JW 도구",
  icon: "bi-book",
  group: "jw-mcp",
  async render(container, ctx) {
    const row = el("div", { class: "row g-3" });
    container.appendChild(row);

    const alertBox = (tone, title, detail) =>
      el("div", { class: `alert alert-${tone} mb-0` }, [
        el("div", { class: "fw-semibold" }, title),
        detail ? el("div", { class: "small mt-1" }, detail) : null,
      ]);

    const spinner = (msg) =>
      el("div", { class: "d-flex align-items-center gap-2 text-secondary small" }, [
        el("div", { class: "spinner-border spinner-border-sm" }),
        msg,
      ]);

    // jw-mcp 는 따로 죽거나 배포 중 잠깐 내려간다(스펙 8절). 그때 이 페이지가
    // 깨지지 않고 안내를 띄우게 한 곳에서 처리한다.
    const failure = (d) => {
      if (d.status === "unconfigured") {
        return alertBox("warning", "연결 정보가 없습니다",
          `${d.reason || ""} ${d.hint || ""}`.trim());
      }
      return alertBox("danger", "jw-mcp 에 연결하지 못했습니다",
        `${d.error || ""} ${d.hint || ""}`.trim());
    };

    // --- 요약 (서버가 준 sections 를 그대로) ---
    const sumCol = el("div", { class: "col-12" });
    const sumCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-hdd-network"></i> jw-mcp 요약' }),
        el("span", { class: "badge text-bg-secondary d-none", id: "jw-ver" }),
        el("span", { class: "small text-secondary ms-auto" },
          "지표 구성은 jw-mcp 가 정합니다 — 늘거나 줄 수 있습니다"),
      ]),
    ]);
    const sumBody = el("div", { class: "card-body" });
    sumCard.appendChild(sumBody);
    sumCol.appendChild(sumCard);
    row.appendChild(sumCol);

    const renderSections = (d) => {
      sumBody.innerHTML = "";
      if (d.status !== "ok") {
        sumBody.appendChild(failure(d));
        return;
      }
      const verBadge = sumCard.querySelector("#jw-ver");
      verBadge.textContent = `v${d.version || "?"}`;
      verBadge.classList.remove("d-none");

      const grid = el("div", { class: "row g-3" });
      for (const sec of d.sections || []) {
        const items = el("div", { class: "d-flex flex-column gap-1" });
        for (const it of sec.items || []) {
          items.appendChild(el("div", { class: "d-flex gap-2 align-items-baseline" }, [
            el("span", { class: "small text-secondary flex-grow-1",
                         style: "min-width:0; word-break:break-word" }, it.label),
            el("span", { class: `fw-semibold ${TONE_CLASS[it.tone] || ""}` },
               String(it.value)),
          ]));
        }
        // 제목·아이콘은 jw-mcp 가 준 값이다. innerHTML 에 넣지 않는다 —
        // 제목은 텍스트 노드로, 아이콘 클래스는 화이트리스트 패턴으로 거른다.
        const icon = /^bi-[a-z0-9-]+$/.test(sec.icon || "") ? sec.icon : "bi-dot";
        grid.appendChild(el("div", { class: "col-12 col-md-6 col-xl-4" },
          el("div", { class: "border rounded p-3 h-100" }, [
            el("div", { class: "fw-semibold mb-2 d-flex align-items-center gap-2" }, [
              el("i", { class: `bi ${icon}` }),
              el("span", {}, String(sec.title ?? "")),
            ]),
            items,
          ])));
      }
      sumBody.appendChild(grid);
    };

    // --- 사용자 ---
    const userCol = el("div", { class: "col-12" });
    const userCard = el("div", { class: "card shadow-sm" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-people"></i> 사용자' }),
        el("span", { class: "small text-secondary ms-auto" },
          "승인 대기가 위로 옵니다"),
      ]),
    ]);
    const userBody = el("div", { class: "card-body" });
    userCard.appendChild(userBody);
    userCol.appendChild(userCard);
    row.appendChild(userCol);

    // 상태 변경은 조회 전용 대시보드의 의도된 예외다. 되돌릴 수 없는 쪽
    // (active → 그 외)은 토큰이 전부 폐기되므로 확인 단계를 둔다.
    const statusBtn = (user, next, label, cls) => {
      const revoking = next !== "active";
      const b = el("button",
        { class: `btn btn-sm ${cls}`, type: "button" }, label);
      let armed = false;
      let timer = null;
      b.addEventListener("click", async () => {
        if (revoking && !armed) {
          armed = true;
          b.textContent = "한 번 더 — 토큰이 폐기됩니다";
          b.classList.add("btn-danger");
          timer = setTimeout(() => {
            armed = false;
            b.textContent = label;
            b.classList.remove("btn-danger");
          }, 4000);
          return;
        }
        if (timer) clearTimeout(timer);
        b.disabled = true;
        b.textContent = "적용 중…";
        try {
          const d = await postJSON("/api/jw/users/status",
            { user_id: user.user_id, status: next });
          if (d.status !== "ok") throw new Error(d.error || d.reason || "실패");
          await loadUsers();
          loadSummary();
        } catch (e) {
          if (e.message !== "unauthorized") {
            userBody.prepend(alertBox("danger", "상태를 바꾸지 못했습니다", e.message));
            b.disabled = false;
            b.textContent = label;
          }
        }
      });
      return b;
    };

    const renderUsers = (d) => {
      userBody.innerHTML = "";
      if (d.status !== "ok") {
        userBody.appendChild(failure(d));
        return;
      }
      const list = d.users || [];
      if (!list.length) {
        userBody.appendChild(el("div", { class: "text-secondary small" }, "사용자가 없습니다."));
        return;
      }

      const pending = list.filter((u) => u.status === "pending").length;
      if (pending) {
        userBody.appendChild(alertBox("warning", `승인 대기 ${pending}명`,
          "승인하면 그 사용자가 커넥터를 연결할 수 있습니다."));
      }

      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      tbl.appendChild(el("thead", {}, el("tr", {}, [
        el("th", {}, "사용자"), el("th", {}, "상태"), el("th", {}, "역할"),
        el("th", { class: "text-end" }, "호출"),
        el("th", { class: "text-end" }, "연결 앱"),
        el("th", {}, "마지막 활동"), el("th", { class: "text-end" }, ""),
      ])));
      const tb = el("tbody");
      for (const u of list) {
        const [label, tone] = STATUS_BADGE[u.status] || [u.status, "light"];
        const actions = el("div", { class: "d-flex gap-1 justify-content-end flex-wrap" });
        if (u.status !== "active") {
          actions.appendChild(statusBtn(u, "active", "승인", "btn-success"));
        }
        if (u.status !== "disabled") {
          actions.appendChild(statusBtn(u, "disabled", "비활성화", "btn-outline-danger"));
        }
        tb.appendChild(el("tr", {}, [
          el("td", {}, [
            el("div", { class: "fw-medium" }, u.display_name || "(이름 없음)"),
            el("div", { class: "small text-secondary mono" }, u.email || "-"),
          ]),
          el("td", {}, badge(label, tone)),
          el("td", {}, u.role === "admin" ? badge("관리자", "primary") : "일반"),
          el("td", { class: "text-end" }, u.call_count ?? 0),
          el("td", { class: "text-end" }, u.active_tokens ?? 0),
          el("td", { class: "small text-secondary" }, ago(u.last_call_at)),
          el("td", {}, actions),
        ]));
      }
      tbl.appendChild(tb);
      userBody.appendChild(el("div", { class: "table-responsive" }, tbl));
      userBody.appendChild(el("div", { class: "form-text small" },
        "비활성화하면 그 사용자의 OAuth 토큰이 즉시 전부 폐기됩니다. "
        + "다시 승인해도 사용자가 커넥터를 직접 재연결해야 합니다."));
    };

    // --- 사용량 ---
    const useCol = el("div", { class: "col-12 col-xl-6" });
    const useCard = el("div", { class: "card shadow-sm h-100" }, [
      el("div", { class: "card-header",
                  html: '<i class="bi bi-graph-up"></i> 사용량 (7일)' }),
    ]);
    const useBody = el("div", { class: "card-body" });
    useCard.appendChild(useBody);
    useCol.appendChild(useCard);
    row.appendChild(useCol);

    const renderUsage = (d) => {
      useBody.innerHTML = "";
      if (d.status !== "ok") {
        useBody.appendChild(failure(d));
        return;
      }
      const daily = d.daily || [];
      if (!daily.length) {
        useBody.appendChild(el("div", { class: "text-secondary small" },
          "최근 7일 호출이 없습니다."));
      } else {
        const max = Math.max(...daily.map((x) => x.calls || 0), 1);
        const rows = el("div", { class: "d-flex flex-column gap-2 mb-3" });
        // daily 는 호출이 있었던 날만 온다 — 빈 날은 애초에 그리지 않는다.
        for (const x of daily) {
          rows.appendChild(el("div", {}, [
            el("div", { class: "d-flex gap-2 small" }, [
              el("span", { class: "mono" }, x.day),
              el("span", { class: "ms-auto" }, `${x.calls}회`),
              x.errors ? el("span", { class: "text-danger" }, `오류 ${x.errors}`) : null,
              el("span", { class: "text-secondary" }, `${x.users}명`),
            ]),
            el("div", { class: "progress", style: "height:6px" },
              el("div", { class: "progress-bar text-bg-primary",
                          style: `width:${Math.round((x.calls / max) * 100)}%` })),
          ]));
        }
        useBody.appendChild(rows);
      }

      const top = d.top_tools || [];
      if (top.length) {
        useBody.appendChild(el("div", { class: "small text-secondary mb-1" }, "많이 쓰는 도구"));
        const ul = el("div", { class: "d-flex flex-column gap-1" });
        for (const t of top) {
          ul.appendChild(el("div", { class: "d-flex gap-2 small" }, [
            el("code", { class: "mono" }, t.tool),
            el("span", { class: "ms-auto text-secondary" }, `${t.n}회 · ${t.avg_ms}ms`),
          ]));
        }
        useBody.appendChild(ul);
      }
    };

    // --- 최근 호출 ---
    const callCol = el("div", { class: "col-12 col-xl-6" });
    const callCard = el("div", { class: "card shadow-sm h-100" }, [
      el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
        el("span", { html: '<i class="bi bi-list-ul"></i> 최근 호출' }),
        el("span", { class: "small text-secondary ms-auto" }, "도구 인자는 기록되지 않습니다"),
      ]),
    ]);
    const callBody = el("div", { class: "card-body" });
    callCard.appendChild(callBody);
    callCol.appendChild(callCard);
    row.appendChild(callCol);

    const renderCalls = (d) => {
      callBody.innerHTML = "";
      if (d.status !== "ok") {
        callBody.appendChild(failure(d));
        return;
      }
      const list = d.calls || [];
      if (!list.length) {
        callBody.appendChild(el("div", { class: "text-secondary small" }, "호출 기록이 없습니다."));
        return;
      }
      const tbl = el("table", { class: "table table-sm align-middle mb-0" });
      const tb = el("tbody");
      for (const c of list) {
        // ok 는 boolean 이 아니라 정수 1/0 이다(스펙 6절).
        const failed = c.ok === 0;
        tb.appendChild(el("tr", { class: failed ? "table-danger" : null }, [
          el("td", {}, [
            el("code", { class: "mono" }, c.tool),
            failed && c.error
              ? el("div", { class: "small text-danger" }, c.error)
              : null,
          ]),
          el("td", { class: "small text-secondary" }, c.display_name || c.email || "-"),
          el("td", { class: "text-end small text-secondary" }, `${c.duration_ms}ms`),
          el("td", { class: "small text-secondary text-nowrap" }, when(c.created_at)),
        ]));
      }
      tbl.appendChild(tb);
      callBody.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    // --- 로더 ---
    const load = (path, host, render) => {
      if (!host.childElementCount) host.appendChild(spinner("불러오는 중…"));
      return fetchJSON(path).then(render).catch((e) => {
        if (e.message === "unauthorized") return;
        host.innerHTML = "";
        host.appendChild(alertBox("danger", "불러오기 실패", e.message));
      });
    };

    const loadSummary = () => load("/api/jw/summary", sumBody, renderSections);
    const loadUsers = () => load("/api/jw/users", userBody, renderUsers);
    const loadUsage = () => load("/api/jw/usage?days=7", useBody, renderUsage);
    const loadCalls = () => load("/api/jw/calls?limit=20", callBody, renderCalls);

    const tick = () => {
      loadSummary(); loadUsers(); loadUsage(); loadCalls();
    };
    tick();
    ctx.addTimer(setInterval(tick, POLL_MS));
  },
};
