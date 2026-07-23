"""반자동 주문 매니저.

신호 → pending 큐 → 대시보드 승인(approve) → 키움 발주.
TTL 이 지난 pending 은 자동 만료. 모든 상태 전이는 audit 테이블에 남긴다.
숏 신호는 현물 계좌 제약상 인버스 ETF '매수' 로 매핑한다(동일 명목금액 근사).
"""
import json
import math
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import settings
from ..signals.rules import Signal

DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            created TEXT NOT NULL,
            expires TEXT NOT NULL,
            symbol TEXT, side TEXT, rule TEXT, reason TEXT,
            entry REAL, stop REAL, target REAL, qty INTEGER,
            exec_symbol TEXT, exec_side TEXT, exec_qty INTEGER,
            status TEXT NOT NULL,   -- pending/approved/sent/rejected/expired/error
            result TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS audit (
            ts TEXT NOT NULL, order_id TEXT, event TEXT, detail TEXT
        )"""
    )
    return conn


def _audit(conn: sqlite3.Connection, order_id: str, event: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit VALUES (?,?,?,?)",
        (datetime.now(UTC).isoformat(), order_id, event, detail),
    )


def propose(sig: Signal, qty: int) -> str:
    """신호를 승인 대기 주문으로 등록. 반환: 주문 id."""
    order_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC)
    ttl_min = settings.RISK.get("signal_ttl_min", 10)
    if sig.side == "short" and settings.INVERSE_ETF:
        exec_symbol, exec_side = settings.INVERSE_ETF, "buy"
        # 명목금액 유지 근사: 종목 수량×가격 만큼 인버스 ETF 매수
        exec_qty = max(1, math.floor(qty * sig.entry / max(sig.entry, 1)))
    else:
        exec_symbol, exec_side, exec_qty = sig.symbol, "buy" if sig.side == "long" else "sell", qty
    with _conn() as conn:
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order_id, now.isoformat(),
                (now + timedelta(minutes=ttl_min)).isoformat(),
                sig.symbol, sig.side, sig.rule, sig.reason,
                sig.entry, sig.stop, sig.target, qty,
                exec_symbol, exec_side, exec_qty,
                "pending", None,
            ),
        )
        _audit(conn, order_id, "proposed", sig.reason)
    return order_id


def expire_stale() -> int:
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='expired' WHERE status='pending' AND expires < ?",
            (now,),
        )
        if cur.rowcount:
            _audit(conn, "", "expired_batch", f"{cur.rowcount}건 만료")
        return cur.rowcount


def list_orders(status: str | None = None, limit: int = 50) -> list[dict]:
    expire_stale()
    with _conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status=? ORDER BY created DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM orders ORDER BY created DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def get(order_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def reject(order_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE orders SET status='rejected' WHERE id=? AND status='pending'",
            (order_id,),
        )
        if cur.rowcount:
            _audit(conn, order_id, "rejected")
    return bool(cur.rowcount)


async def approve_and_send(order_id: str) -> dict:
    """승인 → 키움 발주. 반드시 사용자 액션(대시보드 버튼)에서만 호출할 것."""
    from ..kiwoom.client import client  # 지연 임포트 (테스트에서 네트워크 불필요)

    order = get(order_id)
    if not order:
        return {"ok": False, "error": "주문 없음"}
    if order["status"] != "pending":
        return {"ok": False, "error": f"승인 불가 상태: {order['status']}"}
    if datetime.fromisoformat(order["expires"]) < datetime.now(UTC):
        expire_stale()
        return {"ok": False, "error": "신호 만료됨"}
    with _conn() as conn:
        conn.execute("UPDATE orders SET status='approved' WHERE id=?", (order_id,))
        _audit(conn, order_id, "approved")
    try:
        result = await client.order(
            order["exec_side"], order["exec_symbol"], order["exec_qty"], price=0
        )
        status, detail = "sent", json.dumps(result, ensure_ascii=False)[:2000]
    except Exception as e:  # noqa: BLE001 - 발주 실패는 기록하고 사용자에게 보여준다
        status, detail = "error", str(e)
        result = {"error": str(e)}
    with _conn() as conn:
        conn.execute(
            "UPDATE orders SET status=?, result=? WHERE id=?", (status, detail, order_id)
        )
        _audit(conn, order_id, status, detail)
    return {"ok": status == "sent", "status": status, "result": result}
