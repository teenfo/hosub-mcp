"""반자동 주문 매니저.

신호 → pending 큐 → 대시보드 승인(approve) → 키움 발주.
TTL 이 지난 pending 은 자동 만료. 모든 상태 전이는 audit 테이블에 남긴다.
숏 신호는 현물 계좌 제약상 인버스 ETF '매수' 로 매핑한다(동일 명목금액 근사).
"""
import json
import math
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .. import settings
from ..signals.rules import Signal

DB_PATH = Path(settings.DATA_DIR) / "trading.db"


def _margin_shortfall(result) -> int | None:
    """키움 거부 응답이 '매수증거금 부족'이면 매수가능 수량을 반환한다.
    (파싱 실패 시 0). 증거금 부족이 아니면 None — 이 경우만 대기열에 유지해
    수량을 줄여 재시도할 수 있게 한다."""
    if not isinstance(result, dict):
        return None
    msg = str(result.get("return_msg") or "")
    if "증거금" not in msg or "부족" not in msg:
        return None
    m = re.search(r"(\d+)\s*주\s*매수가능", msg)
    return int(m.group(1)) if m else 0


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
    # 청산 주문 지원용 컬럼 (기존 DB 호환 — 없는 것만 추가)
    for col, ddl in (("kind", "TEXT DEFAULT 'entry'"),  # entry / exit
                     ("link_pos", "TEXT"),               # exit 시 연결된 포지션 id
                     ("exit_px", "REAL")):               # exit 청산 기준가
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass
    return conn


_ENTRY_COLS = ("id, created, expires, symbol, side, rule, reason, entry, stop, "
               "target, qty, exec_symbol, exec_side, exec_qty, status, result, "
               "kind, link_pos, exit_px")


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
            f"INSERT INTO orders ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                order_id, now.isoformat(),
                (now + timedelta(minutes=ttl_min)).isoformat(),
                sig.symbol, sig.side, sig.rule, sig.reason,
                sig.entry, sig.stop, sig.target, qty,
                exec_symbol, exec_side, exec_qty,
                "pending", None, "entry", None, None,
            ),
        )
        _audit(conn, order_id, "proposed", sig.reason)
    return order_id


def propose_exit(pos: dict, reason: str, exit_px: float) -> str:
    """포지션 청산을 '승인 대기 주문'으로 등록(매도). 목표 도달 청산(승인제)에 사용.
    당일 만료로 두어 미승인 시 사라지되, exit_pending 을 세워 중복 제안을 막는다."""
    order_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC)
    exec_symbol = pos.get("exec_symbol") or pos["symbol"]
    with _conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, now.isoformat(), (now + timedelta(hours=8)).isoformat(),
             pos["symbol"], pos["side"], pos["rule"], "🎯 목표 도달 — 청산(매도) 승인",
             pos["entry"], pos["stop"], pos["target"], pos["qty"],
             exec_symbol, "sell", pos["qty"], "pending", None,
             "exit", pos["id"], float(exit_px)),
        )
        _audit(conn, order_id, "exit_proposed", f"{reason} @ {exit_px}")
    from . import ledger
    ledger.set_exit_pending(pos["id"], 1)
    return order_id


async def execute_exit(pos: dict, reason: str, exit_px: float) -> dict:
    """즉시 시장가 매도로 청산(승인 없이). 손절 자동/장 마감 정리에 사용.
    키움은 네이티브 스톱주문이 없어 서버가 감시 후 직접 발주한다."""
    from ..kiwoom.client import client
    from . import ledger

    exec_symbol = pos.get("exec_symbol") or pos["symbol"]
    order_id = uuid.uuid4().hex[:12]
    now = datetime.now(UTC)
    try:
        result = await client.order("sell", exec_symbol, int(pos["qty"]), price=0)
        status, detail = "sent", json.dumps(result, ensure_ascii=False)[:2000]
    except Exception as e:  # noqa: BLE001
        status, detail, result = "error", str(e), {"error": str(e)}
    with _conn() as conn:
        conn.execute(
            f"INSERT INTO orders ({_ENTRY_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (order_id, now.isoformat(), now.isoformat(),
             pos["symbol"], pos["side"], pos["rule"], f"자동청산({reason})",
             pos["entry"], pos["stop"], pos["target"], pos["qty"],
             exec_symbol, "sell", pos["qty"], status, detail,
             "exit", pos["id"], float(exit_px)),
        )
        _audit(conn, order_id, f"exit_{reason}", detail)
    if status == "sent":
        ledger.close_position(pos["id"], float(exit_px), reason)
    return {"ok": status == "sent", "status": status, "result": result}


def expire_stale() -> int:
    now = datetime.now(UTC).isoformat()
    with _conn() as conn:
        # 만료된 청산(exit) 주문은 exit_pending 을 풀어 재제안이 가능하게 한다
        stale_exits = conn.execute(
            "SELECT link_pos FROM orders WHERE status='pending' AND expires < ? "
            "AND kind='exit'", (now,),
        ).fetchall()
        cur = conn.execute(
            "UPDATE orders SET status='expired' WHERE status='pending' AND expires < ?",
            (now,),
        )
        if cur.rowcount:
            _audit(conn, "", "expired_batch", f"{cur.rowcount}건 만료")
    for r in stale_exits:
        if r["link_pos"]:
            from . import ledger
            ledger.set_exit_pending(r["link_pos"], 0)
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
        row = conn.execute("SELECT kind, link_pos FROM orders WHERE id=?", (order_id,)).fetchone()
        cur = conn.execute(
            "UPDATE orders SET status='rejected' WHERE id=? AND status='pending'",
            (order_id,),
        )
        if cur.rowcount:
            _audit(conn, order_id, "rejected")
    # 청산 승인을 거부하면 포지션은 열린 채로 두고 재제안 가능하게 exit_pending 해제
    if cur.rowcount and row and row["kind"] == "exit" and row["link_pos"]:
        from . import ledger
        ledger.set_exit_pending(row["link_pos"], 0)
    return bool(cur.rowcount)


