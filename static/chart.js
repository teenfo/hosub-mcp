// 상용 HTS 급 인터랙티브 캔들차트 — 외부 의존성 없음(순수 캔버스).
// 캔들 + 이동평균선 + 거래량 서브차트 + 십자선/툴팁 + 휠 확대·드래그 이동.
// 한국식 색: 상승 빨강 / 하락 파랑.
//
//   const chart = createProChart(hostDiv, { up:"#d64545", down:"#3a6fd8" });
//   chart.setData(bars);                 // bars: [{time,open,high,low,close,volume}]
//   chart.setVisibleCount(120 | "all");  // 보이는 봉 수
//   chart.setIndicator("bb", true);      // 볼린저밴드 on/off
//   chart.destroy();

const fmt = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });
const fmt2 = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 2 });

const MA_DEFS = [
  { p: 5, color: "#e0a800" },
  { p: 20, color: "#1f9d55" },
  { p: 60, color: "#7048e8" },
  { p: 120, color: "#d6336c" },
];

function sma(vals, p) {
  const out = new Array(vals.length).fill(null);
  let sum = 0;
  for (let i = 0; i < vals.length; i++) {
    sum += vals[i];
    if (i >= p) sum -= vals[i - p];
    if (i >= p - 1) out[i] = sum / p;
  }
  return out;
}

function bollinger(vals, p = 20, k = 2) {
  const mid = sma(vals, p);
  const up = new Array(vals.length).fill(null);
  const lo = new Array(vals.length).fill(null);
  for (let i = p - 1; i < vals.length; i++) {
    let s = 0;
    for (let j = i - p + 1; j <= i; j++) s += (vals[j] - mid[i]) ** 2;
    const sd = Math.sqrt(s / p);
    up[i] = mid[i] + k * sd;
    lo[i] = mid[i] - k * sd;
  }
  return { mid, up, lo };
}

function niceStep(range, ticks) {
  const raw = range / Math.max(1, ticks);
  const mag = Math.pow(10, Math.floor(Math.log10(raw || 1)));
  const norm = raw / mag;
  const step = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10;
  return step * mag;
}

