"""실거래 체결 원장(broker_fills) — **증권사가 원본이다** (2026-08-01).

## 왜

시스템 원장(positions)은 자기 주문 기록을 원본으로 삼고 증권사 데이터를
나중에 '보정'했다. 그래서 네 가지 괴리가 실재했다:

  ① 유령 — 주문번호만 받고 체결 안 된 포지션이 손익에 남음
  ② 수동 매매 — HTS 직접 매매가 원장에 아예 없음(untracked)
  ③ 모델 비용 — 수수료·거래세·슬리피지 3중 오류 실측(kiwoom/realized.py)
  ④ 놓친 청산 — WS 체결 이벤트를 놓치면 계좌엔 없는 포지션이 열려 있음

이 모듈은 방향을 뒤집는다: **체결 사실(무엇이 몇 주, 얼마에, 언제)은 증권사
체결내역(ka10076)이 원본**이고, 시스템 positions 는 의도(손절·목표·규칙 귀속)
레이어다. 의도는 증권사 원장에 존재하지 않으므로 시스템이 계속 소유한다.

## 동작 (main._fills_sync, 장중 60초 주기)

  ① store_today      — 당일 체결 스냅샷을 통째로 교체 저장(멱등)
  ② sync_from_fills  — 원장을 사실에 정렬:
       유령 void    (체결내역에 없는 당일 주문, 유예 3분)
       놓친 청산    (계좌 0 + 매도 체결 존재 → fills 가격으로 청산 기록)
       수동 매매    (주문번호가 원장에 없는 체결 → rule='external' 포지션.
                     손절·목표 없음 — **감시·기록만 하고 자동 청산하지 않는다**)
  ③ reconcile_executions — 기존 멱등 대사(실체결가·실비용 확정). ②가 붙인
       exit_ord_no 도 같은 사이클에 확정된다
  ④ store_daily      — 증권사 계산 실현손익(ka10074, 순액) 저장.
       **일일 가드가 이 값을 주값으로 쓴다**(ledger.realized_today) —
       모델값으로 가드가 판정하던 구멍(실측 하루 1,796원 과대계상)을 막는다

## 되돌리기

이 모듈이 죽어도 기존 경로(WS 체결 수신·모델 손익·수동 reconcile)는 그대로
돈다 — 정렬이 멈출 뿐이다. broker_daily 가 낡으면 가드는 모델값으로
자동 폴백한다(source='model' 로 표시).
"""
import hashlib
import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

GRACE_MIN = 3.0          # 유령 판정 유예 — 발주 직후 스냅샷 미반영 시간
FRESH_SEC = 180.0        # broker_daily 를 가드 주값으로 믿는 나이


