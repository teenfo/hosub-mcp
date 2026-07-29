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
from .backtest import sweep as rule_sweep
from .backtest.report import BacktestReporter
from .scout.engine import engine as scout
from .trade import desk, orders
from . import journal

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


def _api_usage() -> dict:
    """키움 REST 호출 계측 스냅샷 (클라이언트 미초기화·구버전 대비 방어)."""
    from .kiwoom.client import client

    try:
        return client.usage_snapshot()
    except AttributeError:      # 계측 미탑재 빌드
        return {}


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
    from .trade import breakeven, ledger, orders

    eod_done = ""
    while True:
        try:
            cfg = settings.CONFIG.get("execution", {})
            now = datetime.now(KST)
            hhmm = now.strftime("%H:%M")
            if cfg.get("auto_exit", True) and now.weekday() < 5:
                stop_mode = cfg.get("stop_mode", "auto")   # auto(B) / approve(A)
                if "09:00" <= hhmm <= "15:30":
                    auto_all = settings.RISK.get("auto_approve", False)
                    # 체결되지 않은 주문이 손익으로 남지 않게 계좌와 대조해 회수한다.
                    # 청산 판정보다 **먼저** 돌려야 유령 포지션에 청산 주문이 나가지 않는다.
                    await _reap_unfilled(ledger)
                    # 본전 이동 shadow — 실제 청산 판정과 **같은 가격·같은 주기**로
                    # 관측한다. 기록 전용이라 아래 청산 로직에 관여하지 않는다.
                    await _breakeven_observe(breakeven)
                    # 데스크가 켜져 있으면 **손절·목표는 데스크가 소유한다.**
                    # 여기서 또 보면 30초 뒤에 같은 판정이 한 번 더 나온다 —
                    # 이중 발주는 exit_pending 이 막지만, 어느 루프가 냈는지가
                    # 흐려지고 데스크를 껐을 때와 켰을 때의 동작이 뒤섞인다.
                    # 시간 손절은 남긴다: 시각 기준이라 2초 해상도가 필요 없다.
                    #
                    # 데스크가 **꺼져 있으면** 손절·목표를 자동 발주하지 않고 승인
                    # 대기로 올린다(사용자 결정 2026-07-29). 30초 판정으로 시장가를
                    # 던지면 판정 시점 가격과 벌어지는 것을 감수하는 셈인데, 그
                    # 트레이드오프를 사람이 보고 정하겠다는 뜻이다. 도달 사실은
                    # 화면에 남으므로 놓치지는 않는다.
                    for ex in await asyncio.to_thread(ledger.due_exits, _price_of):
                        route = desk.exit_route(ex["reason"])
                        if route == "skip":
                            continue
                        if route == "approve":
                            await asyncio.to_thread(orders.propose_exit, ex,
                                                    ex["reason"], ex["exit_px"])
                            log.info("청산 승인 대기 %s (%s) — 데스크 꺼짐",
                                     ex["symbol"], ex["reason"])
                            continue
                        # 완전 자동 모드면 목표 도달 청산도 승인 없이 즉시 실행
                        # 시간 손절은 손절과 같은 성격(계좌 보호) — 같은 모드를 따른다
                        if (ex["reason"] in ("stop", "timeout") and stop_mode == "auto") \
                                or auto_all:
                            r = await orders.execute_exit(ex, ex["reason"], ex["exit_px"])
                            log.info("%s 자동청산 %s: %s", ex["reason"],
                                     ex["symbol"], r.get("status"))
                        else:  # 승인 모드: 목표 도달, 또는 손절 승인모드(A)
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
        # 정산은 장 마감 뒤에도 돌려야 eod 청산분이 채워진다. 관측 실패가 청산
        # 감시를 멈추지 않도록 위 블록과 분리한다.
        await _breakeven_settle()
        await asyncio.sleep(30)


async def _desk_rest_price(symbol: str) -> float | None:
    """WS 값이 낡은 종목만 REST 로 보충한다(데스크 폴백).

    보유 종목 한정이라 최악에도 사이클당 몇 콜이다. 실패하면 None —
    데스크는 가격을 모르면 판정하지 않는다.
    """
    from .kiwoom.client import client
    from .kiwoom.quote import parse_quote

    try:
        q = parse_quote(await client.quote(symbol))
    except Exception:  # noqa: BLE001
        log.warning("데스크 REST 시세 실패 %s", symbol)
        return None
    return float(q["price"]) if q else None


async def _desk_loop() -> None:
    await desk.loop(
        fresh_price=lambda s: aggregator.fresh_price(s, desk.stale_sec()),
        rest_price=_desk_rest_price,
        execute_exit=orders.execute_exit,
        ws_ok=lambda: feed.connected,
        propose_exit=orders.propose_exit,
    )


