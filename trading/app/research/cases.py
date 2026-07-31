"""매매 케이스 원장 — 청산된 거래 하나를 '공부할 수 있는 형태'로 남긴다.

## 왜 필요한가

일지(`journal.py`)는 하루치 **집계**와 포지션 원본을 남긴다. 그런데 이번 세션에
답을 준 것들은 전부 집계에 없는 값이었다.

  · 진입 후 최대 유리/불리 이동(MFE/MAE) — 손절이 경로에서 맞았는지
  · MFE / 목표폭 — 목표가 애초에 닿을 자리였는지
  · 진입가가 직전 30분 범위의 어디였나 — "진입 시점을 잘못 봤나" 의 답
  · 3분 안에 유리하게 갔는가

그리고 그 계산은 **일회성 스크립트로 하고 버렸다.** 같은 질문이 다시 오면
분봉을 다시 훑어야 하고, 훑을 수 있는 종목은 1분봉이 있는 것뿐이다. 이 표가
그 계산을 한 번만 하게 만든다.

**사후 사실만 담는다.** 예측도 점수도 넣지 않는다 — 그것이 필요해지면 그때
이 표를 재료로 만들지, 이 표가 주장을 하지는 않는다.

## 진입 시점 판단이 나빴다는 실측 (2026-07-31, 107건)

    MFE 평균 +1.198%  ·  MAE 평균 -1.477%      불리 쪽이 더 크다
    같은 종목·같은 날·진입 +-30분 무작위 대조군과 비교해 -0.382%p
    (부트스트랩 95% CI [-0.656, -0.083] — 0 을 포함하지 않는다)
    107건 중 63건(59%)이 "같은 30분 안 아무 때나" 보다 나빴다

MFE 는 대조군과 사실상 같고(-0.088%p) **MAE 만 -0.298%p 더 깊다.** 오를 종목을
못 고르는 게 아니라 밀리기 직전에 들어간다. 이 표가 그 관측을 매일 이어간다.
"""
import json
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "backtest.db"
COST_PCT = 0.33          # 왕복 모델 비용 — 케이스 라벨의 손익분기선


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cases (
            pos_id TEXT PRIMARY KEY,
            d TEXT NOT NULL, symbol TEXT, name TEXT, rule TEXT, side TEXT,
            opened TEXT, closed TEXT, hold_min REAL, hour INTEGER,
            entry REAL, exit REAL, pnl_pct REAL, pnl_krw REAL,
            exit_reason TEXT, outcome TEXT,
            stop_pct REAL, target_pct REAL,          -- 진입가 기준 설계 폭
            mfe_pct REAL, mae_pct REAL,              -- 진입 후 유리/불리 최대 이동
            mfe_3min REAL,                           -- 진입 직후 3분
            mfe_over_target REAL, mae_over_stop REAL,
            entry_pos REAL,                          -- 직전 30분 범위 내 진입 위치 0~1
            bars INTEGER,                            -- 근거가 된 분봉 수(0이면 미측정)
            lessons TEXT,                            -- 자동 라벨(JSON 배열)
            built_at TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cases_d ON cases (d)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_cases_rule ON cases (rule, d)")
    return conn


# --------------------------------------------------------------------------
# 순수 계산 — 분봉과 포지션 하나로 케이스 한 건
# --------------------------------------------------------------------------
def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def excursions(pos: dict, bars: list[tuple], pre: list[tuple] | None = None) -> dict:
    """포지션 + 보유 구간 분봉 → 사후 측정치.

    bars: (ts, high, low, close) — 진입~청산 구간, 시간순.
    pre : 진입 **직전** 30분 봉(같은 형태). 없으면 진입 위치를 내지 않는다.

    부호 규약: `mfe_pct` 는 내 방향으로 간 최대(양수가 유리), `mae_pct` 는 반대로
    간 최대(**음수가 정상**). 절댓값으로 뭉개면 "한 번도 안 밀린 건" 과
    "1% 밀린 건" 이 같아 보인다.
    """
    entry = _num(pos.get("entry")) or 0.0
    if entry <= 0 or len(bars) < 2:
        return {"bars": len(bars)}
    long = (pos.get("side") or "long") == "long"
    hi = max(b[1] for b in bars)
    lo = min(b[2] for b in bars)
    mfe = (hi - entry) / entry * 100 if long else (entry - lo) / entry * 100
    mae = (lo - entry) / entry * 100 if long else (entry - hi) / entry * 100
    seg3 = bars[:3]
    h3, l3 = max(b[1] for b in seg3), min(b[2] for b in seg3)
    mfe3 = (h3 - entry) / entry * 100 if long else (entry - l3) / entry * 100

    stop, target = _num(pos.get("stop")), _num(pos.get("target"))
    stop_pct = abs(entry - stop) / entry * 100 if stop else None
    target_pct = abs(target - entry) / entry * 100 if target else None

    out = {
        "bars": len(bars),
        "mfe_pct": round(mfe, 4), "mae_pct": round(mae, 4),
        "mfe_3min": round(mfe3, 4),
        "stop_pct": round(stop_pct, 4) if stop_pct else None,
        "target_pct": round(target_pct, 4) if target_pct else None,
        "mfe_over_target": round(mfe / target_pct, 4) if target_pct else None,
        # 불리 이동은 음수이므로 크기로 바꿔 비교한다. 1.0 을 넘으면 손절선을
        # 관통했다는 뜻 — 실측 5거래일에서 43%가 여기 해당했다.
        "mae_over_stop": round(abs(min(mae, 0.0)) / stop_pct, 4) if stop_pct else None,
    }
    if pre and len(pre) >= 10:
        ph, pl = max(b[1] for b in pre), min(b[2] for b in pre)
        if ph > pl:
            pos_in = (entry - pl) / (ph - pl) if long else (ph - entry) / (ph - pl)
            out["entry_pos"] = round(pos_in, 4)
    return out


def lessons_for(case: dict) -> list[str]:
    """케이스에서 **기계적으로 확정되는** 관찰만 라벨로 붙인다.

    해석이 아니라 사실이다 — "왜 졌나" 를 쓰지 않고 "무엇이 그러했나" 만 쓴다.
    해석은 사람과 요약 모델의 몫이고, 그 재료가 이 라벨이다.
    """
    out: list[str] = []
    mfe, mae = case.get("mfe_pct"), case.get("mae_pct")
    if mfe is not None and mfe <= 0:
        out.append("한 번도 진입가 위로 못 갔다")
    if case.get("mfe_3min") is not None and case["mfe_3min"] <= 0:
        out.append("진입 후 3분 안에 유리하게 못 갔다")
    if mfe is not None and 0 < mfe <= COST_PCT:
        out.append(f"최대 유리 이동이 왕복 비용({COST_PCT}%)을 못 넘었다")
    mot = case.get("mfe_over_target")
    if mot is not None:
        if mot >= 1.0 and case.get("exit_reason") != "target":
            out.append("목표에 닿았는데 목표로 청산되지 않았다")
        elif mot < 0.3:
            out.append("목표폭의 30%도 못 갔다 — 목표가 너무 멀었다")
    mos = case.get("mae_over_stop")
    if mos is not None and mos > 1.0:
        out.append("손절선을 관통했다(불리 이동 > 손절폭)")
    sp = case.get("stop_pct")
    if sp is not None:
        lo = float(settings.RULES.get("min_stop_pct") or 0)
        hi = float(settings.RULES.get("max_stop_pct") or 0)
        if (lo and sp < lo) or (hi and sp > hi):
            out.append(f"체결 후 손절폭 {sp:.2f}% 가 대역({lo}~{hi}%) 밖이다")
    ep = case.get("entry_pos")
    if ep is not None and ep >= 0.8:
        out.append("직전 30분 범위의 상단 20%에서 샀다")
    if case.get("hour") is not None and case["hour"] >= 14:
        out.append("장 후반 진입 — 마감 정리까지 시간이 짧다")
    return out


# --------------------------------------------------------------------------
# 적재
# --------------------------------------------------------------------------
def _bars_for(symbol: str, start: str, end: str) -> list[tuple]:
    """구간을 **직접 질의한다.** `load_bars` 로 최근 N봉을 받아 자르지 않는다.

    종전 구현은 `load_bars(limit=2000)` 뒤에 구간을 걸렀다. 2,000분봉은 약
    5거래일인데 심층 백필된 종목은 4,500봉을 갖고 있어, **오래된 날의 케이스가
    조용히 잘렸다.** 소급 적재 실측(2026-07-31): 그 구현으로 만든 원장이
    `never_up` 18건 / `target_reached` 22건을 냈는데 같은 표본의 직접 측정은
    14건 / 25건이었다. 평균값(MFE·MAE)은 거의 같아서 **집계만 보면 안 드러난다** —
    구간이 짧아진 케이스만 최고·최저가 덜 잡힌 것이기 때문이다.

    잘린 것이 최근 구간이 아니라 **과거 구간**이라 오늘 만드는 케이스는 멀쩡하고
    소급분만 틀린다. 이런 종류가 가장 늦게 발견된다.
    """
    from ..data.store import _conn as bars_conn

    with bars_conn() as conn:
        rows = conn.execute(
            "SELECT ts, high, low, close FROM bars WHERE symbol=? AND tf='1m' "
            "AND ts >= ? AND ts <= ? ORDER BY ts", (symbol, start, end)).fetchall()
    return [(r[0], float(r[1]), float(r[2]), float(r[3])) for r in rows]


def build_case(pos: dict) -> dict | None:
    """포지션 하나 → 케이스 한 건. 분봉이 없으면 측정치 없이 뼈대만 남긴다.

    분봉이 없다고 케이스를 **버리지 않는다.** 1분봉은 395종목뿐이라 버리면
    표본이 조용히 그쪽으로 치우친다 — `bars=0` 으로 남겨 두면 나중에 어느
    케이스가 미측정인지 셀 수 있다.
    """
    if not pos.get("id") or not pos.get("opened") or not pos.get("closed"):
        return None
    opened, closed = str(pos["opened"]), str(pos["closed"])
    case = {
        "pos_id": pos["id"], "d": opened[:10],
        "symbol": pos.get("symbol"), "name": pos.get("name"),
        "rule": pos.get("rule"), "side": pos.get("side"),
        "opened": opened, "closed": closed,
        "entry": _num(pos.get("entry")), "exit": _num(pos.get("exit")),
        "pnl_pct": _num(pos.get("pnl_pct")), "pnl_krw": _num(pos.get("pnl_krw")),
        "exit_reason": pos.get("exit_reason"), "outcome": pos.get("outcome"),
        "hour": int(opened[11:13]) if len(opened) >= 13 else None,
    }
    try:
        t0, t1 = datetime.fromisoformat(opened), datetime.fromisoformat(closed)
        case["hold_min"] = round((t1 - t0).total_seconds() / 60, 2)
        pre_from = (t0 - timedelta(minutes=30)).isoformat()
    except (TypeError, ValueError):
        case["hold_min"] = None
        pre_from = opened
    bars = _bars_for(case["symbol"], opened, closed)
    pre = _bars_for(case["symbol"], pre_from, opened)
    case.update(excursions(pos, bars, pre))
    case["lessons"] = lessons_for(case)
    return case


def save(cases: list[dict], now: datetime | None = None) -> int:
    """케이스를 적재한다(포지션 id 기준 멱등). 반환: 쓴 행 수.

    덮어쓰기를 허용하는 이유: 대사(reconcile)로 실체결가·실비용이 나중에
    채워지면 손익과 라벨이 바뀐다. 케이스는 **지금 아는 가장 정확한 사실**이어야
    한다 — 원본은 `positions` 가 계속 갖고 있다.
    """
    if not cases:
        return 0
    ts = (now or datetime.now(KST)).isoformat(timespec="seconds")
    cols = ("pos_id", "d", "symbol", "name", "rule", "side", "opened", "closed",
            "hold_min", "hour", "entry", "exit", "pnl_pct", "pnl_krw",
            "exit_reason", "outcome", "stop_pct", "target_pct", "mfe_pct",
            "mae_pct", "mfe_3min", "mfe_over_target", "mae_over_stop",
            "entry_pos", "bars", "lessons", "built_at")
    with _conn() as conn:
        conn.executemany(
            f"INSERT OR REPLACE INTO cases ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [tuple(json.dumps(c.get(k) or [], ensure_ascii=False) if k == "lessons"
                   else (ts if k == "built_at" else c.get(k)) for k in cols)
             for c in cases],
        )
    return len(cases)


