import { fetchJSON, el, badge } from "../app.js";

function stateBadge(state, status) {
  const s = (state || "").toLowerCase();
  if (s === "running") return badge("running", "success");
  if (s === "exited" || s === "dead") return badge(state, "danger");
  if (s === "paused" || s === "restarting" || s === "created") return badge(state, "warning");
  return badge(state || status || "unknown", "secondary");
}

export default {
  id: "docker",
  title: "Docker",
  icon: "bi-box-seam",
  async render(container, ctx) {
    const wrap = el("div", { class: "row g-3" });
    const col = el("div", { class: "col-12" });
    const cardEl = el("div", { class: "card shadow-sm" });
    cardEl.appendChild(
      el("div", { class: "card-header" }, el("span", { html: '<i class="bi bi-box-seam"></i> 컨테이너' }))
    );
    const body = el("div", { class: "card-body" });
    cardEl.appendChild(body);
    col.appendChild(cardEl);
    wrap.appendChild(col);
    container.appendChild(wrap);

    const load = async () => {
      const res = await fetchJSON("/api/docker");
      body.innerHTML = "";
      if (!res.ok) {
        body.appendChild(
          el("div", { class: "alert alert-warning mb-0" }, [
            el("div", { class: "fw-medium" }, "Docker 정보를 가져오지 못했습니다."),
            el("div", { class: "mono small mt-1" }, res.error || ""),
            el("div", { class: "small mt-2 text-secondary" },
              "docker 미설치이거나, hosub 유저에 docker 권한이 없을 수 있습니다 (docker 그룹 추가 필요)."),
          ])
        );
        return;
      }
      if (!res.containers.length) {
        body.appendChild(el("div", { class: "text-secondary small" }, "컨테이너가 없습니다."));
        return;
      }
      const rows = res.containers.map((c) =>
        el("tr", {}, [
          el("td", {}, [
            el("div", { class: "fw-medium" }, c.name),
            el("div", { class: "mono text-secondary" }, (c.id || "").slice(0, 12)),
          ]),
          el("td", { class: "mono small" }, c.image),
          el("td", {}, stateBadge(c.state, c.status)),
          el("td", { class: "small text-secondary" }, c.status),
          el("td", { class: "mono small text-truncate", style: "max-width:180px", title: c.ports }, c.ports || ""),
        ])
      );
      const thead = el("thead", {}, el("tr", {}, [
        el("th", {}, "이름"), el("th", {}, "이미지"), el("th", {}, "상태"),
        el("th", {}, "상세"), el("th", {}, "포트"),
      ]));
      const tbl = el("table", { class: "table table-sm table-hover align-middle mb-0" },
        [thead, el("tbody", {}, rows)]);
      body.appendChild(el("div", { class: "table-responsive" }, tbl));
    };

    await load();
    ctx.addTimer(setInterval(load, 8000));
  },
};
