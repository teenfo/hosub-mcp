import { fetchJSON, el } from "../app.js";

function stateBadge(state) {
  const map = {
    succeeded: "ok",
    running: "warn",
    pending: "muted",
    failed: "err",
    timeout: "err",
  };
  return el("span", { class: "badge " + (map[state] || "muted") }, state);
}

export default {
  id: "jobs",
  title: "최근 백그라운드 잡",
  refreshMs: 5000,
  async render(body) {
    const data = await fetchJSON("/api/jobs?limit=10");
    body.innerHTML = "";
    if (!data.jobs || !data.jobs.length) {
      body.appendChild(el("div", { class: "empty" }, "실행된 잡이 없습니다."));
      return;
    }
    const tbl = el("table", {}, [
      el("thead", {}, el("tr", {}, [el("th", {}, "작업"), el("th", {}, "상태"), el("th", {}, "종료코드")])),
    ]);
    const tb = el("tbody");
    for (const j of data.jobs) {
      tb.appendChild(
        el("tr", {}, [
          el("td", {}, [
            el("div", {}, j.label),
            el("div", { class: "mono", style: "color:var(--muted); font-size:11px" }, j.id),
          ]),
          el("td", {}, stateBadge(j.state)),
          el("td", { class: "mono" }, j.exit_code == null ? "—" : String(j.exit_code)),
        ])
      );
    }
    tbl.appendChild(tb);
    body.appendChild(el("div", { class: "scroll" }, tbl));
  },
};