async def _holdings_map() -> dict[str, int] | None:
    """{종목코드: 보유수량}. 조회 실패·미설정이면 **None** — 호출자는 아무것도 하지 않는다.

    `/api/account` 의 30초 캐시를 그대로 쓴다(신규 호출을 늘리지 않는다).
    """
    import time

    from .kiwoom.account import parse_balance
    from .kiwoom.client import client

    if not settings.KIWOOM_APP_KEY:
        return None
    now = time.monotonic()
    data = _account_cache["data"] if (
        _account_cache["data"] and now - _account_cache["ts"] < 30) else None
    if data is None:
        try:
            data = parse_balance(await client.balance())
        except Exception:  # noqa: BLE001 - 조회 실패는 '모름'이지 '없음'이 아니다
            log.warning("계좌 조회 실패 — 미체결 회수 이번 주기 생략")
            return None
        if data.get("ok"):
            _account_cache.update(ts=now, data=data)
    if not data.get("ok"):
        return None
    out: dict[str, int] = {}
    for h in data.get("holdings") or []:
        try:
            out[str(h.get("code"))] = int(h.get("qty") or 0)
        except (TypeError, ValueError):
            continue
    return out


async def _reap_unfilled(ledger) -> None:
    """미체결 포지션 회수. 실패해도 청산 감시를 막지 않는다."""
    try:
        holdings = await _holdings_map()
        if holdings is None:
            return
        await asyncio.to_thread(ledger.reap_unfilled, holdings)
    except Exception:  # noqa: BLE001
        log.exception("미체결 포지션 회수 오류")


async def _breakeven_observe(breakeven) -> None:
    """shadow 관측. **실패해도 청산 감시를 막지 않는다** — 측정이 실거래
    경로를 위태롭게 하면 안 된다."""
    try:
        if breakeven.enabled():
            await asyncio.to_thread(breakeven.observe, _price_of)
    except Exception:  # noqa: BLE001
        log.exception("본전 이동 shadow 관측 오류")


async def _breakeven_settle() -> None:
    try:
        from .trade import breakeven

        if breakeven.enabled():
            await asyncio.to_thread(breakeven.settle)
    except Exception:  # noqa: BLE001
        log.exception("본전 이동 shadow 정산 오류")


