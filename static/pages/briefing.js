import { fetchJSON, el } from "../app.js";

// 아주 작은 마크다운 렌더러 (.md 브리핑용).
export function mdToHtml(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const inline = (s) =>
    esc(s)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let list = null;
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      closeList();
      const lvl = m[1].length + 2;
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

// 브리핑 HTML 을 iframe 으로 격리 렌더링한다.
// 브리핑 파일은 웹 루트(/var/www/html)에 있는 독립 HTML 문서일 수 있어, 자체 스타일이
// 대시보드로 새어 레이아웃이 커지는 것을 막으려면 격리가 필요하다.
// sandbox="allow-same-origin" : 스크립트 실행 차단(격리) + 부모가 높이 측정은 가능.
export function renderIframe(body, html) {
  const iframe = el("iframe", { class: "briefing-frame", sandbox: "allow-same-origin" });
  body.appendChild(iframe);
  iframe.addEventListener("load", () => resize(iframe));
  iframe.srcdoc = html;
  // 폰트 등 로딩 후 재측정 (한 번). 지속 감시(ResizeObserver 루프)는 쓰지 않는다.
  setTimeout(() => resize(iframe), 300);
}

function resize(iframe) {
  try {
    const doc = iframe.contentDocument;
    if (!doc) return;
    const h = Math.max(
      doc.documentElement.scrollHeight,
      doc.body ? doc.body.scrollHeight : 0
    );
    if (h > 0) iframe.style.height = h + 24 + "px";
  } catch {
    iframe.style.height = "600px";
  }
}

export default {
  id: "briefing",
  title: "데일리 브리핑",
  icon: "bi-journal-text",
  async render(container, ctx) {
    const col = el("div", { class: "col-12 col-xxl-10 mx-auto" });
    const cardEl = el("div", { class: "card shadow-sm" });
    const header = el("div", { class: "card-header d-flex align-items-center gap-2 flex-wrap" }, [
      el("span", { html: '<i class="bi bi-journal-text"></i> 데일리 브리핑' }),
      el("select", { class: "form-select form-select-sm w-auto ms-auto d-none", id: "briefing-date" }),
      el("span", { class: "small text-secondary", id: "briefing-time" }),
    ]);
    const bodyEl = el("div", { class: "card-body" });
    cardEl.appendChild(header);
    cardEl.appendChild(bodyEl);
    col.appendChild(cardEl);
    container.appendChild(el("div", { class: "row g-3" }, col));

    const select = header.querySelector("#briefing-date");
    const timeEl = header.querySelector("#briefing-time");

    const load = async (date) => {
      const q = date ? "?date=" + encodeURIComponent(date) : "";
      const d = await fetchJSON("/api/briefing" + q);
      bodyEl.innerHTML = "";

      if (!d.exists) {
        select.classList.add("d-none");
        timeEl.textContent = "";
        bodyEl.appendChild(
          el("div", { class: "text-center text-secondary py-5 border border-2 border-dashed rounded" }, [
            el("div", { class: "display-6 mb-2" }, el("i", { class: "bi bi-journal-plus" })),
            el("div", {}, "아직 브리핑이 없습니다."),
            el("div", { class: "small mt-1" }, d.hint || "Claude 가 브리핑을 작성하면 여기에 표시됩니다."),
          ])
        );
        return;
      }

      if (d.dates && d.dates.length > 1) {
        select.innerHTML = "";
        for (const dt of d.dates) {
          const opt = el("option", { value: dt }, dt);
          if (dt === d.date) opt.selected = true;
          select.appendChild(opt);
        }
        select.classList.remove("d-none");
      } else {
        select.classList.add("d-none");
      }
      timeEl.textContent = d.updated_at ? "업데이트: " + new Date(d.updated_at).toLocaleString("ko-KR") : "";

      if (d.format === "md") {
        // 마크다운은 안전하므로 대시보드 테마로 직접 렌더
        const holder = el("div", { class: "briefing-body" });
        holder.innerHTML = mdToHtml(d.content);
        bodyEl.appendChild(holder);
      } else {
        // HTML 은 iframe 으로 격리 (스타일 누출/레이아웃 팽창 방지)
        renderIframe(bodyEl, d.content);
      }
    };

    select.addEventListener("change", () => load(select.value));
    await load();
    ctx.addTimer(setInterval(() => load(select.classList.contains("d-none") ? null : select.value), 60000));
  },
};
