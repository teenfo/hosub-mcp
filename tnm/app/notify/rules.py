"""알림 발송 판정 (요청서 7.2) — 순수 함수. 시각·설정을 주입받아 결정론적으로 동작.

| 조건 | 동작 |
| shadow_mode | 기록만 (발송 없음) |
| 조용시간(22:00–07:00) & 비긴급 | defer — 익일 07:00 묶음 |
| 긴급 카테고리(정정공시·거래정지 등) | 조용시간에도 즉시 발송 |
| 종목별 일일 상한 초과 | suppress — 일말 "그 외 N건" 요약 |
| 그 외 (점수≥임계값) | send |
"""
from __future__ import annotations

from datetime import datetime


def in_quiet_hours(now: datetime, cfg: dict) -> bool:
    """조용시간(자정 걸침 지원): quiet_start ≤ t 또는 t < quiet_end."""
    start = str(cfg.get("quiet_start", "22:00"))
    end = str(cfg.get("quiet_end", "07:00"))
    hhmm = now.strftime("%H:%M")
    if start <= end:                       # 같은 날 구간 (예: 01:00~06:00)
        return start <= hhmm < end
    return hhmm >= start or hhmm < end    # 자정 걸침 (기본 22:00~07:00)


def decide(now: datetime, category: str | None, sent_today: int, cfg: dict) -> str:
    """반환: 'shadow' | 'send' | 'defer' | 'suppress'.

    호출 전제: 점수는 이미 종목 임계값을 넘은 후보다 (미달은 후보 조회에서 제외).
    """
    if cfg.get("shadow_mode", True):
        return "shadow"
    urgent = category in set(cfg.get("urgent_categories", []) or [])
    if in_quiet_hours(now, cfg) and not urgent:
        return "defer"
    if sent_today >= int(cfg.get("default_daily_cap", 5)) and not urgent:
        return "suppress"
    return "send"


DIR_LABEL = {"positive": "긍정", "negative": "부정", "neutral": "중립", "unclear": "불명"}
HORIZON_LABEL = {"immediate": "즉시", "short": "단기", "long": "장기"}


def format_message(item: dict) -> str:
    """요청서 7.1 메시지 형식 — 점수·카테고리 병기, 요약 2줄, 원문 링크 필수(P5)."""
    d = DIR_LABEL.get(item.get("impact_direction"), "불명")
    h = HORIZON_LABEL.get(item.get("impact_horizon"), "-")
    summary = (item.get("summary") or "").strip()
    if len(summary) > 200:
        summary = summary[:200] + "…"
    return (f"[{item.get('name', item.get('ticker', ''))}] {item.get('score')}점 · "
            f"{item.get('category', '-')} · {d}({h})\n"
            f"{summary}\n"
            f"▸ 원문 보기: {item.get('url', '')}")


def format_digest(items: list[dict]) -> str:
    """조용시간 묶음(익일 07:00) — 한 메시지로 요약."""
    lines = [f"🌙 밤사이 알림 {len(items)}건 (조용시간 묶음)"]
    for it in items[:10]:
        lines.append(f"• [{it.get('name', '')}] {it.get('score')}점 "
                     f"{it.get('category', '-')} — {(it.get('title') or '')[:60]}")
    if len(items) > 10:
        lines.append(f"…외 {len(items) - 10}건 (대시보드 뉴스 모니터에서 확인)")
    return "\n".join(lines)


def format_cap_summary(ticker_name: str, count: int) -> str:
    return f"[{ticker_name}] 일일 알림 상한 초과 — 그 외 {count}건은 대시보드에서 확인"