def _conn() -> sqlite3.Connection:
    from . import ledger

    conn = ledger._conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS broker_fills (
            fill_key TEXT PRIMARY KEY,
            d TEXT NOT NULL, ord_no TEXT, code TEXT, name TEXT, side TEXT,
            qty INTEGER, price REAL, commission REAL, tax REAL,
            t TEXT, synced_at TEXT)""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS broker_daily (
            d TEXT PRIMARY KEY, realized REAL, commission REAL, tax REAL,
            synced_at TEXT)""")
    return conn


def _key(day: str, e: dict) -> str:
    raw = f"{day}:{e.get('ord_no')}:{e.get('time')}:{e.get('price')}:{e.get('qty')}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def store_today(execs: list[dict], day: str | None = None) -> int:
    """당일 체결 스냅샷 교체 저장 — REST 응답이 그날의 전체 진실이므로
    부분 upsert 가 아니라 통째로 갈아끼운다(멱등)."""
    day = day or datetime.now(KST).date().isoformat()
    ts = datetime.now(KST).isoformat(timespec="seconds")
    with _conn() as conn:
        conn.execute("DELETE FROM broker_fills WHERE d=?", (day,))
        conn.executemany(
            "INSERT OR REPLACE INTO broker_fills VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(_key(day, e), day, e.get("ord_no") or "", e.get("code") or "",
              e.get("name") or "", e.get("side") or "", int(e.get("qty") or 0),
              float(e.get("price") or 0), float(e.get("commission") or 0),
              float(e.get("tax") or 0), e.get("time") or "", ts)
             for e in execs])
    return len(execs)


def store_daily(day: str, parsed: dict) -> None:
    """증권사 계산 실현손익(순액) — ka10074 parse_daily 결과."""
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO broker_daily VALUES (?,?,?,?,?)",
            (day, float(parsed.get("realized") or 0),
             float(parsed.get("commission") or 0), float(parsed.get("tax") or 0),
             datetime.now(KST).isoformat(timespec="seconds")))


def today_fills(day: str | None = None) -> list[dict]:
    day = day or datetime.now(KST).date().isoformat()
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM broker_fills WHERE d=? ORDER BY t", (day,))]


def broker_realized_today(now: datetime | None = None,
                          fresh_sec: float = FRESH_SEC) -> float | None:
    """증권사 계산 당일 실현손익 — 최근 동기화분만 믿는다. 낡으면 None(모델 폴백)."""
    now = now or datetime.now(KST)
    day = now.date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT realized, synced_at FROM broker_daily WHERE d=?", (day,)
        ).fetchone()
    if not row or not row["synced_at"]:
        return None
    try:
        age = (now - datetime.fromisoformat(row["synced_at"])).total_seconds()
    except ValueError:
        return None
    return float(row["realized"]) if 0 <= age <= fresh_sec else None


# --------------------------------------------------------------------------
# 원장 정렬 — 사실(fills)에 맞춰 positions 를 고친다
# --------------------------------------------------------------------------
def sync_from_fills(day: str | None = None, holdings: dict[str, int] | None = None,
                    now: datetime | None = None) -> dict:
    """반환: {"ghost": n, "missed_exit": n, "external": n, "external_closed": n}."""
    from . import ledger

    day = day or datetime.now(KST).date().isoformat()
    now = now or datetime.now(KST)
    fills = today_fills(day)
    fill_ords = {f["ord_no"] for f in fills if f["ord_no"]}
    out = {"ghost": 0, "missed_exit": 0, "external": 0, "external_closed": 0}

    with ledger._conn() as conn:
        open_rows = [dict(r) for r in conn.execute(
            "SELECT * FROM positions WHERE status='open'")]

        # ① 유령 — 오늘 열린 미확정 포지션인데 체결내역 스냅샷에 주문번호가 없다.
        #    executions() 는 당일 범위라 **오늘 연 것만** 판정한다(과거 보유 오판 방지).
        for p in open_rows:
            if p.get("fill_confirmed") or not p.get("ord_no"):
                continue
            if str(p.get("opened") or "")[:10] != day:
                continue
            try:
                age_min = (now - datetime.fromisoformat(p["opened"])).total_seconds() / 60
            except ValueError:
                continue
            if age_min < GRACE_MIN or p["ord_no"] in fill_ords:
                continue
            conn.execute("UPDATE positions SET status='void', exit_reason='unfilled' "
                         "WHERE id=?", (p["id"],))
            out["ghost"] += 1
            log.warning("유령 회수(fills): %s %s ord_no=%s — 체결내역에 없음",
                        p.get("name"), p.get("symbol"), p.get("ord_no"))

    # ② 놓친 청산 — 계좌 보유 0 + 그 종목 매도 체결이 포지션 수량 이상.
    #    holdings 는 명시적 0 이어야 한다(조회 실패 None 이면 아무것도 안 한다).
    from . import ledger as _lg
    if holdings is not None:
        for p in [r for r in _lg.positions(status="open", limit=200)]:
            sym = _lg.executed_symbol(p)
            if holdings.get(sym, 0) != 0:
                continue
            sells = [f for f in fills if f["code"] == sym and f["side"] == "sell"]
            qty = sum(f["qty"] for f in sells)
            if qty < int(p.get("qty") or 0) or qty <= 0:
                continue
            amt = sum(f["price"] * f["qty"] for f in sells)
            avg = amt / qty
            ord_no = max(sells, key=lambda f: f["qty"])["ord_no"]
            _lg.close_position(p["id"], round(avg, 2), "fills_sync", ord_no=ord_no)
            out["missed_exit"] += 1
            log.warning("놓친 청산 기록(fills): %s %s %d주 @ %.0f",
                        p.get("name"), p.get("symbol"), p.get("qty"), avg)

    # ③ 수동(외부) 매매 — 주문번호가 원장 어디에도 없는 체결.
    #    external 포지션 자신의 ord_no 는 제외한다 — 포함하면 다음 동기화에서
    #    그 매수 체결이 '알려진 것'으로 걸러져 왕복 매도를 영영 못 닫는다.
    with ledger._conn() as conn:
        known = {r[0] for r in conn.execute(
            "SELECT ord_no FROM positions WHERE ord_no IS NOT NULL"
            " AND rule != 'external'")}
        known |= {r[0] for r in conn.execute(
            "SELECT exit_ord_no FROM positions WHERE exit_ord_no IS NOT NULL"
            " AND rule != 'external'")}
        ext = [f for f in fills if f["ord_no"] and f["ord_no"] not in known]
        by_code: dict[str, list[dict]] = {}
        for f in ext:
            by_code.setdefault(f["code"], []).append(f)
        for code, fs in by_code.items():
            buys = [f for f in fs if f["side"] == "buy"]
            sells = [f for f in fs if f["side"] == "sell"]
            bq, sq = sum(f["qty"] for f in buys), sum(f["qty"] for f in sells)
            if bq <= 0:
                continue          # 매도만 있는 외부 체결은 ②/대조 리포트의 영역
            pid = f"ext{day.replace('-', '')}{code}"
            b_avg = sum(f["price"] * f["qty"] for f in buys) / bq
            name = buys[0].get("name") or code
            row = conn.execute("SELECT * FROM positions WHERE id=?", (pid,)).fetchone()
            if bq > sq:           # 순매수 보유 중 → 외부 포지션 upsert
                if row is None:
                    conn.execute(
                        "INSERT INTO positions (id, opened, symbol, name, rule, side,"
                        " qty, model_entry, entry, stop, target, status, ord_no,"
                        " fill_confirmed, slippage_pct)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (pid, now.isoformat(timespec="seconds"), code, name,
                         "external", "long", bq - sq, round(b_avg, 2),
                         round(b_avg, 2), None, None, "open",
                         buys[0]["ord_no"], 1, 0.0))
                    out["external"] += 1
                    log.info("외부 매매 편입: %s %s %d주 @ %.0f (감시만 — 자동 청산 없음)",
                             name, code, bq - sq, b_avg)
                elif row["status"] == "open" and int(row["qty"]) != bq - sq:
                    conn.execute("UPDATE positions SET qty=?, entry=? WHERE id=?",
                                 (bq - sq, round(b_avg, 2), pid))
            elif sq >= bq and row is not None and row["status"] == "open":
                # 외부 왕복 종료 — 실비용 실현손익으로 닫는다
                s_avg = sum(f["price"] * f["qty"] for f in sells) / sq
                fee = sum(f["commission"] for f in fs)
                tax = sum(f["tax"] for f in sells)
                krw = ledger.actual_pnl_krw("long", b_avg, s_avg, bq, fee, tax)
                conn.execute(
                    "UPDATE positions SET closed=?, exit=?, exit_reason='external',"
                    " status='closed', pnl_krw=?, pnl_pct=?,"
                    " exit_fill_confirmed=1, exit_fee_krw=?, tax_krw=? WHERE id=?",
                    (now.isoformat(timespec="seconds"), round(s_avg, 2),
                     round(krw, 1),
                     round((krw / (b_avg * bq) * 100) if b_avg * bq else 0.0, 4),
                     fee, tax, pid))
                out["external_closed"] += 1
    return out


def divergence(day: str | None = None,
               holdings: dict[str, int] | None = None) -> dict:
    """시스템 원장 vs 실거래 원장 대조 리포트 — 조회 전용."""
    from . import ledger

    day = day or datetime.now(KST).date().isoformat()
    fills = today_fills(day)
    fill_ords = {f["ord_no"] for f in fills if f["ord_no"]}
    with ledger._conn() as conn:
        sys_pnl = conn.execute(
            "SELECT COALESCE(SUM(pnl_krw),0) FROM positions WHERE status='closed'"
            " AND substr(closed,1,10)=?", (day,)).fetchone()[0]
        opens = [dict(r) for r in conn.execute(
            "SELECT id, symbol, name, qty, ord_no, fill_confirmed, opened"
            " FROM positions WHERE status='open'")]
        known = {r[0] for r in conn.execute(
            "SELECT ord_no FROM positions WHERE ord_no IS NOT NULL")}
        known |= {r[0] for r in conn.execute(
            "SELECT exit_ord_no FROM positions WHERE exit_ord_no IS NOT NULL")}
        broker = conn.execute(
            "SELECT realized, synced_at FROM broker_daily WHERE d=?", (day,)
        ).fetchone()
    ghosts = [p for p in opens
              if not p["fill_confirmed"] and p.get("ord_no")
              and str(p.get("opened") or "")[:10] == day
              and p["ord_no"] not in fill_ords]
    unmatched = [f for f in fills if f["ord_no"] and f["ord_no"] not in known]
    qty_mismatch = []
    if holdings is not None:
        for p in opens:
            sym = ledger.executed_symbol(p) if hasattr(ledger, "executed_symbol") \
                else p["symbol"]
            have = holdings.get(sym)
            if have is not None and have != int(p.get("qty") or 0):
                qty_mismatch.append({"symbol": sym, "ledger_qty": p.get("qty"),
                                     "account_qty": have})
    br = float(broker["realized"]) if broker else None
    return {
        "day": day, "fills": len(fills),
        "system_realized_krw": round(float(sys_pnl or 0), 1),
        "broker_realized_krw": br,
        "realized_diff_krw": (round(float(sys_pnl or 0) - br, 1)
                              if br is not None else None),
        "ghost_suspects": ghosts, "unmatched_fills": unmatched,
        "qty_mismatch": qty_mismatch,
    }