def build_day(day: str, now: datetime | None = None) -> int:
    """그날 청산된 거래를 전부 케이스로 만든다. 반환: 적재 건수."""
    from ..trade import ledger

    with ledger._conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='closed' "
            "AND substr(closed,1,10)=?", (day,))]
    from ..trade.ledger import outcome as _outcome
    cases = []
    for r in rows:
        r["outcome"] = _outcome(r)
        c = build_case(r)
        if c:
            cases.append(c)
    n = save(cases, now)
    log.info("케이스 적재 %s: %d건 (분봉 확보 %d건)",
             day, n, sum(1 for c in cases if c.get("bars")))
    return n


# --------------------------------------------------------------------------
# 조회 — 일지 프롬프트에 주입할 선례
# --------------------------------------------------------------------------
def stats(days: int = 30) -> dict:
    """케이스 원장 요약 — 이번 세션이 일회성 스크립트로 냈던 값들."""
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM cases WHERE bars > 0 ORDER BY d DESC LIMIT ?",
            (days * 60,))]
    if not rows:
        return {"n": 0}

    def avg(key):
        vs = [r[key] for r in rows if r.get(key) is not None]
        return round(sum(vs) / len(vs), 4) if vs else None

    reached = [r for r in rows if (r.get("mfe_over_target") or 0) >= 1.0]
    return {
        "n": len(rows),
        "mfe_pct": avg("mfe_pct"), "mae_pct": avg("mae_pct"),
        "mfe_3min": avg("mfe_3min"), "entry_pos": avg("entry_pos"),
        "never_up": sum(1 for r in rows if (r.get("mfe_pct") or 0) <= 0),
        "target_reached": len(reached),
        "target_exited": sum(1 for r in reached if r.get("exit_reason") == "target"),
        "stop_breached": sum(1 for r in rows if (r.get("mae_over_stop") or 0) > 1.0),
    }


