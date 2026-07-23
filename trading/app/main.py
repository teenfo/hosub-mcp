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
from .data import store, watchlist
from .discovery import Discovery
from .data.collector import BarAggregator
from .kiwoom.auth import token_manager
from .kiwoom.ws import RealtimeFeed
from .signals.engine import SignalEngine
from .signals.scanner import Scanner
from .trade import orders

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trading")

engine = SignalEngine()
aggregator = BarAggregator()
feed = RealtimeFeed(aggregator.on_tick)
scanner = Scanner()
discovery = Discovery()
signer = URLSafeSerializer(settings.SESSION_SECRET, salt="dash")


async def _feed_starter() -> None:
    """API 키가 준비되는 즉시(설정 화면 입력 포함) 실시간 시세 구독 시작."""
    while not settings.KIWOOM_APP_KEY:
        await asyncio.sleep(10)
    feed.start(list(settings.WATCHLIST.keys()))
    log.info("실시간 시세 구독 시작: %s", list(settings.WATCHLIST.keys()))
TEMPLATE = (Path(__file__).parent.parent / "templates" / "dashboard.html").read_text(
    encoding="utf-8"
)


async def _resubscribe() -> None:
    await feed.update(list(settings.WATCHLIST.keys()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    watchlist.init()               # DB 기준으로 감시목록 복원 (최초엔 config 시드)
    watchlist.notifier = _resubscribe
    # 루프는 항상 띄운다 — 키가 없으면 매 주기 스킵하고, 설정 화면에서
    # 키가 입력되는 즉시 다음 주기부터 동작한다.
    tasks = [
        asyncio.create_task(engine.loop()),
        asyncio.create_task(_feed_starter()),
        asyncio.create_task(scanner.loop()),
        asyncio.create_task(discovery.loop()),
    ]
    log.info("신호 엔진 루프 시작 (env=%s, 키 %s)", settings.KIWOOM_ENV,
             "설정됨" if settings.KIWOOM_APP_KEY else "미설정")
    yield
    for t in tasks:
        t.cancel()


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
async def api_bars(symbol: str, tf: str = "1m", _=Depends(require_auth)):
    """봉 데이터. tf=1m(분봉, 기본) 또는 1d(일봉, 발굴 수집분)."""
    if tf not in ("1m", "1d"):
        tf = "1m"
    df = store.load_bars(symbol, tf, limit=500)
    bars = [] if df.empty else [
        {"time": int(ts.timestamp()), "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": int(r.volume)}
        for ts, r in df.iterrows()
    ]
    if tf == "1m":
        # 형성 중인 현재 분봉을 덧붙인다 (실시간 WS 수신분)
        cur = aggregator.snapshot(symbol)
        if cur:
            if bars and bars[-1]["time"] == cur["time"]:
                bars[-1] = cur
            elif not bars or bars[-1]["time"] < cur["time"]:
                bars.append(cur)
    return bars


@app.get("/api/signals")
async def api_signals(_=Depends(require_auth)):
    return engine.last_signals


_account_cache: dict = {"ts": 0.0, "data": None}


@app.get("/api/account")
async def api_account(_=Depends(require_auth)):
    """계좌 평가잔고 요약. 레이트리밋 보호를 위해 30초 캐시."""
    import time

    from .kiwoom.account import parse_balance
    from .kiwoom.client import client

    now = time.monotonic()
    if _account_cache["data"] and now - _account_cache["ts"] < 30:
        return _account_cache["data"]
    if not settings.KIWOOM_APP_KEY:
        return {"ok": False, "error": "API 키 미설정"}
    try:
        data = parse_balance(await client.balance())
    except Exception as e:  # noqa: BLE001 - 조회 실패는 화면에 표시
        data = {"ok": False, "error": str(e)}
    if data.get("ok"):
        # 포지션 사이징 기준 자산을 실제 예탁자산으로 동기화
        equity = data.get("deposit_est") or data.get("total_eval") or 0
        if equity > 0:
            engine.equity = engine.state.equity = float(equity)
        _account_cache.update(ts=now, data=data)
    return data


@app.get("/api/scanner")
async def api_scanner(_=Depends(require_auth)):
    return {"last_scan": scanner.last_scan, "results": scanner.results,
            "presurge": scanner.presurge,
            "config": settings.CONFIG.get("scanner", {})}


@app.get("/api/discovery")
async def api_discovery(_=Depends(require_auth)):
    from . import export

    return discovery.latest() | {"dataset": export.latest_manifest()}


@app.post("/api/discovery/run")
async def api_discovery_run(_=Depends(require_auth)):
    """야간 배치 수동 실행 (조회성 — 주문 없음). 전종목 수집이라 수 분 소요."""
    if discovery.running:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    if not settings.KIWOOM_APP_KEY:
        return JSONResponse({"ok": False, "error": "API 키 미설정"}, 400)
    asyncio.create_task(discovery.run_once())
    return {"ok": True, "message": "백그라운드 실행 시작 — 진행 상황은 /api/discovery"}


@app.get("/api/watchlist")
async def api_watchlist(_=Depends(require_auth)):
    return {"entries": watchlist.entries()}


@app.post("/api/watchlist")
async def api_watchlist_add(payload: dict, _=Depends(require_auth)):
    """종목을 감시목록에 편입. 코드(6자리) 또는 종목명으로 추가 가능.
    종목명이 여러 종목과 매칭되면 candidates 를 돌려주고 추가하지 않는다."""
    from .data import symbols

    code = str(payload.get("code", "")).strip()
    query = str(payload.get("query", "")).strip() or str(payload.get("name", "")).strip()

    async def _add(c: str, n: str):
        watchlist.add(c, n or symbols.name_of(c) or c, source="manual")
        await watchlist.notify()
        log.info("감시목록 편입: %s(%s) — 총 %d 종목", n, c, len(settings.WATCHLIST))
        return {"ok": True, "added": {"code": c, "name": n}, "watchlist": settings.WATCHLIST}

    # 6자리 코드 직접 추가
    if code.isdigit() and len(code) == 6:
        return await _add(code, str(payload.get("name", "")).strip())
    if query.isdigit() and len(query) == 6:
        return await _add(query, symbols.name_of(query) or query)
    if not query:
        return JSONResponse({"ok": False, "error": "종목명 또는 코드를 입력하세요"}, 400)

    # 종목명 → 코드 해석 (마스터 비어 있으면 지연 갱신)
    cands = symbols.resolve(query)
    if not cands and symbols.count() == 0:
        await symbols.refresh()
        cands = symbols.resolve(query)
    if not cands:
        return JSONResponse(
            {"ok": False, "error": f"'{query}' 종목을 찾을 수 없습니다. "
             "코드(6자리)로 직접 추가해 보세요."}, 404
        )
    if len(cands) == 1:
        return await _add(cands[0]["code"], cands[0]["name"])
    return {"ok": False, "candidates": cands[:20]}  # 여러 개 → 사용자 선택


@app.post("/api/symbols/refresh")
async def api_symbols_refresh(_=Depends(require_auth)):
    from .data import symbols

    if not settings.KIWOOM_APP_KEY:
        return JSONResponse({"ok": False, "error": "API 키 미설정"}, 400)
    n = await symbols.refresh()
    return {"ok": n > 0, "count": symbols.count()}


@app.post("/api/watchlist/remove")
async def api_watchlist_remove(payload: dict, _=Depends(require_auth)):
    code = str(payload.get("code", "")).strip()
    ok = watchlist.remove(code)
    await watchlist.notify()
    return {"ok": ok, "watchlist": settings.WATCHLIST}


@app.get("/api/settings")
async def api_settings(_=Depends(require_auth)):
    return settings.masked()


@app.post("/api/settings")
async def api_settings_save(payload: dict, _=Depends(require_auth)):
    env = (payload.get("env") or "").strip().lower() or None
    if env and env not in ("mock", "real"):
        return JSONResponse({"ok": False, "error": "env 는 mock/real 만 가능"}, 400)
    try:
        settings.save_keys(
            env=env,
            app_key=(payload.get("app_key") or "").strip() or None,
            secret_key=(payload.get("secret_key") or "").strip() or None,
            account=(payload.get("account") or "").strip() or None,
        )
    except (OSError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    token_manager.reset()  # 키/환경이 바뀌었으니 토큰 재발급
    log.info("API 설정 저장 (env=%s)", settings.KIWOOM_ENV)
    return {"ok": True, **settings.masked()}
