import { fetchJSON, el } from "../app.js";

function stateBadge(active) {
  if (active === "active") return el("span", { class: "badge ok" }, "active");
  if (active === "failed") return el("span", { class: "badge err" }, "failed");
  if (active === "activating" || active === "deactivating")
    return el("span", { class: "badge warn" }, active);
  return el("span", { class: "badge muted" }, active || "unknown");
}

export default {
  id: "services",
  title: "서비스 상태",
  refreshMs: 8000,
  async render(body) {
    const data = await fetchJSON("/api/services");
    body.innerHTML = "";
    if (!data.services || !data.services.length) {
      body.appendChild(el("div", { class: "empty" }, "등록된 서비스가 없습니다."));
      return;
    }
    const tbl = el("table");
    const tb = el("tbody");
    for (const s of data.services) {
      tb.appendChild(
        el("tr", {}, [
          el("td", {}, [
            el("div", {}, s.name),
            el("div", { class: "mono", style: "color:var(--muted); font-size:11px" }, s.unit),
          ]),
          el("td", { style: "text-align:right" }, [
            stateBadge(s.active_state),
            s.query_ok ? null : el("div", { class: "mono", style: "color:var(--err); font-size:11px; margin-top:4px" }, "조회 실패"),
          ]),
        ])
      );
    }
    tbl.appendChild(tb);
    body.appendChild(el("div", { class: "scroll" }, tbl));
  },
};