def similar(rule: str | None, hour: int | None, limit: int = 5,
            exclude_day: str | None = None) -> list[dict]:
    """같은 규칙·같은 시간대의 지난 케이스 — 오늘을 견줄 선례.

    같은 날은 제외한다. 오늘 일지를 쓰면서 오늘 거래를 '선례' 로 인용하면
    같은 사실을 두 번 세는 것이고, 요약이 그걸 근거처럼 읽는다.
    """
    sql = "SELECT * FROM cases WHERE bars > 0"
    args: list = []
    if rule:
        sql += " AND rule = ?"
        args.append(rule)
    if hour is not None:
        sql += " AND hour BETWEEN ? AND ?"
        args += [hour - 1, hour + 1]
    if exclude_day:
        sql += " AND d <> ?"
        args.append(exclude_day)
    sql += " ORDER BY d DESC, pos_id LIMIT ?"
    args.append(limit)
    with _conn() as conn:
        out = []
        for r in conn.execute(sql, args):
            c = dict(r)
            try:
                c["lessons"] = json.loads(c["lessons"] or "[]")
            except json.JSONDecodeError:
                c["lessons"] = []
            out.append(c)
    return out


def build_range(days: int = 60, now: datetime | None = None) -> dict:
    """지난 `days` 일의 청산 거래를 전부 케이스로 만든다 — **소급 적재**.

    `build_day` 는 일지 생성 경로에서 그날치만 만든다. 이미 지나간 일지는
    아무도 만들어 주지 않으므로 여기서 한 번에 채운다. 멱등이라 여러 번 돌려도
    같고, 대사로 실체결가가 채워진 뒤 다시 돌리면 그 값으로 갱신된다.

    반환: {일자: 건수}. 어느 날이 비었는지가 그대로 보여야 한다 —
    합계만 돌려주면 '분봉이 없어 못 만든 날' 이 침묵으로 사라진다.
    """
    from ..trade import ledger

    end = (now or datetime.now(KST)).date()
    with ledger._conn() as conn:
        rows = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(closed,1,10) FROM positions "
            "WHERE status='closed' AND closed IS NOT NULL "
            "AND substr(closed,1,10) >= ? ORDER BY 1",
            ((end - timedelta(days=days)).isoformat(),))]
    return {day: build_day(day, now) for day in rows if day}


def coverage() -> dict:
    """케이스 원장의 구멍 — 분봉이 없어 측정 못 한 비율.

    1분봉은 유니버스 전체가 아니라 감시목록·로스터에만 있다. 미측정 건을
    세지 않으면 평균이 조용히 '분봉이 있는 종목' 쪽으로 치우친다.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, SUM(CASE WHEN bars > 0 THEN 1 ELSE 0 END) AS measured,"
            " MIN(d) AS first_day, MAX(d) AS last_day FROM cases").fetchone()
    n, measured = row["n"] or 0, row["measured"] or 0
    return {"n": n, "measured": measured, "unmeasured": n - measured,
            "measured_pct": round(measured / n * 100, 1) if n else 0.0,
            "first_day": row["first_day"], "last_day": row["last_day"]}
