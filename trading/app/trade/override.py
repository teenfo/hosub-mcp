"""일일 손실 가드 임시 해제 — 수동 승인으로 내려가지 않고 자동 매매를 이어간다.

**당일 실현손익 자체는 건드리지 않는다.** 화면·일지에 찍히는 -1.86% 같은 숫자는
진실 그대로 유지되고, 바뀌는 것은 '어느 선에서 멈출 것인가' 하나뿐이다.

두 가지 방식:
- reset  : 지금 손익을 기준선으로 삼아 한도를 처음부터 다시 센다.
           (예: -1.86% 에서 한도 1.5% → -3.36% 에서 다시 정지)
- extend : 설정 한도에 지정한 만큼만 더한다. (예: +0.5% → -2.0% 에서 정지)

안전장치를 스스로 여는 기능이므로 반드시 다음을 지킨다.
1. 오늘만 유효 — 날짜가 바뀌면 자동 소멸(파일이 남아 있어도 무시).
2. 시간 만료 — 분 단위 지정, 미지정 시 장 마감(15:30)까지.
3. 하루 총량·횟수 상한(config risk.guard_override_max_pct / _max_count).
   둘 중 하나라도 0이면 기능 자체가 꺼진 것으로 본다.
4. 사용 이력(언제·얼마나·그때 손익)을 남겨 일지에서 되짚을 수 있게 한다.
"""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
FILE = Path(settings.DATA_DIR) / "guard_override.json"
MARKET_CLOSE = "15:30"          # 미지정 시 만료 시각(정규장 마감)
MODES = ("reset", "extend")


def _empty(day: str) -> dict:
    return {"date": day, "extra_loss_pct": 0.0, "until": "", "count": 0,
            "history": []}


def _load_raw() -> dict:
    try:
        d = json.loads(FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _today(now: datetime) -> dict:
    """오늘자 기록만 돌려준다(어제 것은 쓰지 않는다)."""
    day = now.date().isoformat()
    d = _load_raw()
    return d if d.get("date") == day else _empty(day)


def _close_at(now: datetime) -> datetime:
    h, m = MARKET_CLOSE.split(":")
    return now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)


def limits() -> tuple[float, int]:
    """(하루 총량 상한 %, 하루 횟수 상한). 둘 중 하나라도 0이면 기능 꺼짐."""
    try:
        pct = float(settings.RISK.get("guard_override_max_pct", 0) or 0)
        cnt = int(settings.RISK.get("guard_override_max_count", 0) or 0)
    except (TypeError, ValueError):
        return 0.0, 0
    return max(0.0, pct), max(0, cnt)


def state(now: datetime | None = None) -> dict:
    """지금 유효한 임시 해제 상태. 만료됐으면 extra_loss_pct=0.

    파일을 지우지 않는다 — 만료된 이력도 일지·감사에 남아야 한다.
    """
    now = now or datetime.now(KST)
    st = _today(now)
    max_pct, max_count = limits()
    until = st.get("until") or ""
    expired = False
    if st["extra_loss_pct"] > 0:
        try:
            expired = bool(until) and now >= datetime.fromisoformat(until)
        except ValueError:
            expired = True          # 깨진 값은 만료로 본다(안전측)
    return {
        "enabled": max_pct > 0 and max_count > 0,
        "active": st["extra_loss_pct"] > 0 and not expired,
        "extra_loss_pct": 0.0 if expired else st["extra_loss_pct"],
        "granted_pct": st["extra_loss_pct"],     # 만료돼도 오늘 준 총량은 남긴다
        "until": until,
        "count": st["count"],
        "max_pct": max_pct,
        "max_count": max_count,
        "history": st["history"],
        "expired": expired,
    }


def record_for(day: str) -> dict | None:
    """그날의 임시 해제 기록(매매일지용). 다른 날짜면 None.
    파일은 하루치만 들고 있으므로 지난 날짜는 일지에 저장된 사본이 근거가 된다."""
    d = _load_raw()
    return d if d.get("date") == day and d.get("history") else None


def grant(*, mode: str, extra_pct: float, pct_at: float,
          minutes: int | None = None, now: datetime | None = None) -> dict:
    """임시 해제를 부여한다. 상한을 넘으면 ValueError.

    mode=reset 이면 총량을 extra_pct 로 '설정'하고(지금 손익을 기준선으로 다시 셈),
    extend 면 기존 총량에 '더한다'. 어느 쪽이든 하루 총량 상한을 넘지 못한다.
    """
    now = now or datetime.now(KST)
    if mode not in MODES:
        raise ValueError(f"mode 는 {'/'.join(MODES)} 만 가능합니다")
    max_pct, max_count = limits()
    if max_pct <= 0 or max_count <= 0:
        raise ValueError(
            "가드 임시 해제가 꺼져 있습니다 — config risk.guard_override_max_pct "
            "와 guard_override_max_count 를 0보다 크게 설정하세요")
    extra = round(float(extra_pct), 4)
    if extra <= 0:
        raise ValueError("추가 허용치는 0보다 커야 합니다")

    st = _today(now)
    if st["count"] >= max_count:
        raise ValueError(f"하루 임시 해제 횟수 상한({max_count}회)에 도달했습니다")
    total = extra if mode == "reset" else round(st["extra_loss_pct"] + extra, 4)
    if total > max_pct:
        raise ValueError(
            f"하루 임시 허용 총량 상한 {max_pct:g}% 를 넘습니다(요청 반영 시 {total:g}%)")

    if minutes is not None:
        n = int(minutes)
        if not 1 <= n <= 600:
            raise ValueError("유효 시간은 1~600분 범위여야 합니다")
        until = now + timedelta(minutes=n)
    else:
        until = _close_at(now)
    if until <= now:
        raise ValueError("이미 장이 마감돼 임시 해제를 적용할 수 없습니다")

    st["extra_loss_pct"] = total
    st["until"] = until.isoformat(timespec="seconds")
    st["count"] += 1
    st["history"].append({
        "at": now.isoformat(timespec="seconds"),
        "mode": mode,
        "extra_pct": extra,
        "total_pct": total,
        "pct_at": round(float(pct_at), 4),      # 그때의 당일 실현손익률
        "until": st["until"],
    })
    _save(st)
    log.warning("일일 손실 가드 임시 해제(%s): 총 허용 +%.2f%% · %s 까지 "
                "(그때 실현손익 %+.2f%%, 오늘 %d회째)",
                mode, total, st["until"][11:16], pct_at, st["count"])
    return state(now)


def clear(now: datetime | None = None) -> dict:
    """임시 해제를 즉시 거둔다(이력은 남긴다)."""
    now = now or datetime.now(KST)
    st = _today(now)
    if st["extra_loss_pct"] > 0:
        st["history"].append({"at": now.isoformat(timespec="seconds"),
                              "mode": "clear", "extra_pct": 0.0,
                              "total_pct": 0.0, "pct_at": None, "until": ""})
        log.warning("일일 손실 가드 임시 해제 취소 — 설정 한도로 복귀")
    st["extra_loss_pct"] = 0.0
    st["until"] = ""
    _save(st)
    return state(now)


def _save(st: dict) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")


def reset_extra(pct_now: float, base_limit_pct: float) -> float:
    """'지금부터 다시 세기'에 필요한 추가 허용치.

    지금 -1.86% 이고 한도가 1.5% 면 -3.36% 에서 멈춰야 하므로 1.86% 가 필요하다.
    아직 이익 구간이면 추가 허용이 필요 없다(0).
    """
    return round(abs(min(0.0, float(pct_now))), 4)
