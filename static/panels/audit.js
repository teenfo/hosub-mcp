import { fetchJSON, el } from "../app.js";

function outcomeBadge(outcome) {
  const map = {
    ok: "ok",
    succeeded: "ok",
    job_started: "warn",
    approval_required: "warn",
    rejected: "muted",
    error: "err",
    failed: "err",
    timeout: "err",
  };
  return el("span", { class: "badge " + (map[outcome] || "muted") }, outcome);
}

function shortTs(ts) {
  // ISO → HH:MM:SS
  try {
    return new Date(ts).toLocaleTimeString("ko-KR");
  } catch {
    return ts;
  }
}

export default {
  id: "audit",
  title: "감사 로그 (최근 활동)",
  wide: true,
  refreshMs: 7000,
  async render(body) {
    const data = await fetchJSON("/api/audit?limit=25");
    body.innerHTML = "";
    if (!data.audit || !data.audit.length) {
      body.appendChild(el("div", { class: "empty" }, "기록된 활동이 없습니다."));
      return;
    }
    const tbl = el("table", {}, [
      el("thead", {}, el("tr", {}, [
        el("th", {}, "시각"), el("th", {}, "도구"), el("th", {}, "결과"),
        el("th", {}, "확인"), el("th", {}, "요약"),
      ])),
    ]);
    const tb = el("tbody");
    for (const r of data.audit) {
      tb.appendChild(
        el("tr", {}, [
          el("td", { class: "mono" }, shortTs(r.ts)),
          el("td", { class: "mono" }, r.tool),
          el("td", {}, outcomeBadge(r.outcome)),
          el("td", {}, r.confirm ? "✓" : ""),
          el("td", { class: "mono", style: "max-width:360px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap" }, r.result_summary || ""),
        ])
      );
    }
    tbl.appendChild(tb);
    body.appendChild(el("div", { class: "scroll" }, tbl));
  },
};
