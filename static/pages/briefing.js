import { fetchJSON, el } from "../app.js";

// 아주 작은 마크다운 렌더러 (제목/굵게/기울임/코드/목록/링크/문단).
// HTML 을 먼저 이스케이프한 뒤 변환하므로 원본에 태그가 있어도 안전하다.
function mdToHtml(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let list = null; // "ul" | "ol" | null
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList();
      const lvl = m[1].length + 2; // h3~h6
      out.push(`<h${lvl} class="mt-3">${inline(m[2])}</h${lvl}>`);
    } else if ((m = line.match(/^\s*[-*]\s+(.*)$/))) {
      if (list !== "ul") { closeList(); list = "ul"; out.push('<ul class="mb-2">'); }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if ((m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      if (list !== "ol") { closeList(); list = "ol"; out.push('<ol class="mb-2">'); }
      out.push(`<li>${inline(m[1])}</li>`);
    } else if (line === "") {
      closeList();
    } else {
      closeList();
      out.push(`<p class="mb-2">${inline(line)}</p>`);
    }
  }
  closeList();
  return out.join("\n");
}

export default {
  id: "briefing",
  title: "데일리 브리핑",
  icon: "bi-journal-text",
  async render(container, ctx) {
    const wrap = el("div", { class: "row g-3" });
    const col = el("div", { class: "col-12 col-xxl-9" });
    const cardEl = el("div", { class: "card shadow-sm" });
    const header = el("div", { class: "card-header d-flex align-items-center" }, [
      el("span", { html: '<i class="bi bi-journal-text"></i> 데일리 브리핑' }),
      el("span", { class: "ms-auto small text-secondary", id: "briefing-time" }),
    ]);
    const body = el("div", { class: "card-body" });
    cardEl.appendChild(header);
    cardEl.appendChild(body);
    col.appendChild(cardEl);
    wrap.appendChild(col);
    container.appendChild(wrap);

    const load = async () => {
      const d = await fetchJSON("/api/briefing");
      const timeEl = document.getElementById("briefing-time");
      body.innerHTML = "";
      if (!d.exists) {
        timeEl.textContent = "";
        body.appendChild(
          el("div", { class: "text-center text-secondary py-5 border border-2 border-dashed rounded" }, [
            el("div", { class: "display-6 mb-2" }, el("i", { class: "bi bi-journal-plus" })),
            el("div", {}, "아직 브리핑이 없습니다."),
            el("div", { class: "small mt-1" }, d.hint || "Claude 가 브리핑을 작성하면 여기에 표시됩니다."),
          ])
        );
        return;
      }
      if (d.updated_at) {
        timeEl.textContent = "업데이트: " + new Date(d.updated_at).toLocaleString("ko-KR");
      }
      body.appendChild(el("div", { class: "briefing-body", html: mdToHtml(d.content) }));
    };

    await load();
    ctx.addTimer(setInterval(load, 60000)); // 1분마다 갱신
  },
};
