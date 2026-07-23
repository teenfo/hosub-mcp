import { fetchJSON, el, badge } from "../app.js";

function stateBadge(state) {
  const map = {
    succeeded: "success",
    running: "warning",
    pending: "secondary",
    failed: "danger",
    timeout: "danger",
  };
  return badge(state, map[state] || "secondary");
}

export default {
  id: "jobs",
  title: "최근 백그라운드 잡",
  icon: "bi-list-task",
  refreshMs: 5000,
  async render(body) {
    const data = await fetchJSON("/api/jobs?limit=10");
    body.innerHTML = "";
    if (!data.jobs || !data.jobs.length) {
      body.appendChild(el("div", { class: "text-secondary small" }, "실행된 잡이 없습니다."));
      return;
    }
    const rows = data.jobs.map((j) =>
      el("tr", {}, [
        el("td", {}, [
          el("div", { class: "fw-medium" }, j.label),
          el("div", { class: "mono text-secondary" }, j.id),
        ]),
        el("td", {}, stateBadge(j.state)),
        el("td", { class: "mono" }, j.exit_code == null ? "—" : String(j.exit_code)),
      ])
    );
    const thead = el("thead", {}, el("tr", {}, [
      el("th", {}, "작업"), el("th", {}, "상태"), el("th", {}, "종료코드"),
    ]));
    const tbl = el("table", { class: "table table-sm table-hover align-middle mb-0" },
      [thead, el("tbody", {}, rows)]);
    body.appendChild(el("div", { class: "table-responsive" }, tbl));
  },
};
