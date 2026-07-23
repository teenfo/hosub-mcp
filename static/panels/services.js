import { fetchJSON, el, badge } from "../app.js";

function stateBadge(active) {
  const map = { active: "success", failed: "danger", activating: "warning", deactivating: "warning" };
  return badge(active || "unknown", map[active] || "secondary");
}

export default {
  id: "services",
  title: "서비스 상태",
  icon: "bi-hdd-stack",
  refreshMs: 8000,
  async render(body) {
    const data = await fetchJSON("/api/services");
    body.innerHTML = "";
    if (!data.services || !data.services.length) {
      body.appendChild(el("div", { class: "text-secondary small" }, "등록된 서비스가 없습니다."));
      return;
    }
    const rows = data.services.map((s) =>
      el("tr", {}, [
        el("td", {}, [
          el("div", { class: "fw-medium" }, s.name),
          el("div", { class: "mono text-secondary" }, s.unit),
        ]),
        el("td", { class: "text-end" }, [
          stateBadge(s.active_state),
          s.query_ok ? null : el("div", { class: "mono text-danger small mt-1" }, "조회 실패"),
        ]),
      ])
    );
    const tbl = el("table", { class: "table table-sm table-hover align-middle mb-0" },
      el("tbody", {}, rows));
    body.appendChild(el("div", { class: "table-responsive" }, tbl));
  },
};
