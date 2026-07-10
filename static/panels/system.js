import { fetchJSON, el, bar } from "../app.js";

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${d}일 ${h}시간 ${m}분`;
}

export default {
  id: "system",
  title: "시스템 리소스",
  wide: true,
  refreshMs: 5000,
  async render(body) {
    const s = await fetchJSON("/api/status");
    body.innerHTML = "";

    const metrics = el("div", { class: "metric-row" }, [
      metric("CPU", `${s.cpu.percent}%`, s.cpu.percent),
      metric("메모리", `${s.memory.percent}%`, s.memory.percent, `${s.memory.used_gb} / ${s.memory.total_gb} GB`),
      metric("스왑", `${s.swap.percent}%`, s.swap.percent, `${s.swap.used_gb} / ${s.swap.total_gb} GB`),
      metric("코어", `${s.cpu.cores}`, null, s.cpu.load_avg ? `load ${s.cpu.load_avg.join(" / ")}` : ""),
      metric("업타임", fmtUptime(s.uptime_seconds)),
    ]);
    body.appendChild(metrics);

    // 디스크
    if (s.disks && s.disks.length) {
      const tbl = el("table", {}, [
        el("thead", {}, el("tr", {}, [th("마운트"), th("사용"), th("용량"), th("")])),
      ]);
      const tb = el("tbody");
      for (const d of s.disks) {
        tb.appendChild(
          el("tr", {}, [
            el("td", { class: "mono" }, d.mount),
            el("td", {}, `${d.percent}%`),
            el("td", { class: "mono" }, `${d.used_gb} / ${d.total_gb} GB`),
            el("td", { style: "width:120px" }, bar(d.percent)),
          ])
        );
      }
      tbl.appendChild(tb);
      body.appendChild(el("div", { class: "scroll", style: "margin-top:14px" }, tbl));
    }

    // 상위 프로세스
    if (s.top_processes && s.top_processes.length) {
      const tbl = el("table", {}, [
        el("thead", {}, el("tr", {}, [th("PID"), th("프로세스"), th("사용자"), th("MEM%")])),
      ]);
      const tb = el("tbody");
      for (const p of s.top_processes) {
        tb.appendChild(
          el("tr", {}, [
            el("td", { class: "mono" }, String(p.pid)),
            el("td", {}, p.name || ""),
            el("td", { class: "mono" }, p.user || ""),
            el("td", {}, `${p.mem_percent}%`),
          ])
        );
      }
      tbl.appendChild(tb);
      body.appendChild(el("div", { class: "scroll", style: "margin-top:14px" }, tbl));
    }
  },
};

function metric(label, value, percent, sub) {
  const children = [
    el("div", { class: "value" }, value),
    el("div", { class: "label" }, sub || label),
  ];
  if (percent != null) children.push(bar(percent));
  return el("div", { class: "metric" }, children);
}

function th(t) {
  return el("th", {}, t);
}
