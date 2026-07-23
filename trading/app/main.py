"""FastAPI 앱: 대시보드 + 승인 API + 백그라운드 신호 엔진."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from . import settings
from .data import store
from .signals.engine import SignalEngine
from .trade import orders

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trading")

engine = SignalEngine()
signer = URLSafeSerializer(settings.SESSION_SECRET, salt="dash")
TEMPLATE = (Path(__file__).parent.parent / "templates" / "dashboard.html").read_text(
    encoding="utf-8"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    if settings.KIWOOM_APP_KEY:
        task = asyncio.create_task(engine.loop())
        log.info("신호 엔진 시작 (env=%s)", settings.KIWOOM_ENV)
    else:
        log.warning("KIWOOM_APP_KEY 미설정 — 엔진 비활성 (대시보드만 동작)")
    yield
    if task:
        task.cancel()


app = FastAPI(title="hosub-trading", lifespan=lifespan)


def _authed(request: Request) -> bool:
    # hosub-mcp 대시보드 프록시는 공유 시크릿 헤더로 인증한다
    internal = request.headers.get("x-internal-token", "")
    if settings.INTERNAL_TOKEN and hmac.compare_digest(internal, settings.INTERNAL_TOKEN):
        return True
    cookie = request.cookies.get("dash_session", "")
    try:
        return signer.loads(cookie) == "ok"
    except BadSignature:
        return False


def require_auth(request: Request) -> None:
    if not _authed(request):
        raise HTTPException(status_code=401, detail="로그인 필요")


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _authed(request):
        return HTMLResponse(
            """<form method=post action=/login style="margin:20vh auto;width:280px;
            font-family:sans-serif"><h3>hosub-trading</h3>
            <input type=password name=password placeholder="비밀번호" autofocus
            style="width:100%;padding:8px"><button style="width:100%;margin-top:8px;
            padding:8px">로그인</button></form>"""
        )
    return HTMLResponse(TEMPLATE)


@app.post("/login")
async def login(password: str = Form(...)):
    if not hmac.compare_digest(password, settings.DASH_PASSWORD):
        return RedirectResponse("/", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        "dash_session", signer.dumps("ok"), httponly=True, samesite="strict",
        max_age=12 * 3600,
    )
    return resp


@app.get("/api/status")
async def api_status(_=Depends(require_auth)):
    return {
        "env": settings.KIWOOM_ENV,
        "engine_enabled": bool(settings.KIWOOM_APP_KEY),
        "last_run": engine.last_run,
        "watchlist": settings.WATCHLIST,
        "risk": settings.RISK,
        "daily_pnl": engine.state.realized_pnl,
        "loss_limit_hit": engine.state.loss_limit_hit,
    }


@app.get("/api/orders")
async def api_orders(status: str | None = None, _=Depends(require_auth)):
    return orders.list_orders(status=status)


@app.post("/api/orders/{order_id}/approve")
async def api_approve(order_id: str, _=Depends(require_auth)):
    result = await orders.approve_and_send(order_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.post("/api/orders/{order_id}/reject")
async def api_reject(order_id: str, _=Depends(require_auth)):
    return {"ok": orders.reject(order_id)}


@app.get("/api/bars/{symbol}")
async def api_bars(symbol: str, _=Depends(require_auth)):
    df = store.load_bars(symbol, "1m", limit=500)
    if df.empty:
        return []
    return [
        {"time": int(ts.timestamp()), "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": int(r.volume)}
        for ts, r in df.iterrows()
    ]


@app.get("/api/signals")
async def api_signals(_=Depends(require_auth)):
    return engine.last_signals
