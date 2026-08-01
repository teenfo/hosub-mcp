"""실거래 성과 로그 — 승인·발주된 주문의 체결/청산/실현손익을 추적한다.

딥리서치 검증의 가장 강한 긍정 발견은 '실력의 지속성은 실재한다'였다. 그걸
확인하는 유일한 방법이 내 실제 체결을 비용 포함해 측정하는 것이므로, 제안·발주
상태만 남기던 orders 에 더해 여기서 **진입가·청산가·실현손익·슬리피지**를 남긴다.

포지션은 신호 종목의 가격 흐름으로 추적한다(숏은 인버스 ETF로 집행하지만
손익은 백테스터와 동일하게 '신호 종목의 역방향 수익률'로 근사 — 일관성 유지).
장중 30초 주기로 손절/목표 터치를 확인하고, 장 마감에 미청산분을 종가로 정리한다.
"""
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from ..data import store

KST = ZoneInfo("Asia/Seoul")
log = logging.getLogger(__name__)
DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY,
            opened TEXT NOT NULL, symbol TEXT, name TEXT, rule TEXT, side TEXT,
            qty INTEGER, model_entry REAL, entry REAL, stop REAL, target REAL,
            closed TEXT, exit REAL, exit_reason TEXT,
            pnl_pct REAL, pnl_krw REAL, slippage_pct REAL,
            status TEXT NOT NULL,         -- open / closed
            ord_no TEXT, exec_symbol TEXT, fill_confirmed INTEGER DEFAULT 0
        )"""
    )
    # 기존 DB(구버전) 호환: 없는 컬럼만 추가
    for col, ddl in (("ord_no", "TEXT"), ("exec_symbol", "TEXT"),
                     ("fill_confirmed", "INTEGER DEFAULT 0"),
                     # 청산 주문번호와 실측 반영 여부 — 시장가 청산의 실체결가를
                     # 되받아 손익을 정정하기 위해 필요하다(이론가로 남기지 않는다).
                     ("exit_ord_no", "TEXT"),
                     ("exit_fill_confirmed", "INTEGER DEFAULT 0"),
                     ("model_exit", "REAL"),
                     # 증권사가 실제로 부과한 비용. 있으면 모델 대신 이걸 쓴다
                     # (_net_pnl_pct 주석 참조 — 모델은 세 군데가 틀렸다).
                     # 진입·청산을 나눠 두는 이유는 **재대사 멱등성**이다. 한 칸에
                     # 누적하면 체결내역을 두 번 훑을 때 비용이 두 배가 된다.
                     ("entry_fee_krw", "REAL"),
                     ("exit_fee_krw", "REAL"),
                     ("tax_krw", "REAL"),
                     # 매매 데스크가 장중에 갱신하는 손절·익절선. **원본(stop/target)은
                     # 건드리지 않는다** — 갱신 규칙이 나빴을 때 되돌릴 기준이 남아야
                     # 하고, 일지·백테스트·본전 이동 shadow 가 "원래 설계가 무엇이었나"
                     # 를 계속 읽어야 한다.
                     ("stop_live", "REAL"),
                     ("target_live", "REAL"),
                     ("lines_updated", "TEXT"),
                     # 손절선이 **움직인** 시각. 시간 손절의 기준점이다 —
                     # 손절선이 올라갔다는 것은 아직 방향이 맞다는 뜻이므로
                     # 보유시간을 그 시점부터 다시 센다(사용자 지침 2026-07-29).
                     ("stop_moved", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE positions ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    conn.execute(
        """CREATE TABLE IF NOT EXISTS exec_fills (
            ts TEXT, ord_no TEXT, symbol TEXT, price REAL, qty INTEGER,
            state TEXT, matched INTEGER
        )"""
    )
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN exit_pending INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    return conn


# 주문체결 실시간(type 00) FID 기본 매핑 — 라이브 체결로 검증 후 config 로 덮어쓸 것.
_DEFAULT_EXEC_FIDS = {
    "ord_no": "9203", "symbol": "9001", "state": "913",
    "price": "910", "qty": "911", "ts": "908",
}


def parse_execution(values: dict, fids: dict | None = None) -> dict:
    """주문체결 실시간 values → {ord_no, symbol, price, qty, state, ts}.
    필드 코드는 계정/환경마다 다를 수 있어 config(execution.fids)로 조정 가능."""
    f = fids or settings.CONFIG.get("execution", {}).get("fids", _DEFAULT_EXEC_FIDS)
    def num(key, cast):
        try:
            return cast(abs(float(values.get(f.get(key, ""), 0) or 0)))
        except (TypeError, ValueError):
            return cast(0)
    sym = str(values.get(f.get("symbol", ""), "") or "").lstrip("A_").strip()
    return {
        "ord_no": str(values.get(f.get("ord_no", ""), "") or "").strip(),
        "symbol": sym[:6] if sym[:6].isdigit() else sym,
        "state": str(values.get(f.get("state", ""), "") or "").strip(),
        "price": num("price", float),
        "qty": num("qty", int),
        "ts": str(values.get(f.get("ts", ""), "") or "").strip(),
    }


def record_fill(fill: dict) -> bool:
    """실측 체결을 기록하고, 주문번호가 매칭되면 오픈 포지션의 진입가를 실측으로 갱신.
    exec 종목이 신호 종목과 같을 때(롱)만 진입가를 정밀 갱신하고, 다르면(숏=인버스
    ETF) 감사 기록만 남긴다. 반환: 포지션 진입가를 갱신했으면 True."""
    ord_no = fill.get("ord_no") or ""
    price, qty = fill.get("price") or 0, fill.get("qty") or 0
    updated = False
    with _conn() as conn:
        row = None
        if ord_no:
            row = conn.execute(
                "SELECT * FROM positions WHERE ord_no=? AND status='open'", (ord_no,)
            ).fetchone()
        if row and price > 0 and fill.get("symbol") == row["symbol"]:
            model = row["model_entry"] or price
            slip = (price - model) / model * 100 if model else 0.0
            conn.execute(
                "UPDATE positions SET entry=?, qty=?, slippage_pct=?, fill_confirmed=1 "
                "WHERE id=?",
                (float(price), int(qty) or row["qty"], round(slip, 4), row["id"]),
            )
            _apply_refit(conn, row, float(price))
            updated = True
        elif ord_no and price > 0:
            # 청산 체결 — 이론가로 적어 둔 손익을 실체결가로 정정한다.
            # (진입과 주문번호가 다르고 이미 status='closed' 라 위 조회에 걸리지 않는다)
            ex = conn.execute(
                "SELECT * FROM positions WHERE exit_ord_no=? AND status='closed'",
                (ord_no,)
            ).fetchone()
            if ex and float(price) != (ex["exit"] or 0):
                net = _net_pnl_pct(ex["side"], ex["entry"], float(price))
                conn.execute(
                    "UPDATE positions SET exit=?, pnl_pct=?, pnl_krw=?, "
                    "exit_fill_confirmed=1 WHERE id=?",
                    (round(float(price), 2), round(net, 4),
                     round(ex["qty"] * ex["entry"] * net / 100, 1), ex["id"]),
                )
                log.info("청산 실체결 반영 %s: %s → %s (%+.2f%%)", ex["symbol"],
                         ex["exit"], price, net)
                updated = True
            elif ex:
                conn.execute(
                    "UPDATE positions SET exit_fill_confirmed=1 WHERE id=?", (ex["id"],))
        conn.execute(
            "INSERT INTO exec_fills VALUES (?,?,?,?,?,?,?)",
            (datetime.now(KST).isoformat(timespec="seconds"), ord_no,
             fill.get("symbol", ""), float(price), int(qty),
             fill.get("state", ""), 1 if row else 0),
        )
    return updated


def executed_symbol(row) -> str:
    """이 포지션이 **실제로 계좌에서 오간 종목코드**.

    숏 신호는 현물 계좌에서 공매도가 안 되므로 인버스 ETF 매수로 바뀌어 나간다
    (`orders.propose` → `exec_symbol`). 계좌 잔고와 대조할 때는 반드시 이 값을
    써야 한다 — 원 종목으로 보면 "계좌에 없다" 가 되어 실보유를 유령으로 읽는다.
    """
    try:
        return str(row["exec_symbol"] or row["symbol"])
    except (KeyError, IndexError, TypeError):
        return str(row["symbol"])


def _substituted(row, code: str) -> bool:
    """체결된 종목이 포지션의 기준 종목과 다른가(인버스 ETF 대체 등)."""
    return bool(code) and code != str(row["symbol"])


def reconcile_executions(execs: list[dict]) -> dict:
    """증권사 체결내역(ka10076)으로 원장의 추정값을 실측으로 확정한다.

    지금 원장의 진입·청산가는 주문체결 실시간을 못 받으면 **1분봉 종가 근사**다
    (`open_position` 의 `latest_price` 폴백). 실측 2026-07-28 기준 진입 8건 ·
    청산 11건이 미확정이었다. 실시간을 놓쳐도 마감 후 이걸 한 번 돌리면 메워진다.

    `ord_no` 가 조인 키다 — 원장이 이미 `ord_no`/`exit_ord_no` 를 들고 있고
    체결내역이 같은 번호를 준다. 새로 이어 붙일 것이 없다.

    같은 주문번호에 체결이 여러 건이면(분할 체결) **수량가중 평균가**로 합치고
    비용은 더한다. 한 건만 보고 확정하면 나머지 체결이 통째로 사라진다.

    ## 대체 종목(인버스 ETF)으로 나간 주문은 가격을 덮지 않는다

    숏 신호는 현물 계좌에서 공매도가 안 되므로 `settings.INVERSE_ETF` 매수로
    바뀌어 나간다(`orders.propose`). 그때 체결가는 **ETF 의 가격**이고 포지션의
    손절·목표·손익은 **원 종목 기준**이다. 주문번호만 보고 덮으면 기준이 무너진다.

    실측 2026-07-29: LG디스플레이 숏(기준가 9,020)에 KODEX 인버스 체결가
    1,235 가 들어가 슬리피지가 −86.3% 로 기록됐다. 삼성중공업 숏도 20,450 →
    1,236(−94.0%). 손익은 원 종목 기준으로 이미 계산돼 있어 **숫자만 서로
    어긋난 채 남았다.** 그래서 체결 종목이 다르면 확정 표시와 비용만 채운다.

    반환: {entries, exits} — 각각 갱신한 포지션 수.
    """
    agg: dict[str, dict] = {}
    for e in execs:
        ord_no, qty = e.get("ord_no") or "", int(e.get("qty") or 0)
        if not ord_no or qty <= 0:
            continue
        a = agg.setdefault(ord_no, {"amt": 0.0, "qty": 0, "fee": 0.0, "tax": 0.0,
                                    "code": str(e.get("code") or "")})
        a["amt"] += float(e.get("price") or 0) * qty
        a["qty"] += qty
        a["fee"] += float(e.get("commission") or 0)
        a["tax"] += float(e.get("tax") or 0)

    entries = exits = 0
    with _conn() as conn:
        for ord_no, a in agg.items():
            price = a["amt"] / a["qty"] if a["qty"] else 0.0
            if price <= 0:
                continue
            row = conn.execute(
                "SELECT * FROM positions WHERE ord_no=?", (ord_no,)).fetchone()
            if row:
                if _substituted(row, a["code"]):
                    conn.execute(
                        "UPDATE positions SET entry_fee_krw=?, fill_confirmed=1 "
                        "WHERE id=?", (a["fee"], row["id"]))
                    entries += 1
                    continue
                model = row["model_entry"] or price
                conn.execute(
                    "UPDATE positions SET entry=?, qty=?, slippage_pct=?, "
                    "entry_fee_krw=?, fill_confirmed=1 WHERE id=?",
                    (round(price, 2), a["qty"],
                     round((price - model) / model * 100, 4) if model else 0.0,
                     a["fee"], row["id"]),
                )
                _apply_refit(conn, row, round(price, 2))
                entries += 1
                continue
            ex = conn.execute(
                "SELECT * FROM positions WHERE exit_ord_no=? AND status='closed'",
                (ord_no,)).fetchone()
            if ex:
                if _substituted(ex, a["code"]):
                    conn.execute(
                        "UPDATE positions SET exit_fee_krw=?, tax_krw=?, "
                        "exit_fill_confirmed=1 WHERE id=?",
                        (a["fee"], a["tax"], ex["id"]))
                    exits += 1
                    continue
                conn.execute(
                    "UPDATE positions SET exit=?, exit_fee_krw=?, tax_krw=?, "
                    "exit_fill_confirmed=1 WHERE id=?",
                    (round(price, 2), a["fee"], a["tax"], ex["id"]),
                )
                exits += 1
    if entries or exits:
        apply_actual_costs()
    return {"entries": entries, "exits": exits}


def apply_actual_costs() -> int:
    """양쪽 체결이 다 확정된 청산 포지션의 손익을 **실비용 기준**으로 다시 낸다.

    한쪽만 확정된 것은 건드리지 않는다 — 비용이 반쪽이라 모델보다 더 틀린다.
    반환: 갱신한 포지션 수.
    """
    n = 0
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='closed'"
            " AND fill_confirmed=1 AND exit_fill_confirmed=1").fetchall()
        for r in rows:
            fee = (r["entry_fee_krw"] or 0) + (r["exit_fee_krw"] or 0)
            krw = actual_pnl_krw(r["side"], r["entry"], r["exit"], r["qty"],
                                 fee, r["tax_krw"] or 0)
            cost_basis = (r["entry"] or 0) * (r["qty"] or 0)
            pct = (krw / cost_basis * 100) if cost_basis else 0.0
            if (round(krw, 1), round(pct, 4)) == (r["pnl_krw"], r["pnl_pct"]):
                continue
            conn.execute("UPDATE positions SET pnl_krw=?, pnl_pct=? WHERE id=?",
                         (round(krw, 1), round(pct, 4), r["id"]))
            n += 1
    return n


def fills(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM exec_fills ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def latest_price(symbol: str) -> float | None:
    """최근 1분봉 종가(체결가 근사)."""
    df = store.load_bars(symbol, "1m", limit=1)
    return None if df.empty else float(df["close"].iloc[-1])


def _net_pnl_pct(side: str, entry: float, exit_px: float) -> float:
    """**모델** 비용 반영 실현손익률. 백테스터와 동일 공식.

    실체결 비용을 아직 못 받은 포지션에만 쓴다. 증권사 실비용이 도착하면
    `apply_actual_costs()` 가 이 값을 덮어쓴다.

    백테스터에서는 이 공식이 맞다 — 모델가로 시뮬레이션하므로 슬리피지를 따로
    빼야 한다. **실거래 원장에 그대로 쓴 것이 잘못이었다**(2026-07-28 실측,
    마키나락스 3건 대조에서 건당 59~72원 과대 손실):

      수수료   0.015%/편도(16원) vs 실제 **10원 고정**          → 과대
      거래세   0.15%             vs 실제 **약 0.20%**           → 과소
      슬리피지 0.05%×2 를 차감    vs **이미 체결가에 들어 있다** → 이중계상

    셋이 상쇄돼 합계는 비슷해 보였지만 개별로는 전부 틀렸다.
    """
    c = settings.COSTS
    raw = (exit_px - entry) / entry * 100
    if side == "short":
        raw = -raw
    return raw - c.get("commission_pct", 0.015) * 2 - c.get("sell_tax_pct", 0.20) \
        - c.get("slippage_bp", 5) / 100 * 2


def actual_pnl_krw(side: str, entry: float, exit_px: float, qty: int,
                   fee: float, tax: float) -> float:
    """실체결가 + 실비용으로 낸 실현손익(원). 슬리피지는 빼지 않는다 — 이미
    체결가 안에 있다.

    증권사 `tdy_sel_pl` 과 같은 정의다(실측 확인: 마키나락스 26,850 → 27,750
    × 4주 → 총액 3,600 − 비용 242 = 3,358 = tdy_sel_pl).
    """
    gross = (exit_px - entry) * qty
    if side == "short":
        gross = -gross
    return gross - (fee or 0) - (tax or 0)


def open_position(order: dict, fill: float | None = None,
                  ord_no: str | None = None) -> None:
    """발주 성공 주문을 오픈 포지션으로 기록. fill 미지정 시 최신가→모델가 순.
    ord_no(키움 주문번호)를 함께 저장해 두면 실시간 체결 수신 시 진입가를
    실측으로 갱신할 수 있다(정밀도 향상)."""
    symbol = order["symbol"]
    model_entry = float(order["entry"])
    entry = float(fill if fill else (latest_price(symbol) or model_entry))
    name = settings.WATCHLIST.get(symbol) or symbol
    slippage = (entry - model_entry) / model_entry * 100 if model_entry else 0.0
    with _conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO positions "
            "(id, opened, symbol, name, rule, side, qty, model_entry, entry, "
            "stop, target, closed, exit, exit_reason, pnl_pct, pnl_krw, "
            "slippage_pct, status, ord_no, exec_symbol, fill_confirmed) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order["id"], datetime.now(KST).isoformat(timespec="seconds"),
             symbol, name, order["rule"], order["side"], int(order["qty"]),
             model_entry, entry, float(order["stop"]), float(order["target"]),
             None, None, None, None, None, round(slippage, 4), "open",
             (ord_no or None), order.get("exec_symbol"), 0),
        )


def _close(conn: sqlite3.Connection, row: sqlite3.Row, exit_px: float, reason: str,
           *, ord_no: str | None = None, confirmed: bool = False,
           model_exit: float | None = None) -> None:
    net = _net_pnl_pct(row["side"], row["entry"], exit_px)
    pnl_krw = row["qty"] * row["entry"] * net / 100
    conn.execute(
        "UPDATE positions SET closed=?, exit=?, exit_reason=?, pnl_pct=?, pnl_krw=?, "
        "status='closed', exit_ord_no=?, exit_fill_confirmed=?, model_exit=? "
        "WHERE id=?",
        (datetime.now(KST).isoformat(timespec="seconds"), round(exit_px, 2), reason,
         round(net, 4), round(pnl_krw, 1), ord_no or row["exit_ord_no"],
         1 if confirmed else 0,
         round(float(model_exit if model_exit is not None else exit_px), 2), row["id"]),
    )


def fill_price_of(ord_no: str) -> float | None:
    """이미 도착한 체결 중 그 주문번호의 체결가(없으면 None).
    실시간 체결이 청산 기록보다 먼저 올 수 있어, 닫을 때 한 번 확인한다."""
    if not ord_no:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT price FROM exec_fills WHERE ord_no=? AND price>0 "
            "ORDER BY ts DESC LIMIT 1", (ord_no,)
        ).fetchone()
    return float(row["price"]) if row else None


def close_position(pos_id: str, exit_px: float, reason: str = "manual",
                   ord_no: str | None = None) -> bool:
    """포지션을 청산 기록한다.

    exit_px 는 '이론가'(손절선·목표가·종가)다. 시장가 청산의 실제 체결가가 이미
    들어와 있으면 그 값으로 대체하고, 아직이면 나중에 record_fill 이 정정한다.
    이 구분이 없으면 급락장에서 stop 가격으로 팔린 것처럼 기록돼 실현손익이
    실제보다 좋게 남는다 — 그 숫자를 가드·성과·일지가 그대로 쓴다.
    """
    filled = fill_price_of(ord_no) if ord_no else None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id=? AND status='open'", (pos_id,)
        ).fetchone()
        if not row:
            return False
        _close(conn, row, filled if filled else exit_px, reason, ord_no=ord_no,
               confirmed=filled is not None, model_exit=exit_px)
    return True


def monitor(price_of) -> int:
    """오픈 포지션의 손절/목표 터치를 확인해 청산. price_of(symbol)->float|None.
    반환: 이번에 청산된 건수.

    **프로덕션 무호출 — 테스트 전용 헬퍼다.** 실경로는 데스크(2초)와
    `main._ledger_loop`(발주 경유)이고, 이 함수는 발주 없이 원장만 닫으므로
    실거래에 쓰면 고아 포지션을 만든다. 테스트가 청산 경로를 구동할 때만 쓴다.
    판정선은 실경로와 같은 `effective_lines`(*_live 우선) — 여기만 원본
    stop/target 을 보면 재사용되는 순간 현행 규약과 갈라진다(감사 2026-08-01).
    """
    closed = 0
    with _conn() as conn:
        for row in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            p = price_of(row["symbol"])
            if p is None:
                continue
            stop, target = effective_lines(row)
            if row["side"] == "long":
                hit = "stop" if p <= stop else ("target" if p >= target else None)
            else:
                hit = "stop" if p >= stop else ("target" if p <= target else None)
            if hit:
                _close(conn, row, float(stop if hit == "stop" else target), hit)
                closed += 1
    return closed


def max_hold_min(rule: str) -> int:
    """그 규칙의 최대 보유 시간(분). 0 이면 시간 손절 없음.
    규칙별 설정이 전역 기본값(rules.max_hold_min)을 덮어쓴다."""
    cfg = settings.RULES
    r = cfg.get(rule)
    v = r.get("max_hold_min") if isinstance(r, dict) and "max_hold_min" in r \
        else cfg.get("max_hold_min", 0)
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def hold_since(row) -> str:
    """시간 손절이 세는 시작점. 손절선이 움직였으면 **그 시각부터 다시 센다.**

    사용자 지침(2026-07-29). 시간 손절의 근거는 "인트라데이 셋업은 첫 30분 안에
    결판난다 — 오래 끌수록 원래 논리는 사라진다" 였다. 그런데 손절선이 따라
    올라갔다는 것은 **방향이 아직 맞다**는 실측이다. 그 경우까지 진입 시각으로
    자르면 이기고 있는 자리를 시계 때문에 닫는다.

    반대로 손절선이 한 번도 안 움직였으면 원래 규칙 그대로다 — 진입 후 값이
    나오지 않은 자리이므로 자를 근거가 그대로 남아 있다.
    """
    try:
        moved = row["stop_moved"]
    except (KeyError, IndexError):     # 구버전 행(컬럼 없음)
        moved = None
    return str(moved or row["opened"])


def held_minutes(opened: str, now: datetime | None = None) -> float | None:
    try:
        t = datetime.fromisoformat(opened)
    except (TypeError, ValueError):
        return None
    now = now or datetime.now(KST)
    if t.tzinfo is None:
        t = t.replace(tzinfo=KST)
    return (now - t).total_seconds() / 60


def refit_lines(side: str, model_entry: float, entry: float, stop: float,
                target: float, lo: float, hi: float) -> tuple[float, float] | None:
    """체결가가 확정된 뒤 손절·목표를 **실측 진입가 기준**으로 다시 본다.

    신호 단계의 손절폭 대역 검사(`rules.evaluate_all`)는 모델 진입가로 한다.
    체결가는 그 뒤에 정해지고, 슬리피지가 폭을 대역 밖으로 밀어낼 수 있다.
    **진입 전이라면 폐기했을 폭을 진입 후에는 아무도 다시 보지 않는다.**

    실측 2026-07-27~31: 107건 중 17건이 체결 후 1.0~2.5% 밖이었고, 슬리피지가
    폭을 최대 0.94%p 밀어냈다. 대역의 양쪽 끝은 각각 "비용을 못 이긴다"와
    "목표가 하루 변동폭 밖이다" 를 뜻하므로, 밖으로 나간 폭은 그대로 두면
    진입 시점에 이미 결론이 난 거래가 된다.

    대역 **안이면 손절선을 그대로 둔다** — 대개 구조적 수준(레인지 저점·스윙
    저점)이고 옮기면 근거가 사라진다. 밖일 때만 가장 가까운 경계로 당기고,
    목표는 원래 설계한 손익비 R 을 유지하도록 다시 만든다.

    반환: 새 (손절선, 목표가). 손댈 필요가 없으면 None.
    """
    if not (lo or hi):
        return None
    if not (model_entry and entry and stop) or entry <= 0 or model_entry <= 0:
        return None
    long = side == "long"
    # 체결가가 이미 손절선을 넘어 버렸다면 옮기지 않는다. 손절선을 다시 그으면
    # '즉시 청산될 자리' 가 '한 번 더 잃을 자리' 로 바뀐다 — 실거래에 닿는
    # 판단은 보수적인 쪽으로 둔다.
    if (long and entry <= stop) or (not long and entry >= stop):
        return None
    width = round(abs(entry - stop) / entry * 100, 4)
    if (not lo or width >= lo) and (not hi or width <= hi):
        return None
    want = min(max(width, lo or width), hi or width)
    # 원 설계의 손익비. 목표가 없거나 손절폭이 0이면 R 을 복원할 수 없다 —
    # 그때는 목표를 건드리지 않는다(폭만 고친다).
    base = abs(model_entry - stop)
    r = abs(target - model_entry) / base if base > 0 and target else None
    new_stop = entry * (1 - want / 100) if long else entry * (1 + want / 100)
    new_target = target
    if r is not None:
        new_target = (entry * (1 + want * r / 100) if long
                      else entry * (1 - want * r / 100))
    return round(new_stop, 2), round(float(new_target), 2)


def _apply_refit(conn, row, entry: float) -> bool:
    """체결 확정 직후 손절·목표 재검증을 실제 행에 반영한다(같은 트랜잭션 안).

    쓰는 곳은 `stop_live`/`target_live` 다 — **원본은 보존한다.** 일지·백테스트가
    "원래 설계는 무엇이었나" 를 계속 읽어야 하고, 재검증 규칙이 나빴을 때
    되돌릴 기준이 남아야 한다(데스크 라인 갱신과 같은 규약).
    """
    if row["status"] != "open":
        return False
    cfg = settings.RULES or {}
    if not cfg.get("refit_stop_on_fill", True):
        return False
    fit = refit_lines(
        row["side"], float(row["model_entry"] or 0), entry,
        float(row["stop"] or 0), float(row["target"] or 0),
        float(cfg.get("min_stop_pct") or 0), float(cfg.get("max_stop_pct") or 0))
    if fit is None:
        return False
    stop, target = fit
    conn.execute(
        "UPDATE positions SET stop_live=?, target_live=?, lines_updated=? WHERE id=?",
        (stop, target, datetime.now(KST).isoformat(timespec="seconds"), row["id"]))
    log.info("체결 후 손절폭 재검증 %s: 진입 %s → 손절 %s→%s · 목표 %s→%s",
             row["symbol"], entry, row["stop"], stop, row["target"], target)
    return True


def effective_lines(row) -> tuple[float, float]:
    """지금 판정에 쓸 (손절선, 목표가).

    매매 데스크가 갱신한 `*_live` 가 있으면 그 값을, 없으면 진입 시 고정값을 쓴다.
    데스크가 꺼져 있거나 아직 갱신하지 않은 포지션은 자동으로 기존 동작이 된다.
    """
    def _pick(live_key: str, base_key: str) -> float:
        try:
            v = row[live_key]
        except (KeyError, IndexError):     # 구버전 행(컬럼 없음)
            v = None
        return float(v if v is not None else row[base_key])

    return _pick("stop_live", "stop"), _pick("target_live", "target")


def set_lines(pos_id: str, stop: float | None, target: float | None,
              now: datetime | None = None) -> bool:
    """손절·익절선 갱신. **값이 실제로 바뀔 때만 쓴다**(반환: 썼는가).

    데스크가 초 단위로 도는데 매번 UPDATE 하면 SQLite 쓰기가 그만큼 잦아진다.
    """
    ts = (now or datetime.now(KST)).isoformat(timespec="seconds")
    with _conn() as conn:
        row = conn.execute(
            "SELECT entry, side, stop, stop_live, target_live, stop_moved "
            "FROM positions WHERE id=? AND status='open'", (pos_id,)).fetchone()
        if row is None:
            return False
        if row["stop_live"] == stop and row["target_live"] == target:
            return False
        # 손절선이 실제로 움직였을 때만 시간 손절 시계를 다시 돌린다. 목표가만
        # 바뀐 것으로 보유시간을 늘리면 근거가 없다. **원본과 비교**하는 이유는
        # 첫 갱신에서 live 가 None→원본값 으로 채워지는 경우가 있어서다 —
        # 값은 그대로인데 시계만 초기화되면 안 된다.
        #
        # **손실 구간에서는 리셋하지 않는다**(사용자 결정 2026-07-29). 잠깐 반등해
        # 손절선이 한 칸 오른 것만으로 만기가 뒤로 밀리면, 그 사이 더 빠진 채로
        # 시간 청산된다 — 실측에서 그 전이(timeout− → timeout−) 6건이 5,245원을
        # 깎았다. 시계를 늘릴 근거는 "이익을 확정했다" 이지 "잠깐 올랐다" 가 아니다.
        prev_stop = row["stop_live"] if row["stop_live"] is not None else row["stop"]
        entry = float(row["entry"] or 0)
        in_profit = stop is not None and entry > 0 and (
            float(stop) > entry if row["side"] == "long" else float(stop) < entry)
        moved = ts if (stop is not None and float(stop) != float(prev_stop)
                       and in_profit) else row["stop_moved"]
        conn.execute(
            "UPDATE positions SET stop_live=?, target_live=?, lines_updated=?, "
            "stop_moved=? WHERE id=?", (stop, target, ts, moved, pos_id))
    return True


def due_exits(price_of, now: datetime | None = None) -> list[dict]:
    """손절/목표/시간초과에 걸린 오픈 포지션 목록(청산 대상).

    시간 손절: 인트라데이 셋업은 첫 30분 안에 결판난다. 오래 끌수록 원래 논리는
    사라지고 자금만 묶인다(실측: 60분 이상 보유 5건 전패 vs 8분 승리 1건).
    실제 청산·기록은 호출자가 한다. exit_pending 인 포지션은 제외하고,
    포지션을 변경하지 않는다."""
    out: list[dict] = []
    now = now or datetime.now(KST)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' "
            "AND (exit_pending IS NULL OR exit_pending=0)"
        ).fetchall()
    for row in rows:
        if row["stop"] is None:
            continue     # 외부(rule='external') 포지션 — 감시·기록만, 자동 청산 없음
        p = price_of(row["symbol"])
        if p is None:
            continue
        stop, target = effective_lines(row)
        if row["side"] == "long":
            reason = "stop" if p <= stop else ("target" if p >= target else None)
        else:
            reason = "stop" if p >= stop else ("target" if p <= target else None)
        px = None
        if reason:
            px = float(stop if reason == "stop" else target)
        else:
            limit = max_hold_min(row["rule"])
            held = held_minutes(hold_since(row), now) if limit else None
            if limit and held is not None and held >= limit:
                reason, px = "timeout", float(p)   # 시간 손절은 현재가로 정리
        if reason:
            out.append(dict(row) | {"reason": reason, "exit_px": px})
    return out


def set_exit_pending(pos_id: str, val: int) -> None:
    with _conn() as conn:
        conn.execute("UPDATE positions SET exit_pending=? WHERE id=?", (val, pos_id))


def force_close_eod(price_of) -> int:
    """장 마감 미청산 포지션을 현재가로 정리(reason=eod).

    **프로덕션 무호출 — 테스트 전용 헬퍼다**(`monitor` 와 같은 이유).
    실경로의 EOD 정리는 `main._ledger_loop` 가 시장가 발주를 거쳐 한다.
    """
    closed = 0
    with _conn() as conn:
        for row in conn.execute("SELECT * FROM positions WHERE status='open'").fetchall():
            p = price_of(row["symbol"]) or row["entry"]
            _close(conn, row, float(p), "eod")
            closed += 1
    return closed


WIN, LOSS, FLAT = "익절", "손절", "본전"


def outcome(row) -> str | None:
    """**실제 손익 부호로 정한 결과 구분.** 청산 사유와 별개다.

    사용자 지침(2026-07-30):
    > 실제 손익이 + 이면, 청산 사유가 손절라인 근접이어도 익절로 표기한다.

    라인 추종을 켜면 손절선이 진입가 위로 올라간다. 그 선에 닿아 나온 것은
    기계적 사유로는 `stop` 이지만 **결과는 이익**이다. 사유 이름만 보면
    "손절이 늘었다" 로 잘못 읽힌다 — 추종 투사에서 이미 같은 함정을 만나
    `stop+`/`stop-` 로 나눠 세고 있었다(research/trailing.py `_tally`).
    그 구분을 원장·일지·화면에도 같은 이름으로 올린다.

    **`exit_reason` 을 덮어쓰지 않는다.** 기계적 사유는 진단의 입력이다 —
    목표에 닿아 나온 것과 올라간 손절선에 닿아 나온 것은 같은 이익이어도
    다음에 고칠 것이 다르다(전자는 목표를, 후자는 확정률을 본다).

    기준값은 `pnl_krw` 다. 대사가 끝난 건은 `apply_actual_costs()` 가 이 칸을
    증권사 실비용 기준으로 덮어쓰므로, 그 뒤에는 이 값이 곧 실제 손익이다.
    아직 안 붙은 건은 모델값이고 부호가 뒤집힐 수 있어 화면이 `~` 로 표시한다.

    청산 전(`open`)이거나 손익이 없으면 None — 없는 결과를 만들지 않는다.
    """
    if row is None:
        return None
    krw = row["pnl_krw"] if not isinstance(row, dict) else row.get("pnl_krw")
    if krw is None:
        return None
    try:
        krw = float(krw)
    except (TypeError, ValueError):
        return None
    return WIN if krw > 0 else LOSS if krw < 0 else FLAT


def _row(r) -> dict:
    """원장 행 → 화면·일지가 쓰는 dict. 파생 필드는 여기 한 곳에서 붙인다."""
    d = dict(r)
    d["outcome"] = outcome(d)
    return d


def positions(status: str | None = None, limit: int = 100) -> list[dict]:
    """status 미지정이면 **void 를 뺀** 전체. void 를 보려면 명시해야 한다."""
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status=? ORDER BY opened DESC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM positions WHERE status!='void' "
                "ORDER BY opened DESC LIMIT ?", (limit,)).fetchall()
    return [_row(r) for r in rows]


# --------------------------------------------------------------------------
# 미체결 회수 — 체결되지 않은 주문이 손익으로 남지 않게 한다
# --------------------------------------------------------------------------
def void_position(pos_id: str, note: str = "") -> bool:
    """포지션을 무효 처리한다(status='void'). 삭제하지 않는다 — 감사 흔적을 남긴다.

    `void` 는 `status='open'`/`'closed'` 어느 조회에도 걸리지 않으므로
    실현손익·가드·일지·성과 집계에서 자동으로 빠진다.
    """
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE positions SET status='void', exit_reason=? WHERE id=? "
            "AND status!='void'",
            (f"void:{note}" if note else "void", pos_id),
        )
        return cur.rowcount > 0


def reap_unfilled(holdings: dict[str, int] | None, now: datetime | None = None,
                  grace_min: float = 5.0) -> list[dict]:
    """체결이 확인되지 않은 오픈 포지션을 **계좌 잔고와 대조해** 회수한다.

    2026-07-29 실측이 이유다. `orders.approve_and_send` 는 키움 REST 가 주문번호를
    반환하면(`status == "sent"`) 곧바로 `open_position()` 을 부른다. 거래소가 그
    뒤에 주문을 거부해도 되돌리는 경로가 없다. 그날 현물 계좌에서 개별주 공매도
    2건이 주문번호만 받고 체결되지 않았는데, 원장에는 포지션이 열리고 수동 청산으로
    **−1,025원이 실현손익에 들어갔다**. 그 값을 일일 손실 가드가 그대로 썼다.

    **판별 기준은 원장이 아니라 계좌 잔고다.** `fill_confirmed=0` 만으로는 유령을
    가릴 수 없다 — 같은 날 고려산업은 `fill_confirmed=0` 이었지만 계좌에 52주가
    실재했다(실시간 체결 이벤트만 놓친 경우). 원장만 보고 지웠으면 실제 보유
    종목의 청산 감시가 사라졌을 것이다.

    holdings: {종목코드: 보유수량}. **None 이면 아무것도 하지 않는다** —
        조회 실패로 전 포지션을 지우는 것이 유령을 남기는 것보다 훨씬 위험하다
        (`engine._held_symbols` 의 '과차단보다 통과' 와 같은 판단).
    반환: 회수한 포지션 목록.
    """
    if holdings is None:
        return []
    now = now or datetime.now(KST)
    reaped: list[dict] = []
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='open' "
            "AND (fill_confirmed IS NULL OR fill_confirmed=0)"
        ).fetchall()
        for r in rows:
            held = held_minutes(r["opened"], now)
            if held is None or held < grace_min:
                continue                      # 아직 체결이 도착할 시간이다
            # 숏은 인버스 ETF 매수로 나간다 — 원 종목으로 대조하면 실보유가
            # 전부 유령으로 읽힌다(2026-07-29 실측: LG디스플레이 숏 = 114800 매수)
            if int(holdings.get(executed_symbol(r), 0) or 0) > 0:
                continue                      # 계좌에 실재한다 — 유령이 아니다
            conn.execute(
                "UPDATE positions SET status='void', exit_reason=? WHERE id=?",
                (f"void:미체결 {held:.0f}분", r["id"]),
            )
            reaped.append(dict(r))
    for r in reaped:
        log.warning("미체결 포지션 회수: %s(%s) %s주 — 주문번호 %s, 계좌 보유 없음",
                    r.get("name"), r.get("symbol"), r.get("qty"), r.get("ord_no"))
    return reaped


def _agg(rows: list[sqlite3.Row]) -> dict:
    if not rows:
        return {"trades": 0}
    pnls = [r["pnl_pct"] for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    return {
        "trades": len(rows),
        "win_rate": round(len(wins) / len(rows) * 100, 1),
        "expectancy_pct": round(sum(pnls) / len(pnls), 3),   # 건당 기대값
        "total_pnl_krw": round(sum(r["pnl_krw"] for r in rows), 0),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 2)
        if losses and sum(losses) != 0 else (None if wins else 0.0),
        "avg_slippage_pct": round(
            sum(r["slippage_pct"] or 0 for r in rows) / len(rows), 4),
    }


def realized_today(equity: float) -> dict:
    """당일(KST) 실현손익 → {krw, pct, trades, source, model_krw}.

    일일 목표·손실 가드의 기준값이다. **주값은 증권사 계산값**(broker_daily,
    ka10074 순액 — 수동 매매 포함, 실비용 반영)이고, 동기화가 낡았거나 없으면
    시스템 원장 합계(모델·대사 혼합)로 폴백한다(source 로 구분).

    모델값으로 가드가 판정하던 것이 실측 구멍이었다 — 2026-07-29 모델이 손실을
    1,796원 과대계상한 상태로 가드가 잠겼다. 실거래 원장 대치(2026-08-01)의
    2단계가 이 전환이다.
    """
    today = datetime.now(KST).date().isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT pnl_krw FROM positions WHERE status='closed' "
            "AND substr(closed,1,10)=?", (today,)
        ).fetchall()
    model_krw = sum((r["pnl_krw"] or 0) for r in rows)
    krw, source = model_krw, "model"
    try:
        from . import fills
        br = fills.broker_realized_today()
        if br is not None:
            krw, source = br, "broker"
    except Exception:  # noqa: BLE001 - 대치 레이어 오류가 가드를 세우면 안 된다
        pass
    pct = (krw / equity * 100) if equity else 0.0
    return {"krw": round(krw, 1), "pct": round(pct, 4), "trades": len(rows),
            "source": source, "model_krw": round(model_krw, 1)}


def open_pnl(price_of, equity: float) -> dict:
    """미청산 포지션의 **평가손익** 합계 → {krw, pct, positions, priced}.

    일일 손실 가드가 실현손익만 보던 것이 이번 실측의 구멍이다 — 2026-07-30
    한도 1.5% 인 날 계좌는 −4.26% 까지 갔다. 가드는 청산된 것만 세므로, 물려
    있는 포지션이 아무리 깊어도 침묵한다. **손실은 청산할 때 생기는 게 아니라
    청산할 때 확정될 뿐이다.**

    가격을 못 얻은 포지션은 `priced` 에서 빠진다. 0 으로 세면 손실을 과소계상해
    가드가 다시 침묵하므로, 호출부가 "몇 건을 못 봤는가" 를 알 수 있게 한다.
    """
    krw = 0.0
    n = priced = 0
    for row in positions(status="open"):
        n += 1
        px = price_of(row["symbol"])
        entry, qty = float(row["entry"] or 0), int(row["qty"] or 0)
        if not px or entry <= 0 or qty <= 0:
            continue
        priced += 1
        d = (float(px) - entry) if row["side"] == "long" else (entry - float(px))
        krw += d * qty
    return {"krw": round(krw, 1), "pct": round(krw / equity * 100, 4) if equity else 0.0,
            "positions": n, "priced": priced}


def _epoch(iso: str) -> int | None:
    """ISO(KST) → 차트 x축과 같은 단위(유닉스 초)."""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    return int((t.replace(tzinfo=KST) if t.tzinfo is None else t).timestamp())


def marks_for(symbol: str, day: str) -> dict:
    """그 종목·그 날짜의 체결 지점과 보유 중 손절/목표선 — 1분봉 차트 오버레이용.

    marks  : 진입(▲)·청산(▼) 각각의 시각과 **실제 기록된 체결가**
    levels : 아직 열려 있는 포지션의 손절·목표 가격(수평 점선)
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE symbol=? AND status!='void' "
            "AND (substr(opened,1,10)=? OR substr(closed,1,10)=?) ORDER BY opened",
            (symbol, day, day),
        ).fetchall()
    marks: list[dict] = []
    levels: list[dict] = []
    for r in rows:
        if str(r["opened"] or "")[:10] == day and r["entry"]:
            t = _epoch(r["opened"])
            if t:
                marks.append({
                    "time": t, "price": round(float(r["entry"]), 2), "kind": "entry",
                    "side": r["side"], "qty": r["qty"], "rule": r["rule"],
                    "confirmed": bool(r["fill_confirmed"]),
                })
        if r["status"] == "closed" and str(r["closed"] or "")[:10] == day and r["exit"]:
            t = _epoch(r["closed"])
            if t:
                marks.append({
                    "time": t, "price": round(float(r["exit"]), 2), "kind": "exit",
                    "reason": r["exit_reason"], "qty": r["qty"], "rule": r["rule"],
                    "outcome": outcome(r),
                    "pnl_pct": r["pnl_pct"], "pnl_krw": r["pnl_krw"],
                    "confirmed": bool(r["exit_fill_confirmed"]),
                })
        if r["status"] == "open":
            for price, kind in ((r["stop"], "stop"), (r["target"], "target")):
                if price:
                    levels.append({"price": round(float(price), 2), "kind": kind,
                                   "rule": r["rule"]})
    marks.sort(key=lambda m: m["time"])
    return {"marks": marks, "levels": levels}


def open_symbols() -> set[str]:
    """지금 열려 있는 포지션의 종목코드 집합 — 같은 종목 중복 진입 차단용."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM positions WHERE status='open'").fetchall()
    return {r["symbol"] for r in rows}


def open_count() -> int:
    """현재 열린 포지션 수 — 동시 포지션 한도(max_positions) 판정 기준."""
    with _conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) c FROM positions WHERE status='open'").fetchone()["c"])


def stats() -> dict:
    """청산 완료 포지션 집계: 전체 + 규칙별. 실현손익·기대값·슬리피지."""
    with _conn() as conn:
        closed = conn.execute("SELECT * FROM positions WHERE status='closed'").fetchall()
        open_n = conn.execute(
            "SELECT COUNT(*) c FROM positions WHERE status='open'").fetchone()["c"]
    by_rule = {}
    for r in {row["rule"] for row in closed}:
        by_rule[r] = _agg([row for row in closed if row["rule"] == r])
    return {"overall": _agg(closed), "by_rule": by_rule, "open_count": int(open_n)}
