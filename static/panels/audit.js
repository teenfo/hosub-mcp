import { fetchJSON, el, badge } from "../app.js";

function outcomeBadge(outcome) {
  const map = {
    ok: "success",
    succeeded: "success",
    job_started: "warning",
    approval_required: "warning",
    rejected: "secondary",
    error: "danger",
    failed: "danger",
    timeout: "danger",
  };
  return badge(outcome, map[outcome] || "secondary");
}

function shortTs(ts) {
  try {
    return new Date(ts).toLocaleTimeString("ko-KR");
  } catch {
    return ts;
  }
}

export default {
  id: "audit",
  title: "감사 로그 (최근 활동)",
  icon: "bi-shield-check",
  wide: true,
  refreshMs: 7000,
  async render(body) {
    const data = await fetchJSON("/api/audit?limit=25");
    body.innerHTML = "";
    if (!data.audit || !data.audit.length) {
      body.appendChild(el("div", { class: "text-secondary small" }, "기록된 활동이 없습니다."));
      return;
    }
    const rows = data.audit.map((r) =>
      el("tr", {}, [
        el("td", { class: "mono text-nowrap" }, shortTs(r.ts)),
        el("td", { class: "mono" }, r.tool),
        el("td", {}, outcomeBadge(r.outcome)),
        el("td", { class: "text-center" }, r.confirm ? el("i", { class: "bi bi-check-lg text-success" }) : ""),
        el("td", {
          class: "mono text-truncate",
          style: "max-width:360px",
          title: r.result_summary || "",
        }, r.result_summary || ""),
      ])
    );
    const thead = el("thead", {}, el("tr", {}, [
      el("th", {}, "시각"), el("th", {}, "도구"), el("th", {}, "결과"),
      el("th", { class: "text-center" }, "확인"), el("th", {}, "요약"),
    ]));
    const tbl = el("table", { class: "table table-sm table-hover align-middle mb-0" },
      [thead, el("tbody", {}, rows)]);
    // 높이 고정 + 세로 스크롤 (헤더는 sticky)
    body.appendChild(el("div", { class: "table-responsive audit-scroll" }, tbl));
  },
};