async def approve_and_send(order_id: str, qty: int | None = None) -> dict:
    """승인 → 키움 발주. 반드시 사용자 액션(대시보드 버튼)에서만 호출할 것.
    qty 를 지정하면 발주 수량을 그 값으로 조정한다(진입 주문에 한함)."""
    from ..kiwoom.client import client  # 지연 임포트 (테스트에서 네트워크 불필요)

    order = get(order_id)
    if not order:
        return {"ok": False, "error": "주문 없음"}
    if order["status"] != "pending":
        return {"ok": False, "error": f"승인 불가 상태: {order['status']}"}
    if datetime.fromisoformat(order["expires"]) < datetime.now(UTC):
        expire_stale()
        return {"ok": False, "error": "신호 만료됨"}
    # 사용자가 발주 수량을 조정한 경우 반영 (청산 주문은 포지션 전량 매도라 고정)
    if qty is not None and order.get("kind") != "exit":
        try:
            qty = int(qty)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            return {"ok": False, "error": "발주 수량은 1주 이상이어야 합니다"}
        order["qty"] = order["exec_qty"] = qty
    with _conn() as conn:
        conn.execute(
            "UPDATE orders SET status='approved', qty=?, exec_qty=? WHERE id=?",
            (order["qty"], order["exec_qty"], order_id),
        )
        _audit(conn, order_id, "approved", f"qty={order['exec_qty']}")
    try:
        result = await client.order(
            order["exec_side"], order["exec_symbol"], order["exec_qty"], price=0
        )
        # 키움이 HTTP 200 으로 거부 응답을 주기도 한다(예: 증거금 부족 return_code 20).
        # return_code 0 만 성공으로 보고, 아니면 거부로 처리한다(유령 포지션 방지).
        rc = result.get("return_code") if isinstance(result, dict) else 0
        if rc in (0, "0", None):
            status = "sent"
        elif _margin_shortfall(result) is not None:
            # 매수증거금 부족 → 버리지 않고 대기열에 유지(수량 줄여 재시도 가능)
            status = "pending"
        else:
            status = "rejected"
        detail = json.dumps(result, ensure_ascii=False)[:2000]
    except Exception as e:  # noqa: BLE001 - 발주 실패는 기록하고 사용자에게 보여준다
        status, detail = "error", str(e)
        result = {"error": str(e)}
    with _conn() as conn:
        if status == "pending":
            # 증거금 부족 재시도용: 만료시간을 새 TTL 로 갱신해 바로 사라지지 않게 한다.
            ttl_min = settings.RISK.get("signal_ttl_min", 10)
            new_expires = (datetime.now(UTC) + timedelta(minutes=ttl_min)).isoformat()
            conn.execute(
                "UPDATE orders SET status='pending', result=?, expires=? WHERE id=?",
                (detail, new_expires, order_id),
            )
            _audit(conn, order_id, "margin_reject_retry", detail)
        else:
            conn.execute(
                "UPDATE orders SET status=?, result=? WHERE id=?", (status, detail, order_id)
            )
            _audit(conn, order_id, status, detail)
    if status == "sent":
        from . import ledger

        if order.get("kind") == "exit":
            # 청산(매도) 승인 발주 → 연결 포지션을 청산 처리
            try:
                ledger.close_position(order["link_pos"], float(order["exit_px"]), "target")
            except Exception:  # noqa: BLE001
                pass
        else:
            # 실거래 성과 로그에 오픈 포지션 기록. 키움 주문번호(ord_no)를 함께 저장해
            # 두면 실시간 체결 수신 시 진입가를 실측으로 갱신한다(체결가 근사 → 실측).
            ord_no = ""
            if isinstance(result, dict):
                ord_no = str(result.get("ord_no") or result.get("odno")
                             or result.get("order_no") or "").strip()
            try:
                ledger.open_position(order, ord_no=ord_no or None)
            except Exception:  # noqa: BLE001 - 로그 실패가 발주를 되돌리지 않는다
                pass
    # 사용자에게 보여줄 한 줄 메시지 (성공=주문번호 / 증거금부족=재시도 안내 / 실패=키움 사유)
    if isinstance(result, dict) and status == "sent":
        message = "발주 접수" + (f" · 주문번호 {result['ord_no']}" if result.get("ord_no") else "")
    elif status == "pending":
        buyable = _margin_shortfall(result)
        hint = f" · 최대 {buyable}주 매수 가능" if buyable else ""
        message = "매수증거금 부족 — 대기열 유지, 수량을 줄여 다시 승인하세요" + hint
    elif isinstance(result, dict):
        message = result.get("return_msg") or result.get("error") or "발주 거부"
    else:
        message = status
    return {"ok": status == "sent", "status": status, "result": result,
            "message": message, "retryable": status == "pending"}