export function createProChart(host, opts = {}) {
  const UP = opts.up || "#d64545";
  const DOWN = opts.down || "#3a6fd8";
  const canvas = document.createElement("canvas");
  canvas.style.cssText = "width:100%;display:block;cursor:crosshair;touch-action:none";
  host.appendChild(canvas);

  let bars = [];
  let ma = {};          // {5:[...],20:[...],60:[...],120:[...]}
  let bb = null;        // {mid,up,lo}
  let showBB = false;
  let i0 = 0, i1 = 0;   // 보이는 구간 [i0, i1)
  let cursor = null;    // {x,y}
  let raf = 0;

  const themeAxis = () =>
    getComputedStyle(document.documentElement).getPropertyValue("--bs-secondary-color").trim() || "#888";
  const themeGrid = () =>
    getComputedStyle(document.documentElement).getPropertyValue("--bs-border-color").trim() || "rgba(128,128,128,.2)";
  const themeText = () =>
    getComputedStyle(document.documentElement).getPropertyValue("--bs-body-color").trim() || "#333";
  const themeBg = () =>
    getComputedStyle(document.documentElement).getPropertyValue("--bs-body-bg").trim() || "#fff";

  function computeIndicators() {
    const closes = bars.map((b) => b.close);
    ma = {};
    for (const d of MA_DEFS) ma[d.p] = sma(closes, d.p);
    bb = closes.length >= 20 ? bollinger(closes, 20, 2) : null;
  }

  function setData(data) {
    bars = Array.isArray(data) ? data.slice() : [];
    computeIndicators();
    const def = Math.min(bars.length, 120);
    i1 = bars.length;
    i0 = Math.max(0, i1 - def);
    cursor = null;
    schedule();
  }

  function setVisibleCount(n) {
    if (n === "all") { i0 = 0; i1 = bars.length; }
    else {
      i1 = bars.length;
      i0 = Math.max(0, i1 - Math.min(bars.length, n));
    }
    schedule();
  }

  function setIndicator(name, on) {
    if (name === "bb") showBB = !!on;
    schedule();
  }

  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; draw(); });
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    const W = host.clientWidth || 800;
    const H = host.clientHeight || 480;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.height = H + "px";
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    g.font = "11px sans-serif";

    if (!bars.length) {
      g.fillStyle = themeAxis();
      g.font = "13px sans-serif";
      g.fillText("일봉 데이터 없음 — 야간 발굴 수집 후 표시됩니다", 16, 40);
      return;
    }

    const RPAD = 60, LPAD = 4, TPAD = 8, DATEH = 20;
    const plotW = W - RPAD - LPAD;
    const usableH = H - TPAD - DATEH;
    const priceH = Math.round(usableH * 0.72);
    const volTop = TPAD + priceH + 8;
    const volH = usableH - priceH - 8;

    const count = Math.max(1, i1 - i0);
    const view = bars.slice(i0, i1);
    const cw = plotW / count;
    const xOf = (idx) => LPAD + (idx - i0 + 0.5) * cw;

    // 가격 범위 (캔들 + 보이는 MA/BB 포함)
    let lo = Infinity, hi = -Infinity;
    for (const b of view) { if (b.low < lo) lo = b.low; if (b.high > hi) hi = b.high; }
    for (let idx = i0; idx < i1; idx++) {
      for (const d of MA_DEFS) { const v = ma[d.p][idx]; if (v != null) { if (v < lo) lo = v; if (v > hi) hi = v; } }
      if (showBB && bb) { const u = bb.up[idx], l = bb.lo[idx]; if (u != null) { if (u > hi) hi = u; if (l < lo) lo = l; } }
    }
    const pad = (hi - lo) * 0.06 || hi * 0.06 || 1;
    lo -= pad; hi += pad;
    const yOf = (p) => TPAD + (hi - p) / (hi - lo || 1) * priceH;

    // 거래량 범위
    let vmax = 0;
    for (const b of view) if (b.volume > vmax) vmax = b.volume;
    const vyOf = (v) => volTop + volH - (v / (vmax || 1)) * volH;

    // --- 가격 그리드 + 우측 축 ---
    const grid = themeGrid(), axis = themeAxis();
    g.strokeStyle = grid; g.fillStyle = axis; g.lineWidth = 1;
    const step = niceStep(hi - lo, 5);
    const first = Math.ceil(lo / step) * step;
    g.beginPath();
    for (let p = first; p <= hi; p += step) {
      const y = yOf(p);
      g.moveTo(LPAD, y); g.lineTo(LPAD + plotW, y);
      g.fillText(fmt(p), LPAD + plotW + 6, y + 3);
    }
    g.stroke();

    // --- 볼린저밴드 (채움) ---
    if (showBB && bb) {
      g.beginPath();
      let started = false;
      for (let idx = i0; idx < i1; idx++) { const u = bb.up[idx]; if (u == null) continue; const x = xOf(idx); if (!started) { g.moveTo(x, yOf(u)); started = true; } else g.lineTo(x, yOf(u)); }
      for (let idx = i1 - 1; idx >= i0; idx--) { const l = bb.lo[idx]; if (l == null) continue; g.lineTo(xOf(idx), yOf(l)); }
      g.closePath();
      g.fillStyle = "rgba(120,120,200,.10)"; g.fill();
      for (const key of ["up", "mid", "lo"]) {
        g.beginPath(); let st = false;
        for (let idx = i0; idx < i1; idx++) { const v = bb[key][idx]; if (v == null) continue; const x = xOf(idx), y = yOf(v); st ? g.lineTo(x, y) : (g.moveTo(x, y), st = true); }
        g.strokeStyle = "rgba(120,120,200,.55)"; g.setLineDash(key === "mid" ? [] : [3, 3]); g.stroke();
      }
      g.setLineDash([]);
    }

    // --- 캔들 ---
    const bw = Math.max(1, cw * 0.7);
    for (let k = 0; k < view.length; k++) {
      const b = view[k];
      const x = xOf(i0 + k);
      const up = b.close >= b.open;
      g.strokeStyle = g.fillStyle = up ? UP : DOWN;
      g.beginPath(); g.moveTo(x, yOf(b.high)); g.lineTo(x, yOf(b.low)); g.stroke();
      const yo = yOf(b.open), yc = yOf(b.close);
      const bh = Math.max(1, Math.abs(yo - yc));
      g.fillRect(x - bw / 2, Math.min(yo, yc), bw, bh);
      // 거래량
      g.fillStyle = up ? "rgba(214,69,69,.55)" : "rgba(58,111,216,.55)";
      const vy = vyOf(b.volume);
      g.fillRect(x - bw / 2, vy, bw, volTop + volH - vy);
    }

    // --- 이동평균선 ---
    for (const d of MA_DEFS) {
      const arr = ma[d.p];
      if (!arr.some((v, idx) => v != null && idx >= i0 && idx < i1)) continue;
      g.beginPath(); let st = false;
      for (let idx = i0; idx < i1; idx++) { const v = arr[idx]; if (v == null) continue; const x = xOf(idx), y = yOf(v); st ? g.lineTo(x, y) : (g.moveTo(x, y), st = true); }
      g.strokeStyle = d.color; g.lineWidth = 1.3; g.stroke();
    }
    g.lineWidth = 1;

    // 거래량 구분선
    g.strokeStyle = grid; g.beginPath(); g.moveTo(LPAD, volTop - 4); g.lineTo(LPAD + plotW, volTop - 4); g.stroke();
    g.fillStyle = axis; g.fillText("거래량 " + fmt(vmax), LPAD + plotW + 6, volTop + 8);

    // --- 날짜 축 ---
    const dstr = (t) => new Date(t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" });
    g.fillStyle = axis;
    const labels = Math.min(6, count);
    for (let j = 0; j < labels; j++) {
      const idx = i0 + Math.round((j / Math.max(1, labels - 1)) * (count - 1));
      const b = bars[idx]; if (!b) continue;
      const x = Math.max(LPAD + 2, Math.min(LPAD + plotW - 44, xOf(idx) - 22));
      g.fillText(dstr(b.time), x, H - 6);
    }

    // --- 십자선 + 툴팁 ---
    if (cursor) {
      const k = Math.floor((cursor.x - LPAD) / cw);
      const idx = i0 + Math.max(0, Math.min(count - 1, k));
      const b = bars[idx];
      const cx = xOf(idx);
      g.strokeStyle = axis; g.setLineDash([4, 3]); g.lineWidth = 1;
      g.beginPath(); g.moveTo(cx, TPAD); g.lineTo(cx, TPAD + priceH); g.stroke();
      if (cursor.y >= TPAD && cursor.y <= TPAD + priceH) {
        g.beginPath(); g.moveTo(LPAD, cursor.y); g.lineTo(LPAD + plotW, cursor.y); g.stroke();
        const price = hi - (cursor.y - TPAD) / priceH * (hi - lo);
        g.setLineDash([]);
        g.fillStyle = themeText(); g.fillRect(LPAD + plotW, cursor.y - 8, RPAD, 16);
        g.fillStyle = themeBg(); g.fillText(fmt(price), LPAD + plotW + 6, cursor.y + 3);
      }
      g.setLineDash([]);
      drawLegend(g, b, idx, themeBg(), themeText());
    } else if (view.length) {
      drawLegend(g, bars[i1 - 1], i1 - 1, themeBg(), themeText());
    }
  }

  function drawLegend(g, b, idx, bg, text) {
    if (!b) return;
    const prev = bars[idx - 1];
    const chg = prev ? (b.close - prev.close) / prev.close * 100 : 0;
    const col = b.close >= (prev ? prev.close : b.open) ? UP : DOWN;
    const lines = [
      [dstr(b.time), text],
      [`시 ${fmt(b.open)}  고 ${fmt(b.high)}`, text],
      [`저 ${fmt(b.low)}  종 ${fmt(b.close)}`, text],
      [`${chg >= 0 ? "▲" : "▼"} ${fmt2(Math.abs(chg))}%  거래량 ${fmt(b.volume)}`, col],
    ];
    for (const d of MA_DEFS) { const v = ma[d.p][idx]; if (v != null) lines.push([`MA${d.p} ${fmt(v)}`, d.color]); }
    g.font = "11px sans-serif";
    const wBox = 168, hBox = 14 * lines.length + 8;
    g.globalAlpha = 0.85; g.fillStyle = bg; g.fillRect(6, 6, wBox, hBox); g.globalAlpha = 1;
    g.strokeStyle = "rgba(128,128,128,.35)"; g.strokeRect(6, 6, wBox, hBox);
    lines.forEach((ln, k) => { g.fillStyle = ln[1]; g.fillText(ln[0], 14, 22 + k * 14); });
    function dstr(t) { return new Date(t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" }); }
  }
  function dstr(t) { return new Date(t * 1000).toLocaleDateString("ko-KR", { year: "2-digit", month: "2-digit", day: "2-digit" }); }

  // --- 인터랙션 ---
  const onMove = (e) => {
    const r = canvas.getBoundingClientRect();
    cursor = { x: e.clientX - r.left, y: e.clientY - r.top };
    schedule();
  };
  const onLeave = () => { cursor = null; schedule(); };
  const onWheel = (e) => {
    if (!bars.length) return;
    e.preventDefault();
    const r = canvas.getBoundingClientRect();
    const plotW = (host.clientWidth || 800) - 64;
    const count = i1 - i0;
    const frac = Math.max(0, Math.min(1, (e.clientX - r.left - 4) / plotW));
    const pivot = i0 + frac * count;
    const factor = e.deltaY < 0 ? 0.85 : 1.18;
    let newCount = Math.round(count * factor);
    newCount = Math.max(10, Math.min(bars.length, newCount));
    let n0 = Math.round(pivot - frac * newCount);
    n0 = Math.max(0, Math.min(bars.length - newCount, n0));
    i0 = n0; i1 = n0 + newCount;
    schedule();
  };
  let drag = null;
  const onDown = (e) => { drag = { x: e.clientX, i0, i1 }; canvas.style.cursor = "grabbing"; };
  const onUp = () => { drag = null; canvas.style.cursor = "crosshair"; };
  const onDrag = (e) => {
    if (!drag) return onMove(e);
    const plotW = (host.clientWidth || 800) - 64;
    const count = drag.i1 - drag.i0;
    const cw = plotW / count;
    const shift = Math.round((e.clientX - drag.x) / cw);
    let n0 = drag.i0 - shift;
    n0 = Math.max(0, Math.min(bars.length - count, n0));
    i0 = n0; i1 = n0 + count;
    onMove(e);
  };
  const onDbl = () => setVisibleCount(120);

  canvas.addEventListener("pointermove", onDrag);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.addEventListener("pointerdown", onDown);
  window.addEventListener("pointerup", onUp);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("dblclick", onDbl);
  const ro = new ResizeObserver(() => schedule());
  ro.observe(host);

  function destroy() {
    canvas.removeEventListener("pointermove", onDrag);
    canvas.removeEventListener("pointerleave", onLeave);
    canvas.removeEventListener("pointerdown", onDown);
    window.removeEventListener("pointerup", onUp);
    canvas.removeEventListener("wheel", onWheel);
    canvas.removeEventListener("dblclick", onDbl);
    ro.disconnect();
    host.removeChild(canvas);
  }

  return { setData, setVisibleCount, setIndicator, redraw: schedule, destroy, MA_DEFS };
}

export { MA_DEFS };
