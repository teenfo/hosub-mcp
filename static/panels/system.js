import { fetchJSON, el, bar } from "../app.js";

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${d}일 ${h}시간 ${m}분`;
}

function stat(label, value, percent, sub) {
  const inner = [
    el("div", { class: "stat-value" }, value),
    el("div", { class: "stat-label" }, sub || label),
  ];
  if (percent != null) inner.push(el("div", { class: "mt-1" }, bar(percent)));
  return el("div", { class: "col-6 col-md-4 col-xl stat" }, inner);
}

export default {
  id: "system",
  title: "시스템 리소스",
  icon: "bi-cpu",
  wide: true,
  refreshMs: 5000,
  async render(body) {
    const s = await fetchJSON("/api/status");
    body.innerHTML = "";

    body.appendChild(
      el("div", { class: "row g-3 mb-3" }, [
        stat("CPU", `${s.cpu.percent}%`, s.cpu.percent),
        stat("메모리", `${s.memory.percent}%`, s.memory.percent, `${s.memory.used_gb} / ${s.memory.total_gb} GB`),
        stat("스왑", `${s.swap.percent}%`, s.swap.percent, `${s.swap.used_gb} / ${s.swap.total_gb} GB`),
        stat("코어", `${s.cpu.cores}`, null, s.cpu.load_avg ? `load ${s.cpu.load_avg.join(" / ")}` : "코어"),
        stat("업타임", fmtUptime(s.uptime_seconds), null, "업타임"),
      ])
    );

    if (s.disks && s.disks.length) {
      const rows = s.disks.map((d) =>
        el("tr", {}, [
          el("td", { class: "mono" }, d.mount),
          el("td", {}, `${d.percent}%`),
          el("td", { class: "mono" }, `${d.used_gb} / ${d.total_gb} GB`),
          el("td", { style: "min-width:120px" }, bar(d.percent)),
        ])
      );
      body.appendChild(
        table(["마운트", "사용", "용량", ""], rows, "디스크")
      );
    }

    if (s.top_processes && s.top_processes.length) {
      const rows = s.top_processes.map((p) =>
        el("tr", {}, [
          el("td", { class: "mono" }, String(p.pid)),
          el("td", {}, p.name || ""),
          el("td", { class: "mono" }, p.user || ""),
          el("td", {}, `${p.mem_percent}%`),
        ])
      );
      body.appendChild(table(["PID", "프로세스", "사용자", "MEM%"], rows, "상위 프로세스"));
    }
  },
};

function table(headers, rows, caption) {
  const thead = el("thead", {}, el("tr", {}, headers.map((h) => el("th", {}, h))));
  const tbody = el("tbody", {}, rows);
  const tbl = el("table", { class: "table table-sm table-hover align-middle mb-0" }, [thead, tbody]);
  const wrap = el("div", { class: "table-responsive mt-3" });
  if (caption) wrap.appendChild(el("div", { class: "text-secondary small mb-1" }, caption));
  wrap.appendChild(tbl);
  return wrap;
}
