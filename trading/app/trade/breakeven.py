"""본전 이동 shadow — "손절선을 진입가로 올렸다면" 을 **기록만** 한다.

2026-07-28 실거래에서 나온 관찰이 출발점이다. 손절된 종목 일부가 익절가
근처까지 갔다가 되돌아 손절됐다 — 가져갈 수 있었던 수익을 놓쳤다는 뜻이다.

그런데 같은 날 MFE(최대 유리 변동)를 재보니 **손절 10건 중 2건**만 목표거리의
50% 이상에 닿았고, 8건은 9%도 못 갔다(5건은 진입가 위로 한 번도 못 올라갔다).
이틀치 34건으로 넓혀도 손절 18건 중 5건이다. 즉 "되돌릴 수익" 이 실재하는
표본은 생각보다 작다.

그래서 추세 분류기를 만들지 않는다. 모델을 하나 더 얹으면 발굴 3규칙 점수가
'그럴듯해서' 들어갔다가 1년 뒤 베타 프록시로 판명된 것과 같은 일이 된다.
대신 **결정론 규칙 하나**를 실제 청산과 나란히 돌려 값을 먼저 만든다.

  목표거리의 arm_at_pct% 에 닿으면 → 손절선을 진입가로 올린다(무장)
  무장된 뒤 진입가로 되돌아오면    → 그 자리에서 청산했다고 **가정**한다

## 이 모듈은 실제 청산에 관여하지 않는다

`positions` 를 읽기만 하고 쓰지 않는다. 별도 테이블에만 기록한다. 그래서
`enabled: false` 로 끄면 기록이 멈출 뿐, 되돌려야 할 실거래 상태가 없다 —
발굴 엔진 토글이 남긴 자국(감시목록 13행)과 같은 비대칭이 생기지 않는다.
쌓인 관측치까지 지우려면 `reset()`.

## 실제 청산과 같은 표본으로 잰다

봉의 고가로 MFE 를 재면 30초마다 도는 청산 루프가 실제로는 보지 못했을 값을
집계하게 된다 — 백테스트만 좋고 실전은 못 따라가는 전형적인 함정이다. 그래서
`observe()` 는 `_ledger_loop` 안에서 **실제 청산 판정과 같은 `price_of` · 같은
30초 주기**로 호출된다. 봉 고가 기준 MFE 와의 차이(`mfe_pct` vs 봉 분석값)가
곧 "실시간 구현이 놓치는 몫" 이다.

## 손익은 모델 공식끼리 비교한다

shadow 청산은 일어나지 않았으므로 증권사 실비용이 없다. 그래서 비교는
`ledger._net_pnl_pct`(모델) 로 **양쪽 다** 계산한다. 원장에 저장된 실현손익은
실비용이 반영돼 있어(#172) 그대로 빼면 비용 모델 차이가 델타에 섞인다.
참고용으로 원장 값도 함께 남기되, 판단은 모델끼리의 델타로 한다.

**본전 이동은 손익 본전이 아니다.** 진입가에 팔아도 수수료·거래세가 남아
-0.2% 안팎이다. 그 값이 그대로 shadow 손익에 들어간다.
"""
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from .. import settings
from . import ledger

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

DEFAULTS = {"enabled": True, "arm_at_pct": 50.0}


def cfg() -> dict:
    c = (settings.CONFIG.get("execution", {}) or {}).get("breakeven_shadow", {})
    return DEFAULTS | (c if isinstance(c, dict) else {})


def enabled() -> bool:
    return bool(cfg().get("enabled", True))


