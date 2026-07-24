"""FastAPI 앱: 대시보드 + 승인 API + 백그라운드 신호 엔진."""
import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Body, Depends, FastAPI, Form, HTTPException, Request
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
from .backtest.report import BacktestReporter
from .trade import orders

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("trading")
KST = ZoneInfo("Asia/Seoul")

engine = SignalEngine()
aggregator = BarAggregator()
feed = RealtimeFeed(aggregator.on_tick)
scanner = Scanner()
discovery = Discovery()
reporter = BacktestReporter()


async def _on_fill(fill: dict) -> None:
    """주문체결 실시간 → 실거래 로그에 실측 체결 기록(진입가 근사 → 실측)."""
    from .trade import ledger

    try:
        await asyncio.to_thread(ledger.record_fill, fill)
    except Exception:  # noqa: BLE001
        log.exception("실측 체결 기록 실패: %s", fill)


feed.on_fill = _on_fill   # 주문체결 실시간 → 실측 체결 기록
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


def _price_of(symbol: str) -> float | None:
    """실시간 형성 봉(우선) → 최근 저장 분봉 종가."""
    snap = aggregator.snapshot(symbol)
    if snap:
        return float(snap["close"])
    from .trade import ledger
    return ledger.latest_price(symbol)


async def _ledger_loop() -> None:
    """오픈 포지션 청산 감시(장중 30초) + 장 마감 미청산분 정리(1회).

    키움 REST 는 스톱주문이 없어 서버가 감시 후 직접 발주한다.
    - 손절 도달: stop_mode=auto 면 즉시 시장가 매도(계좌 보호), approve 면 승인 대기
    - 목표 도달: 승인 대기(청산 매도) — 사용자가 확인 후 발주
    - 장 마감: 미청산분 시장가 정리
    execution.auto_exit=false 면 전체 비활성(장부 기록만)."""
    from .trade import ledger, orders

    eod_done = ""
    while True:
        try:
            cfg = settings.CONFIG.get("execution", {})
            now = datetime.now(KST)
            hhmm = now.strftime("%H:%M")
            if cfg.get("auto_exit", True) and now.weekday() < 5:
                stop_mode = cfg.get("stop_mode", "auto")   # auto(B) / approve(A)
                if "09:00" <= hhmm <= "15:30":
                    for ex in await asyncio.to_thread(ledger.due_exits, _price_of):
                        if ex["reason"] == "stop" and stop_mode == "auto":
                            r = await orders.execute_exit(ex, "stop", ex["exit_px"])
                            log.info("손절 자동청산 %s: %s", ex["symbol"], r.get("status"))
                        else:  # 목표 도달, 또는 손절 승인모드(A)
                            await asyncio.to_thread(orders.propose_exit, ex,
                                                    ex["reason"], ex["exit_px"])
                            log.info("청산 승인 대기 %s (%s)", ex["symbol"], ex["reason"])
                elif hhmm > "15:30" and eod_done != now.date().isoformat():
                    n = 0
                    for pos in await asyncio.to_thread(ledger.positions, "open", 200):
                        px = _price_of(pos["symbol"]) or pos["entry"]
                        r = await orders.execute_exit(pos, "eod", px)
                        n += 1 if r.get("ok") else 0
                    eod_done = now.date().isoformat()
                    if n:
                        log.info("장 마감 미청산 %d건 시장가 정리", n)
        except Exception:  # noqa: BLE001
            log.exception("청산 감시 오류")
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app: FastAPI):
    watchlist.init()               # DB 기준으로 감시목록 복원 (최초엔 config 시드)
    watchlist.notifier = _resubscribe
    # 루프는 항상 띄운다 — 키가 없으면 매 주기 스킵하고, 설정 화면에서
    # 키가 입력되는 즉시 다음 주기부터 동작한다.
    tasks = [
        asyncio.create_task(engine.loop()),
        asyncio.create_task(engine.roster_loop()),   # 감시목록 이탈 종목 수집 연속성
        asyncio.create_task(_feed_starter()),
        asyncio.create_task(scanner.loop()),
        asyncio.create_task(discovery.loop()),
        asyncio.create_task(reporter.loop()),
        asyncio.create_task(_ledger_loop()),
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
    # 매매 엔진은 서버 시계로 장중을 판단하므로 시각·NTP 동기화를 노출한다.
    synced = Path("/run/systemd/timesync/synchronized").exists()
    return {
        "env": settings.KIWOOM_ENV,
        "engine_enabled": bool(settings.KIWOOM_APP_KEY),
        "last_run": engine.last_run,
        "watchlist": settings.WATCHLIST,
        "risk": settings.RISK,
        "daily_pnl": engine.state.realized_pnl,
        "loss_limit_hit": engine.state.loss_limit_hit,
        "server_time": datetime.now(KST).isoformat(timespec="seconds"),
        "clock_synced": synced,
    }


@app.get("/api/orders")
async def api_orders(status: str | None = None, _=Depends(require_auth)):
    from .data import symbols

    rows = orders.list_orders(status=status)
    for o in rows:                       # 종목명 + 신호 진입가 대비 현재가 괴리 표시용
        sym = o.get("symbol", "")
        o["cur_price"] = _price_of(sym)
        o["name"] = settings.WATCHLIST.get(sym) or symbols.name_of(sym) or sym
    return rows


@app.post("/api/orders/{order_id}/approve")
async def api_approve(order_id: str, payload: dict | None = Body(None),
                      _=Depends(require_auth)):
    # 항상 200 으로 결과를 돌려준다 — 발주 성공/거부(키움 사유) 모두 화면에 표시하기 위해.
    # payload.qty 가 오면 발주 수량을 사용자 지정값으로 조정.
    qty = None
    if isinstance(payload, dict) and payload.get("qty") is not None:
        try:
            qty = int(payload["qty"])
        except (TypeError, ValueError):
            qty = None
    return await orders.approve_and_send(order_id, qty=qty)


@app.post("/api/orders/{order_id}/reject")
async def api_reject(order_id: str, _=Depends(require_auth)):
    return {"ok": orders.reject(order_id)}


@app.get("/api/bars/{symbol}")
async def api_bars(symbol: str, tf: str = "1m", live: int = 0, _=Depends(require_auth)):
    """봉 데이터. tf=1m(분봉, 기본) 또는 1d(일봉, 발굴 수집분).
    live=1 이면 분봉을 키움 REST 로 즉시 조회해 최신분을 채운 뒤 반환(실시간)."""
    if tf not in ("1m", "1d"):
        tf = "1m"
    if tf == "1m" and live and settings.KIWOOM_APP_KEY:
        from .data import collector

        try:
            await collector.backfill_minutes(symbol)  # REST 즉시 조회(실시간)
        except Exception:  # noqa: BLE001 - 조회 실패 시 저장분으로 폴백
            pass
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
    # 각 신호에 현재가를 실시간으로 덧붙인다(진입가 대비 괴리 표시용).
    return [{**s, "cur_price": _price_of(s.get("symbol", ""))}
            for s in engine.last_signals]


@app.get("/api/prices")
async def api_prices(_=Depends(require_auth)):
    """감시목록 종목의 현재가 맵 — 프론트가 가격 셀만 2초 주기로 부분 갱신."""
    return {"prices": {code: _price_of(code) for code in settings.WATCHLIST}}


@app.get("/api/rules")
async def api_rules(_=Depends(require_auth)):
    """등록된 매매 규칙(테크닉) 목록 + 활성 여부 — 기법 점검/관리용."""
    from .signals.rules import REGISTRY

    out = []
    for name, (fn, _needs, side) in REGISTRY.items():
        cfg = settings.RULES.get(name, {})
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        regimes = cfg.get("regimes")
        blocked = bool(regimes and cfg.get("enabled") and engine.regime not in regimes)
        out.append({"name": name, "enabled": bool(cfg.get("enabled")),
                    "side": side, "desc": doc,
                    "regime_blocked": blocked, "cur_regime": engine.regime,
                    "config": {k: v for k, v in cfg.items()
                               if not k.startswith("_")}})
    return {"rules": out, "max_stop_pct": settings.RULES.get("max_stop_pct"),
            "long_only": settings.RISK.get("long_only", False)}


@app.post("/api/rules/{name}/toggle")
async def api_rule_toggle(name: str, payload: dict | None = Body(None),
                          _=Depends(require_auth)):
    """기법 활성/비활성 토글(영속화). enabled 미지정 시 현재 값 반전."""
    from .signals.rules import REGISTRY

    if name not in REGISTRY:
        return JSONResponse({"ok": False, "error": f"알 수 없는 규칙: {name}"}, 404)
    cur = bool(settings.RULES.get(name, {}).get("enabled"))
    enabled = bool(payload["enabled"]) if isinstance(payload, dict) and "enabled" in payload else not cur
    try:
        settings.save_rule_enabled(name, enabled)
    except (OSError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    log.info("기법 토글: %s → %s", name, "ON" if enabled else "OFF")
    return {"ok": True, "name": name, "enabled": enabled}


@app.get("/api/backtest/coverage")
async def api_backtest_coverage(_=Depends(require_auth)):
    """감시목록 종목별 분봉 축적 일수(백테스트 표본 크기 확인용).
    주의: '/{symbol}' 보다 먼저 등록해야 'coverage' 가 종목코드로 매칭되지 않는다."""
    from .data import symbols

    counts = dict(store.minute_symbols(1))   # {code: 축적 일수}
    rows = [{"code": c, "name": n or symbols.name_of(c) or c,
             "days": counts.get(c, 0)} for c, n in settings.WATCHLIST.items()]
    rows.sort(key=lambda x: -x["days"])
    return {"symbols": rows}


@app.get("/api/backtest/{symbol}")
async def api_backtest(symbol: str, tf: str = "1m", _=Depends(require_auth)):
    """저장된 봉으로 규칙 백테스트(비용 반영). 딥리서치 원칙 '내 데이터로 검증'.
    tf=1m 분봉(축적분) 또는 1d 일봉. 분봉 다일치 축적 전에는 표본이 얇다."""
    from .backtest import runner

    df = store.load_bars(symbol, tf if tf in ("1m", "1d") else "1m", limit=50000)
    if df.empty:
        return {"ok": False, "symbol": symbol, "error": "저장된 봉이 없습니다"}
    days = int(df.index.normalize().nunique())
    result = runner.run(symbol, df)
    trades = [
        {"rule": t.rule, "side": t.side, "entry": round(t.entry, 2),
         "exit": None if t.exit is None else round(t.exit, 2),
         "reason": t.exit_reason, "entry_ts": str(t.entry_ts),
         "pnl_pct": round(t.pnl_pct(settings.COSTS), 3)}
        for t in result.trades[-100:]
    ]
    from .data import symbols

    return {"ok": True, "symbol": symbol, "name": symbols.name_of(symbol),
            "tf": tf, "days": days, "stats": result.stats(), "trades": trades}


@app.get("/api/backtest/report/latest")
async def api_backtest_report(_=Depends(require_auth)):
    """분봉 축적분 주기 백테스트 최신 리포트(전체 요약 + 종목별). 종목명 포함."""
    from .data import symbols

    data = reporter.latest()
    for row in data.get("symbols", []):
        row["name"] = symbols.name_of(row.get("symbol", "")) or ""
    return data


@app.get("/api/backtest/report/history")
async def api_backtest_history(_=Depends(require_auth)):
    """실행별 전체 요약 추이(승률·손익비 변화 관찰용)."""
    return {"runs": reporter.history()}


@app.post("/api/backtest/report/run")
async def api_backtest_report_run(_=Depends(require_auth)):
    """백테스트 리포트 수동 실행(조회성 — 주문 없음)."""
    if reporter.running:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    return await asyncio.to_thread(reporter.run_once)


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
        return {"ok": False, "error": "API 키 미설정",
                "account_no": settings.KIWOOM_ACCOUNT}
    try:
        data = parse_balance(await client.balance())
    except Exception as e:  # noqa: BLE001 - 조회 실패는 화면에 표시
        data = {"ok": False, "error": str(e)}
    data["account_no"] = settings.KIWOOM_ACCOUNT   # 계좌번호 노출(마스킹 안 함)
    if data.get("ok"):
        # 포지션 사이징 기준 자산을 실제 예탁자산으로 동기화
        equity = data.get("deposit_est") or data.get("total_eval") or 0
        if equity > 0:
            engine.equity = engine.state.equity = float(equity)
        _account_cache.update(ts=now, data=data)
    return data


@app.get("/api/risk")
async def api_risk(_=Depends(require_auth)):
    """일일 목표·손실 가드 상태 + 오늘 실현손익 + 설정값."""
    return engine.day_guard_status() | {
        "risk_per_trade_pct": settings.RISK.get("risk_per_trade_pct", 0),
        "max_positions": settings.RISK.get("max_positions", 3),
    }


@app.post("/api/risk")
async def api_risk_save(payload: dict, _=Depends(require_auth)):
    """일일 목표·손실한도·거래당 리스크 설정(영속). 목표값을 UI 에서 조정."""
    try:
        settings.save_risk(
            daily_target_pct=payload.get("daily_target_pct"),
            daily_loss_limit_pct=payload.get("daily_loss_limit_pct"),
            risk_per_trade_pct=payload.get("risk_per_trade_pct"),
        )
    except (OSError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    log.info("리스크 설정 갱신: %s", {k: settings.RISK.get(k) for k in
             ("daily_target_pct", "daily_loss_limit_pct", "risk_per_trade_pct")})
    return {"ok": True, **engine.day_guard_status()}


@app.get("/api/performance")
async def api_performance(_=Depends(require_auth)):
    """실거래 성과: 청산 완료 집계(전체·규칙별) + 오픈/최근 청산 포지션."""
    from .trade import ledger

    return {"stats": ledger.stats(),
            "open": ledger.positions(status="open", limit=50),
            "closed": ledger.positions(status="closed", limit=50),
            "fills": ledger.fills(limit=30)}


@app.post("/api/positions/{pos_id}/close")
async def api_position_close(pos_id: str, _=Depends(require_auth)):
    """추적 중인 포지션을 현재가로 청산 처리(장부상 — 실제 청산 주문은 별도)."""
    from .trade import ledger

    pos = next((p for p in ledger.positions(status="open", limit=200)
                if p["id"] == pos_id), None)
    if not pos:
        return JSONResponse({"ok": False, "error": "오픈 포지션 없음"}, 404)
    px = _price_of(pos["symbol"]) or pos["entry"]
    return {"ok": ledger.close_position(pos_id, float(px), "manual")}


@app.get("/api/scanner")
async def api_scanner(_=Depends(require_auth)):
    return {"last_scan": scanner.last_scan, "results": scanner.results,
            "presurge": scanner.presurge, "gainers": scanner.gainers,
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
    entries = watchlist.entries()
    for e in entries:                       # 실시간 현재가 덧붙임
        e["cur_price"] = _price_of(e.get("code", ""))
    return {"entries": entries}


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


@app.post("/api/watchlist/mode")
async def api_watchlist_mode(payload: dict, _=Depends(require_auth)):
    """종목 매매/수집전용 전환. collect_only=true 면 데이터만 수집(신호·주문 제외)."""
    code = str(payload.get("code", "")).strip()
    collect_only = bool(payload.get("collect_only"))
    ok = watchlist.set_mode(code, collect_only)
    log.info("감시목록 모드 변경: %s → %s", code,
             "수집전용" if collect_only else "매매")
    return {"ok": ok, "code": code, "collect_only": collect_only,
            "collect_only_set": sorted(settings.COLLECT_ONLY)}


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
