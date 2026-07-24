// 트레이딩 그룹 페이지(매매 데스크·발굴감시·성과백테스트) 공용 헬퍼.
import { badge } from "../app.js";

export async function postJSON(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("unauthorized");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || "HTTP " + res.status);
  return data;
}

export const fmt = (n) => Number(n).toLocaleString("ko-KR", { maximumFractionDigits: 0 });
export const won = (n) => (n > 0 ? "+" : "") + fmt(n) + "원";
export const pct = (n) => (n > 0 ? "+" : "") + n + "%";

// 가격 셀 렌더: 현재가 + (진입가 있으면) 괴리%. 초기 렌더·부분 갱신 공용.
export function priceCellHTML(cell, price, entry) {
  if (price == null || price === "" || isNaN(Number(price))) { cell.textContent = "—"; return; }
  entry = Number(entry) || 0;
  if (entry) {
    const gap = (price - entry) / entry * 100;
    cell.innerHTML = `<span class="fw-semibold">${fmt(price)}</span>` +
      `<span class="small ms-1 ${gap >= 0 ? "text-danger" : "text-primary"}">${gap >= 0 ? "+" : ""}${gap.toFixed(2)}%</span>`;
  } else {
    cell.textContent = fmt(price);
  }
}

// 생성 시각 → "N분 전" / 만료 시각 → "만료까지 M분" (staleness 표시)
export const agoStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "방금";
  if (m < 60) return m + "분 전";
  return Math.floor(m / 60) + "시간 " + (m % 60) + "분 전";
};
export const leftStr = (iso) => {
  if (!iso) return "";
  const m = Math.round((new Date(iso) - Date.now()) / 60000);
  return m <= 0 ? "만료됨" : "만료까지 " + m + "분";
};

export const sideBadge = (side) =>
  badge(side === "short" ? "숏" : "롱", side === "short" ? "danger" : "success");

// 변경 감지 메모 팩토리: 폴링 데이터가 실제로 바뀔 때만 DOM 재렌더(깜빡임 제거).
export function makeChanged() {
  const memo = {};
  const changed = (key, data) => {
    const s = JSON.stringify(data);
    if (memo[key] === s) return false;
    memo[key] = s;
    return true;
  };
  changed.invalidate = (key) => { memo[key] = undefined; };
  return changed;
}