def arm_at_pct() -> float:
    try:
        return float(cfg().get("arm_at_pct", 50.0))
    except (TypeError, ValueError):
        return 50.0


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(ledger.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS breakeven_shadow (
            pos_id TEXT PRIMARY KEY,
            day TEXT, symbol TEXT, name TEXT, rule TEXT, side TEXT,
            opened TEXT, qty INTEGER, entry REAL, stop REAL, target REAL,
            peak REAL, peak_at TEXT,          -- 30초 표본 기준 최유리 가격
            mfe_pct REAL, reach_pct REAL,     -- 진입가 대비 % / 목표거리 대비 %
            armed INTEGER DEFAULT 0, armed_at TEXT,
            shadow_exit REAL, shadow_exit_at TEXT,
            settled INTEGER DEFAULT 0,
            actual_pct REAL, shadow_pct REAL, delta_krw REAL,
            ledger_pnl_krw REAL               -- 참고용(실비용 반영값)
        )"""
    )
    return conn


# --------------------------------------------------------------------------
# 관측 (청산 감시 루프가 매 주기 호출)
# --------------------------------------------------------------------------
def _reach(side: str, entry: float, target: float, peak: float) -> float | None:
    """목표거리 대비 도달률(%). 목표가 진입가와 같은 쪽이 아니면 판정 불가."""
    span = (target - entry) if side == "long" else (entry - target)
    if span <= 0:
        return None
    gain = (peak - entry) if side == "long" else (entry - peak)
    return gain / span * 100


def observe(price_of, now: datetime | None = None) -> int:
    """열려 있는 포지션의 최유리 가격을 갱신하고 무장·가상청산을 기록한다.

    **positions 를 수정하지 않는다.** 반환: 갱신된 행 수.
    """
    if not enabled():
        return 0
    now = now or datetime.now(KST)
    ts = now.isoformat(timespec="seconds")
    arm = arm_at_pct()
    touched = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open'").fetchall()
        for r in rows:
            p = price_of(r["symbol"])
            if p is None or not r["entry"]:
                continue
            p = float(p)
            side, entry = r["side"], float(r["entry"])
            cur = conn.execute(
                "SELECT * FROM breakeven_shadow WHERE pos_id=?", (r["id"],)
            ).fetchone()
            if cur is None:
                conn.execute(
                    "INSERT INTO breakeven_shadow (pos_id, day, symbol, name, rule,"
                    " side, opened, qty, entry, stop, target, peak, peak_at,"
                    " mfe_pct, reach_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["id"], str(r["opened"] or "")[:10], r["symbol"], r["name"],
                     r["rule"], side, r["opened"], r["qty"], entry, r["stop"],
                     r["target"], entry, ts, 0.0, 0.0),
                )
                cur = conn.execute(
                    "SELECT * FROM breakeven_shadow WHERE pos_id=?", (r["id"],)
                ).fetchone()

            peak = float(cur["peak"])
            better = p > peak if side == "long" else p < peak
            if better:
                peak, peak_at = p, ts
            else:
                peak_at = cur["peak_at"]

            mfe = (peak - entry) / entry * 100
            if side == "short":
                mfe = -mfe
            reach = _reach(side, entry, float(r["target"] or 0), peak)

            armed = int(cur["armed"] or 0)
            armed_at = cur["armed_at"]
            if not armed and reach is not None and reach >= arm:
                armed, armed_at = 1, ts

            # 무장된 뒤 진입가로 되돌아오면 그 자리에서 청산했다고 가정한다.
            # 한 번만 기록한다 — 이후 실제 포지션이 어디로 가든 shadow 는 끝났다.
            sx, sx_at = cur["shadow_exit"], cur["shadow_exit_at"]
            if armed and sx is None:
                back = p <= entry if side == "long" else p >= entry
                if back:
                    sx, sx_at = entry, ts

            conn.execute(
                "UPDATE breakeven_shadow SET peak=?, peak_at=?, mfe_pct=?,"
                " reach_pct=?, armed=?, armed_at=?, shadow_exit=?, shadow_exit_at=?"
                " WHERE pos_id=?",
                (round(peak, 2), peak_at, round(mfe, 4),
                 round(reach, 2) if reach is not None else None,
                 armed, armed_at, sx, sx_at, r["id"]),
            )
            touched += 1
    return touched


# --------------------------------------------------------------------------
# 정산 (실제 청산이 끝난 건을 모델 대 모델로 비교)
# --------------------------------------------------------------------------
def settle(now: datetime | None = None) -> int:
    """청산된 포지션의 shadow 결과를 확정한다. 반환: 이번에 정산된 건수."""
    n = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT b.*, p.exit AS real_exit, p.pnl_krw AS real_pnl_krw,"
            " p.status AS pstatus FROM breakeven_shadow b"
            " JOIN positions p ON p.id = b.pos_id"
            " WHERE b.settled=0 AND p.status='closed' AND p.exit IS NOT NULL"
        ).fetchall()
        for r in rows:
            side, entry, qty = r["side"], float(r["entry"]), int(r["qty"] or 0)
            actual = ledger._net_pnl_pct(side, entry, float(r["real_exit"]))
            # shadow 가 발동하지 않았으면 결과는 실제와 같다(델타 0).
            shadow = (ledger._net_pnl_pct(side, entry, float(r["shadow_exit"]))
                      if r["shadow_exit"] is not None else actual)
            delta = (shadow - actual) / 100 * entry * qty
            conn.execute(
                "UPDATE breakeven_shadow SET settled=1, actual_pct=?, shadow_pct=?,"
                " delta_krw=?, ledger_pnl_krw=? WHERE pos_id=?",
                (round(actual, 4), round(shadow, 4), round(delta, 1),
                 r["real_pnl_krw"], r["pos_id"]),
            )
            n += 1
    return n


# --------------------------------------------------------------------------
# 조회
# --------------------------------------------------------------------------
def rows_on(day: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM breakeven_shadow WHERE day=? ORDER BY opened", (day,)
        ).fetchall()
    return [dict(r) for r in rows]


def report(day: str) -> dict:
    """그날의 shadow 집계. **관측치일 뿐 판단이 아니다.**

    triggered  : 무장 후 진입가로 되돌아와 가상청산된 건수
    armed_only : 무장은 됐으나 되돌아오지 않은 건(=실제로 목표까지 간 쪽)
    delta_krw  : shadow − 실제 (모델 공식끼리). 양수면 본전 이동이 나았다.
    """
    rows = [r for r in rows_on(day) if r.get("settled")]
    if not rows:
        return {"trades": 0, "armed": 0, "triggered": 0, "delta_krw": 0.0,
                "helped": 0, "hurt": 0, "arm_at_pct": arm_at_pct(),
                "sampled_mfe_pct": 0.0}
    trig = [r for r in rows if r.get("shadow_exit") is not None]
    armed = [r for r in rows if r.get("armed")]
    return {
        "trades": len(rows),
        "armed": len(armed),
        "armed_only": len(armed) - len(trig),
        "triggered": len(trig),
        "delta_krw": round(sum(r.get("delta_krw") or 0 for r in rows), 0),
        "helped": sum(1 for r in rows if (r.get("delta_krw") or 0) > 0),
        "hurt": sum(1 for r in rows if (r.get("delta_krw") or 0) < 0),
        "arm_at_pct": arm_at_pct(),
        # 30초 표본으로 관측한 MFE 평균 — 봉 고가 기준 값보다 낮은 것이 정상이고,
        # 그 차이가 실시간 구현이 놓치는 몫이다.
        "sampled_mfe_pct": round(
            sum(r.get("mfe_pct") or 0 for r in rows) / len(rows), 3),
    }


def reset(day: str | None = None) -> int:
    """관측치 삭제(끄는 것만으로는 지워지지 않는다). 반환: 삭제 행 수."""
    with _conn() as conn:
        if day:
            return conn.execute(
                "DELETE FROM breakeven_shadow WHERE day=?", (day,)).rowcount
        return conn.execute("DELETE FROM breakeven_shadow").rowcount