@asynccontextmanager
async def lifespan(app: FastAPI):
    watchlist.init()               # DB 기준으로 감시목록 복원 (최초엔 config 시드)
    watchlist.notifier = _resubscribe
    # 루프는 항상 띄운다 — 키가 없으면 매 주기 스킵하고, 설정 화면에서
    # 키가 입력되는 즉시 다음 주기부터 동작한다.
    tasks = [
        asyncio.create_task(engine.loop()),
        asyncio.create_task(engine.roster_loop()),   # 감시목록 이탈 종목 수집 연속성
        asyncio.create_task(engine.eod_backfill_loop()),  # 마감 후 그날 분봉 확정
        asyncio.create_task(_feed_starter()),
        asyncio.create_task(scanner.loop()),
        asyncio.create_task(discovery.loop()),
        # 발굴 엔진 — 기본 shadow. 신호를 모으고 결정을 기록만 하며 감시목록에는
        # 손대지 않는다. 기존 scanner/discovery 자동편입 경로는 그대로 돈다.
        asyncio.create_task(scout.loop()),
        asyncio.create_task(reporter.loop()),
        asyncio.create_task(rule_sweep.loop()),   # 주간 기법 스윕(토 09시)
        asyncio.create_task(journal.loop()),      # 매매일지(평일 마감 후)
        asyncio.create_task(_ledger_loop()),
        # 매매 데스크 — 보유 포지션만 초 단위로 본다. execution.desk.enabled 가
        # false 면 아무것도 하지 않고 위 _ledger_loop(30초)가 그대로 맡는다.
        asyncio.create_task(_desk_loop()),
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
        # 매매 데스크에서 "지금 감시 중인가"를 한눈에 보기 위한 상태
        "market": engine.market_status(),
        # 키움 REST 호출 부하 — 주기를 줄일 여유가 있는지 판단 근거
        "api_usage": _api_usage(),
        # 매매 데스크 계측. WS 대비 REST 비율이 이 설계의 성적표다
        "desk": desk.status(),
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
    # 파싱 불가한 qty 는 '원 수량으로 발주'가 아니라 400 으로 거부한다(안전).
    qty = None
    if isinstance(payload, dict) and payload.get("qty") is not None:
        try:
            qty = int(payload["qty"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": f"잘못된 수량: {payload['qty']!r}"}, 400)
    return await orders.approve_and_send(order_id, qty=qty)


@app.post("/api/orders/{order_id}/reject")
async def api_reject(order_id: str, _=Depends(require_auth)):
    return {"ok": orders.reject(order_id)}


@app.get("/api/bars/{symbol}")
async def api_bars(symbol: str, tf: str = "1m", live: int = 0,
                   date: str | None = None, _=Depends(require_auth)):
    """봉 데이터. tf=1m(분봉, 기본) 또는 1d(일봉, 발굴 수집분).

    분봉은 날짜 단위로 끊어서 본다(장중 차트 가독성):
    - date 미지정 → 저장분 중 가장 최근 거래일(보통 오늘)
    - date=YYYY-MM-DD → 그 날짜의 분봉만
    live=1 이면 키움 REST 로 최신분을 채운 뒤 반환(실시간, 최근일 조회에만 의미).
    """
    if tf not in ("1m", "1d"):
        tf = "1m"
    if tf == "1m" and live and settings.KIWOOM_APP_KEY and not date:
        from .data import collector

        try:
            await collector.backfill_minutes(symbol)  # REST 즉시 조회(실시간)
        except Exception:  # noqa: BLE001 - 조회 실패 시 저장분으로 폴백
            pass
    df = store.load_bars(symbol, tf, limit=5000 if tf == "1m" else 500)
    if tf == "1m" and not df.empty:
        # 날짜 문자열로 비교한다 — 인덱스 tz(aware/naive) 차이에 영향받지 않고
        # /dates 응답(str(d.date()))과 정확히 같은 키를 쓴다.
        days = [str(d.date()) for d in df.index]
        want = date if (date and date in days) else max(days)
        df = df[[d == want for d in days]]
    bars = [] if df.empty else [
        {"time": int(ts.timestamp()), "open": r.open, "high": r.high,
         "low": r.low, "close": r.close, "volume": int(r.volume)}
        for ts, r in df.iterrows()
    ]
    if tf == "1m" and not date:
        # 형성 중인 현재 분봉을 덧붙인다 (실시간 WS 수신분)
        cur = aggregator.snapshot(symbol)
        if cur:
            if bars and bars[-1]["time"] == cur["time"]:
                bars[-1] = cur
            elif not bars or bars[-1]["time"] < cur["time"]:
                bars.append(cur)
    return bars


@app.get("/api/bars/{symbol}/dates")
async def api_bar_dates(symbol: str, _=Depends(require_auth)):
    """분봉 데이터가 있는 날짜 목록 (데이트 피커 표시용, 최신순)."""
    df = store.load_bars(symbol, "1m", limit=50000)
    if df.empty:
        return {"dates": []}
    days = sorted({str(d.date()) for d in df.index}, reverse=True)
    return {"dates": days}


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
    from .backtest import sweep
    from .signals.rules import REGISTRY

    sw = await asyncio.to_thread(sweep.latest)      # 파일 1개 읽기
    sw_rules = (sw or {}).get("rules") or {}
    out = []
    for name, (fn, _needs, side) in REGISTRY.items():
        cfg = settings.RULES.get(name, {})
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
        regimes = cfg.get("regimes")
        enabled = bool(cfg.get("enabled"))
        blocked = bool(regimes and enabled and engine.regime not in regimes)
        # config 기본값과 현재값이 어긋나면 화면에서 드러내야 한다. rules.json 은
        # UI 토글이 쓰는 파일이라 '사용자의 더 최근 결정'이지만, 그 사실이 보이지
        # 않으면 어느 쪽이 의도인지 아무도 모른다.
        default = settings.RULES_CONFIG_ENABLED.get(name)
        out.append({"name": name, "enabled": enabled,
                    "side": side, "desc": doc,
                    "config_enabled": default,
                    "overridden": default is not None and default != enabled,
                    "sweep": sw_rules.get(name),
                    "priority": cfg.get("priority"),
                    "regime_blocked": blocked, "cur_regime": engine.regime,
                    "config": {k: v for k, v in cfg.items()
                               if not k.startswith("_")}})
    return {"rules": out, "max_stop_pct": settings.RULES.get("max_stop_pct"),
            "long_only": settings.RISK.get("long_only", False),
            "sweep_run_ts": (sw or {}).get("run_ts"),
            "sweep_symbols": (sw or {}).get("symbols")}


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
    deep = store.deep_backfilled()           # 연속조회로 과거를 끌어온 종목
    rows = [{"code": c, "name": n or symbols.name_of(c) or c,
             "days": counts.get(c, 0), "deep": c in deep}
            for c, n in settings.WATCHLIST.items()]
    rows.sort(key=lambda x: -x["days"])
    return {"symbols": rows,
            "deep_pending": sum(1 for r in rows if not r["deep"])}


@app.get("/api/backtest/{symbol}")
async def api_backtest(symbol: str, tf: str = "1m", _=Depends(require_auth)):
    """저장된 봉으로 규칙 백테스트(비용 반영). 딥리서치 원칙 '내 데이터로 검증'.
    tf=1m 분봉(축적분) 또는 1d 일봉. 분봉 다일치 축적 전에는 표본이 얇다."""
    from .backtest import runner

    # pandas 재생은 수 초 걸린다 — 이벤트 루프(신호·주문·시세)를 막지 않도록
    # 봉 로드부터 재생까지 통째로 워커 스레드에서 실행한다.
    df = await asyncio.to_thread(
        store.load_bars, symbol, tf if tf in ("1m", "1d") else "1m", 50000)
    if df.empty:
        return {"ok": False, "symbol": symbol, "error": "저장된 봉이 없습니다"}
    days = int(df.index.normalize().nunique())
    # 롱 전용 실전과 정합: 집행 불가한 숏은 백테스트에서도 미체결 처리
    sides = ("long",) if settings.RISK.get("long_only") else None
    result = await asyncio.to_thread(runner.run, symbol, df, sides=sides)
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


@app.get("/api/trades/{symbol}")
async def api_trades(symbol: str, date: str | None = None,
                     _=Depends(require_auth)):
    """1분봉 차트에 겹쳐 그릴 체결 지점 — 실제 매매한 가격을 차트에서 확인한다."""
    from .trade import ledger

    day = date or datetime.now(KST).date().isoformat()
    return await asyncio.to_thread(ledger.marks_for, symbol, day)


@app.post("/api/guard/override")
async def api_guard_override(payload: dict, _=Depends(require_auth)):
    """일일 손실 가드 임시 해제 — 자동 매매를 유지한 채 한도만 늘린다.

    mode=reset  : 지금 손익을 기준선으로 한도를 다시 센다(추가치를 서버가 계산)
    mode=extend : extra_pct 만큼만 한도에 더한다
    minutes     : 유효 시간(미지정 시 장 마감까지)
    당일 실현손익은 그대로 유지된다 — 바뀌는 것은 정지선뿐이다.
    """
    from .trade import override

    mode = str(payload.get("mode") or "reset")
    guard = await asyncio.to_thread(engine.day_guard_status)
    if mode == "reset":
        extra = override.reset_extra(guard["pct"], guard["daily_loss_limit_pct"])
        if extra <= 0:
            return JSONResponse(
                {"ok": False, "error": "지금은 손실 구간이 아니라 기준 리셋이 필요 없습니다"},
                400)
    else:
        extra = payload.get("extra_pct")
        if extra is None:
            return JSONResponse({"ok": False, "error": "extra_pct 가 필요합니다"}, 400)
    try:
        st = await asyncio.to_thread(
            lambda: override.grant(mode=mode, extra_pct=float(extra),
                                   pct_at=guard["pct"],
                                   minutes=payload.get("minutes")))
    except (OSError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    return {"ok": True, "override": st, **engine.day_guard_status()}


@app.post("/api/guard/override/clear")
async def api_guard_override_clear(_=Depends(require_auth)):
    """임시 해제 즉시 취소 — 설정 한도로 복귀(이력은 남는다)."""
    from .trade import override

    st = await asyncio.to_thread(override.clear)
    return {"ok": True, "override": st, **engine.day_guard_status()}


@app.get("/api/journal")
async def api_journal(date: str | None = None, _=Depends(require_auth)):
    """하루치 매매일지. 저장본이 있으면 그대로, 없으면 즉석 계산(요약은 제외).

    저장본을 우선하는 이유: LLM 요약은 생성 시점에 한 번만 만들고, 조회할 때마다
    게이트웨이를 부르지 않는다.
    """
    day = date or datetime.now(KST).date().isoformat()
    saved = await asyncio.to_thread(journal.load, day)
    if saved and saved.get("final"):
        return saved                # 마감 후 확정본 — 요약 포함, 그대로 반환
    # 진행 중인 날짜는 저장본이 있어도 그때그때 다시 계산한다(저장본은 스냅샷일 뿐).
    entry = await asyncio.to_thread(journal.build, day)
    ok, why = journal.summary_ready(day)
    entry["final"] = False
    entry["saved"] = bool(saved)
    entry["summary"] = ({"ok": False, "pending": True, "reason": why} if not ok
                        else {"ok": False,
                              "reason": "아직 작성되지 않음 — '다시 작성'으로 만들 수 있습니다"})
    return entry


@app.get("/api/journal/history")
async def api_journal_history(days: int = 30, _=Depends(require_auth)):
    """일자별 요약 + 기간 누적(매매 경과 추이)."""
    rows = await asyncio.to_thread(journal.history, max(1, min(365, days)))
    return {"rows": rows, "cumulative": journal.cumulative(rows)}


@app.post("/api/journal/run")
async def api_journal_run(payload: dict | None = Body(None),
                          _=Depends(require_auth)):
    """일지 수동 생성·재작성(조회성 — 주문 없음). summary=false 면 LLM 생략."""
    payload = payload or {}
    day = payload.get("date") or datetime.now(KST).date().isoformat()
    entry = await journal.generate(day, with_summary=payload.get("summary", True))
    return entry


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
    return await reporter.run_offloaded()   # 별도 프로세스 — 루프를 막지 않는다


@app.get("/api/research/news-impact")
async def api_news_impact(_=Depends(require_auth)):
    """뉴스 영향 소급 측정 최신 결과(조회성 — 주문 없음)."""
    from .research import newsimpact

    return await asyncio.to_thread(newsimpact.latest)


_news_running = {"on": False}


@app.post("/api/research/news-impact/run")
async def api_news_impact_run(_=Depends(require_auth)):
    """TNM 분석 전량 × 일봉 조인 — 무겁다. 별도 프로세스."""
    from .backtest import offload

    if _news_running["on"]:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    _news_running["on"] = True
    try:
        return await offload.run_job("news")
    finally:
        _news_running["on"] = False


@app.get("/api/regime/history")
async def api_regime_history(_=Depends(require_auth)):
    """국면 판정 이력 + 신호별 적중률(조회성 — 주문 없음).

    야간 리포트가 결정론 대조군(전일 breadth · 시가갭)을 이기는지 본다.
    못 이기면 `regime_gate.use_night_bias` 를 끄는 것이 정직한 결론이다.
    """
    from .data import regime_log

    def _build() -> dict:
        rows = regime_log.daily()
        since = rows[0]["date"] if rows else ""
        market = store.market_returns(since) if rows else {}
        return {
            "daily": [r | {"market_pct": (round(market[r["date"]], 3)
                                          if r["date"] in market else None)}
                      for r in rows],
            "recent": regime_log.entries(200),
            "score": regime_log.score(rows, market),
            "signals": list(regime_log.SIGNALS),
            "min_days": regime_log.MIN_DAYS,
        }

    return await asyncio.to_thread(_build)     # 일봉 집계 — 루프를 막지 않는다


@app.get("/api/scout")
async def api_scout(_=Depends(require_auth)):
    """발굴 엔진 상태 + 후보 큐 + 최근 결정(조회성 — 주문 없음).

    shadow 모드에서는 `decisions` 가 "엔진이라면 이렇게 했을 것" 이다. 화면이
    실제 감시목록과 나란히 놓고 보여 준다.
    """
    from .scout import engine as scout_mod
    from .scout import store as scout_store

    cur = scout_mod.snapshot_current()      # 모듈 함수다 — Engine 의 메서드가 아니다
    # 소스별 원시 결과 — 취합 **전** 각 패키지가 무엇을 올렸는지.
    # candidates 는 그룹 내 max 로 합쳐진 뒤라 어느 소스가 무엇을 봤는지 안 보인다.
    # 세기 0(목록 꼴찌)도 포함한다 — 그것도 그 소스가 본 것이다.
    by_source: dict[str, list] = {}
    for s in scout_store.live():
        by_source.setdefault(s.source, []).append({
            "code": s.code, "name": s.name, "kind": s.kind,
            "strength": round(s.strength, 3),
            "effective": round(s.effective(), 3),
            "raw": s.raw, "price": s.price, "evidence": s.evidence,
            "observed_at": s.observed_at.isoformat(timespec="seconds"),
            "age_sec": int(s.age_sec()),
            "tier": cur.tier.get(s.code, "none"),
        })
    for rows in by_source.values():
        rows.sort(key=lambda r: (-r["effective"], r["code"]))
    return {
        "status": scout.status(),
        "by_source": by_source,
        "candidates": [
            {"code": c.code, "name": c.name, "score": c.score, "price": c.price,
             "sources": c.sources, "by_group": c.by_group,
             "group_count": c.group_count,
             "tier": cur.tier.get(c.code, "none"),
             "protected": c.code in cur.protected,
             "evidence": c.evidence()}
            for c in scout.candidates[:100]
        ],
        "pending": scout.last_decisions,
        "watchlist": [{"code": k, "name": cur.names.get(k, k), "tier": v,
                       "protected": k in cur.protected}
                      for k, v in sorted(cur.tier.items())],
        "decisions": scout_store.recent_decisions(100),
    }


@app.post("/api/scout/mode")
async def api_scout_mode(payload: dict = Body(...), _=Depends(require_auth)):
    """모드 전환 · 킬 스위치. **배포 없이 되돌릴 수 있어야 한다** — 실거래 중이다."""
    from .scout import engine as scout_mod

    try:
        st = scout_mod.set_state(
            mode=payload.get("mode"),
            frozen=bool(payload["frozen"]) if "frozen" in payload else None)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    return {"ok": True, "state": st}


@app.post("/api/scout/run")
async def api_scout_run(_=Depends(require_auth)):
    """수동 1회 실행 — 다음 주기를 기다리지 않고 지금 본다."""
    scout.next_due.clear()
    return {"ok": True} | await scout.run_once()


@app.get("/api/research/event-study")
async def api_event_study(_=Depends(require_auth)):
    """발굴 점수 이벤트 스터디 최신 결과(조회성 — 주문 없음)."""
    from .research import eventstudy

    return await asyncio.to_thread(eventstudy.latest)   # 파일 1개 읽기


_study_running = {"on": False}


@app.post("/api/research/event-study/run")
async def api_event_study_run(_=Depends(require_auth)):
    """이벤트 스터디 실행. 전종목 × 전 구간이라 무겁다 — 별도 프로세스."""
    from .backtest import offload

    if _study_running["on"]:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    _study_running["on"] = True
    try:
        return await offload.run_job("study")
    finally:
        _study_running["on"] = False


@app.get("/api/research/trailing")
async def api_trailing(_=Depends(require_auth)):
    """라인 추종 스윕 최신 결과(조회성 — 주문 없음)."""
    import json as _json

    p = Path(settings.DATA_DIR) / "trailing.json"
    running = _trail_running["on"]
    if not p.exists():
        return {"ok": False, "running": running,
                "error": "실행 중입니다" if running else "아직 실행된 적이 없습니다"}
    try:
        got = await asyncio.to_thread(
            lambda: _json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        return {"ok": False, "running": running, "error": str(e)}
    return {"ok": True, "running": running,
            "updated_at": datetime.fromtimestamp(
                p.stat().st_mtime, tz=KST).isoformat(timespec="seconds")} | got


_trail_running = {"on": False}


@app.post("/api/research/trailing/run")
async def api_trailing_run(_=Depends(require_auth)):
    """라인 추종 스윕 실행. **즉시 돌아오고 결과는 파일로 남는다.**

    전 종목 × 변종 9종이라 수십 분 걸린다. 다른 research 잡처럼 결과를 기다리면
    호출자가 그동안 묶인다 — 2026-07-29 에 MCP 셸로 돌렸다가 실행 슬롯이 막혀
    장중 운영 명령이 통째로 멈췄다(`echo` 조차 타임아웃). 중단 명령도 같은
    슬롯에 걸려 못 들어갔다.

    그래서 여기서는 자식 프로세스를 띄우고 바로 응답한다. 진행 여부는
    `GET /api/research/trailing` 의 `running` 으로, 결과는 같은 응답으로 본다.
    """
    from .backtest import offload

    if _trail_running["on"]:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)

    async def _go():
        _trail_running["on"] = True
        try:
            got = await offload.run_job("trailing")
            log.info("라인 추종 스윕 완료: %s", str(got)[:200])
        finally:
            _trail_running["on"] = False

    asyncio.create_task(_go())
    return {"ok": True, "started": True,
            "hint": "GET /api/research/trailing 으로 진행·결과를 봅니다"}


@app.get("/api/research/trailing/real")
async def api_trailing_real(day: str | None = None, _=Depends(require_auth)):
    """실거래 진입을 그대로 두고 청산 정책만 투사한 결과.

    표본이 작아(원장 전체 57건) 값을 고르는 용도가 아니다 — "내 거래에 켰다면
    어땠나" 를 보는 경로다. 원장 조회 + 분봉 재생이라 가볍다(전종목 스윕과 다르다).
    """
    from .research import trailing as _t

    return await asyncio.to_thread(_t.analyze_real, None, day)


@app.get("/api/research/ranking")
async def api_ranking(_=Depends(require_auth)):
    """랭킹 방식 비교 최신 결과(조회성 — 주문 없음)."""
    from .research import ranking

    return await asyncio.to_thread(ranking.latest)


_rank_running = {"on": False}


@app.post("/api/research/ranking/run")
async def api_ranking_run(_=Depends(require_auth)):
    """랭킹 방식 비교 실행. 전종목 × 전 구간 × 방식 6종이라 무겁다 — 별도 프로세스."""
    from .backtest import offload

    if _rank_running["on"]:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    _rank_running["on"] = True
    try:
        return await offload.run_job("rank")
    finally:
        _rank_running["on"] = False


@app.get("/api/backtest/sweep/latest")
async def api_sweep_latest(_=Depends(require_auth)):
    """주간 기법 스윕 최신 성적표(규칙별 격리 백테스트, 롱 방향)."""
    from .backtest import sweep

    return sweep.latest()


@app.post("/api/backtest/sweep/run")
async def api_sweep_run(_=Depends(require_auth)):
    """기법 스윕 수동 실행(수 분 소요, 조회성 — 주문 없음). 백그라운드 시작."""
    from .backtest import sweep

    if sweep.running:
        return JSONResponse({"ok": False, "error": "이미 실행 중"}, 409)
    asyncio.create_task(sweep.run_offloaded())   # 별도 프로세스

    return {"ok": True, "message": "스윕 시작 — 수 분 후 결과가 갱신됩니다"}


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


_realized_cache: dict = {"ts": 0.0, "data": None}


@app.get("/api/account/realized")
async def api_account_realized(days: int = 30, _=Depends(require_auth)):
    """계좌 실현손익(증권사) vs 엔진 실현손익(원장), 그리고 그 차이.

    둘은 **원래 다르다.** 원장은 승인·발주된 주문만 기록하므로 수동매매가
    안 들어온다. 실측 2026-07-28: 계좌 +181,019 vs 원장 −23,465 인데, 차이의
    99.9%가 원장에 없는 7/16 한 건(+204,645)이었다.

    그래서 하나로 합치지 않고 나란히 둔다 — 차이 자체가 '수동매매분' 이라는
    정보다. 지금은 엔진 값만 '실현손익' 이라 부르고 있어서 계좌와 다르면
    버그처럼 보인다.
    """
    import time
    from datetime import timedelta

    from .kiwoom.client import client
    from .kiwoom.realized import parse_daily
    from .trade import ledger

    now = time.monotonic()
    if _realized_cache["data"] and now - _realized_cache["ts"] < 60:
        return _realized_cache["data"]

    today = datetime.now(KST).date()
    start = (today - timedelta(days=max(1, days))).strftime("%Y%m%d")
    broker = {"ok": False, "error": "API 키 미설정"}
    if settings.KIWOOM_APP_KEY:
        try:
            broker = parse_daily(
                await client.daily_realized(start, today.strftime("%Y%m%d")))
        except Exception as e:  # noqa: BLE001 - 조회 실패는 화면에 표시
            broker = {"ok": False, "error": str(e)}

    closed = ledger.positions(status="closed", limit=1000)
    engine_krw = round(sum(p.get("pnl_krw") or 0 for p in closed), 0)
    # 화면의 '오늘 실현손익' 은 이 값을 쓴다. 원장 합산은 모델 비용이라
    # 2026-07-28 실측에서 손실을 1,796원 과대계상했고, 가드가 그 값으로 판정했다.
    ymd = today.strftime("%Y%m%d")
    today_row = next((d for d in (broker.get("days") or []) if d["date"] == ymd), None)
    data = {
        "period": {"start": start, "end": ymd},
        "broker": broker,
        "today": today_row,
        "engine": {"realized": engine_krw, "trades": len(closed)},
        # 증권사가 못 세는 게 아니라 원장이 못 보는 것이다 — 이름을 그렇게 붙인다
        "untracked": (round(broker.get("realized", 0) - engine_krw, 0)
                      if broker.get("ok") else None),
        "unconfirmed": {
            "entries": sum(1 for p in closed if not p.get("fill_confirmed")),
            "exits": sum(1 for p in closed if not p.get("exit_fill_confirmed")),
        },
    }
    if broker.get("ok"):
        _realized_cache.update(ts=now, data=data)
    return data


@app.post("/api/account/reconcile")
async def api_account_reconcile(_=Depends(require_auth)):
    """증권사 체결내역으로 원장의 추정 진입·청산가를 실측 확정한다.

    실시간 체결 수신을 놓친 건을 마감 후 메우는 경로다. 멱등이라 여러 번
    돌려도 비용이 겹치지 않는다.
    """
    from .kiwoom.client import client
    from .kiwoom.realized import parse_executions
    from .trade import ledger

    if not settings.KIWOOM_APP_KEY:
        return {"ok": False, "error": "API 키 미설정"}
    try:
        execs = parse_executions(await client.executions())
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    got = ledger.reconcile_executions(execs)
    _realized_cache["data"] = None
    return {"ok": True, "fills": len(execs)} | got


@app.get("/api/risk")
async def api_risk(_=Depends(require_auth)):
    """일일 목표·손실 가드 상태 + 오늘 실현손익 + 설정값."""
    return engine.day_guard_status() | {
        "risk_per_trade_pct": settings.RISK.get("risk_per_trade_pct", 0),
        "max_positions": settings.RISK.get("max_positions", 3),
        "auto_approve": bool(settings.RISK.get("auto_approve", False)),
        "scan_interval_sec": engine.scan_interval(),
        "scan_interval_range": [settings.SCAN_INTERVAL_MIN_SEC,
                                settings.SCAN_INTERVAL_MAX_SEC],
        "max_position_weight_pct": engine.max_position_weight_pct(),
        "long_only": bool(settings.RISK.get("long_only", False)),
        "signal_ttl_min": settings.RISK.get("signal_ttl_min", 10),
        # 돌파 확인: risk.json 오버라이드가 없으면 config.yaml 의 rules 기본값
        "confirm_on_close": bool(settings.RISK.get(
            "confirm_on_close", settings.RULES.get("confirm_on_close", False))),
    }


@app.post("/api/risk")
async def api_risk_save(payload: dict, _=Depends(require_auth)):
    """일일 목표·손실한도·거래당 리스크 설정(영속). 목표값을 UI 에서 조정."""
    try:
        settings.save_risk(
            daily_target_pct=payload.get("daily_target_pct"),
            daily_loss_limit_pct=payload.get("daily_loss_limit_pct"),
            risk_per_trade_pct=payload.get("risk_per_trade_pct"),
            auto_approve=payload.get("auto_approve"),
            scan_interval_sec=payload.get("scan_interval_sec"),
            max_position_weight_pct=payload.get("max_position_weight_pct"),
            confirm_on_close=payload.get("confirm_on_close"),
            max_positions=payload.get("max_positions"),
            long_only=payload.get("long_only"),
            signal_ttl_min=payload.get("signal_ttl_min"),
        )
    except (OSError, ValueError, TypeError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, 400)
    log.info("리스크 설정 갱신: %s", {k: settings.RISK.get(k) for k in
             ("daily_target_pct", "daily_loss_limit_pct", "risk_per_trade_pct",
              "scan_interval_sec", "max_position_weight_pct", "confirm_on_close")})
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
    """**실제 매도를 발주**하고 체결 접수된 경우에만 원장을 닫는다.

    2026-07-29 까지 이 경로는 주문을 내지 않고 원장만 닫았다(`close_position` 직접
    호출). 화면 버튼은 '청산' 이라고 보이는데 계좌에는 종목이 그대로 남아, 원장과
    계좌가 어긋난 **고아 종목**이 생겼다. 그날 고아가 만들어진 원인 중 하나다.

    사람이 화면에서 명시적으로 누른 것이 이미 승인이므로 즉시 시장가로 나간다.
    발주가 실패하면 `execute_exit` 가 원장을 닫지 않는다 — 그것이 이 수정의 핵심이다.
    계좌와 어긋난 항목을 장부에서만 정리하려면 `/void` 를 쓴다.
    """
    from .trade import ledger, orders

    pos = next((p for p in ledger.positions(status="open", limit=200)
                if p["id"] == pos_id), None)
    if not pos:
        return JSONResponse({"ok": False, "error": "오픈 포지션 없음"}, 404)
    px = _price_of(pos["symbol"]) or pos["entry"]
    return await orders.execute_exit(pos, "manual", float(px))


@app.post("/api/positions/{pos_id}/void")
async def api_position_void(pos_id: str, payload: dict | None = Body(None),
                            _=Depends(require_auth)):
    """계좌와 어긋난 원장 항목을 무효 처리한다(**주문 없음**).

    청산이 아니다 — 계좌에 실재하지 않는데 원장에만 남은 고아를 지우는 경로다.
    `void` 는 실현손익·가드·일지·성과 집계에서 빠지고 감사 흔적은 남는다.

    계좌에 **실재하는** 종목을 무효 처리하면 청산 감시가 사라지므로, 보유 수량이
    0이 아니면 `confirm: true` 없이는 거절한다. 계좌 조회에 실패하면 수량을 모르는
    것이므로 마찬가지로 거절한다 — 모르는 상태에서 지우지 않는다.
    """
    from .trade import ledger

    pos = next((p for p in ledger.positions(status="open", limit=200)
                if p["id"] == pos_id), None)
    if not pos:
        return JSONResponse({"ok": False, "error": "오픈 포지션 없음"}, 404)
    holdings = await _holdings_map()
    # 계좌에 실재하는지는 **실제 체결 종목**으로 본다 — 숏은 인버스 ETF 로 나간다
    sym = ledger.executed_symbol(pos)
    held = None if holdings is None else int(holdings.get(sym, 0) or 0)
    confirm = bool((payload or {}).get("confirm"))
    if not confirm and held != 0:
        return {"ok": False, "need_confirm": True, "held_qty": held,
                "ledger_qty": pos.get("qty"),
                "held_symbol": sym,
                "error": ("계좌 보유 수량을 확인할 수 없습니다"
                          if held is None else
                          f"계좌에 {sym} {held}주가 실재합니다 — 무효 처리하면 "
                          "청산 감시가 사라집니다")}
    ok = await asyncio.to_thread(ledger.void_position, pos_id, "수동 제외")
    if ok:
        log.warning("포지션 수동 무효 처리: %s(%s) 원장 %s주 · 계좌 %s주",
                    pos.get("name"), pos.get("symbol"), pos.get("qty"), held)
    return {"ok": ok, "held_qty": held, "ledger_qty": pos.get("qty")}


@app.post("/api/desk")
async def api_desk_set(payload: dict | None = Body(None), _=Depends(require_auth)):
    """매매 데스크를 **배포 없이** 켜고 끈다(런타임 오버라이드 → `desk.json`).

    초 단위로 실거래 청산을 내는 루프이므로 이상이 보이면 그 자리에서 멈출 수
    있어야 한다. 루프 자체는 항상 떠 있고 매 사이클 설정을 다시 읽으므로
    재시작이 필요 없다 — 끄면 다음 사이클부터 아무것도 하지 않는다.

    끈 동안 청산 감시가 비지 않는다. 기존 30초 `_ledger_loop` 가 그대로 맡는다.
    """
    from .trade import desk as _desk

    p = payload or {}
    try:
        st = await asyncio.to_thread(
            _desk.set_state,
            enabled=p.get("enabled"), interval_sec=p.get("interval_sec"),
            stale_sec=p.get("stale_sec"), max_symbols=p.get("max_symbols"),
            degraded_interval_sec=p.get("degraded_interval_sec"),
            trailing=p.get("trailing"))
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": f"잘못된 값: {e}"}, 400)
    return {"ok": True, "override": st, "desk": _desk.status()}


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
