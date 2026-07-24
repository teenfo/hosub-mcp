"""주간 기법 스윕 — 등록된 모든 규칙을 격리 백테스트해 기법별 성적표를 남긴다.

매주 토요일 오전(장 없음)에 축적 분봉 전체 × 규칙별 단독 백테스트(롱 방향)를
돌려 rule_sweep.json 에 저장한다. 매매 기법 페이지가 이 성적표를 카드에 표시해
'구현 → 스윕 검증 → 데이터 판정' 연구 사이클을 자동화한다.
"""
import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings
from . import runner

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
OUT_FILE = Path(settings.DATA_DIR) / "rule_sweep.json"


def run_sweep() -> dict:
    """전 감시목록 분봉 × 레지스트리 전 규칙(각각 단독 활성) 백테스트."""
    from ..data import store
    from ..signals.rules import REGISTRY

    risk_pct = settings.RISK.get("risk_per_trade_pct", 0.8)
    costs = settings.COSTS
    max_stop = settings.RULES.get("max_stop_pct", 0)
    dfs = {}
    for sym in list(settings.WATCHLIST):
        df = store.load_bars(sym, "1m", limit=5000)
        if len(df) >= 200:
            dfs[sym] = df
    results: dict = {}
    for name in REGISTRY:
        rule_cfg = dict(settings.RULES.get(name, {}))
        rule_cfg["enabled"] = True
        cfg = {name: rule_cfg, "max_stop_pct": max_stop}
        trades = []
        for sym, df in dfs.items():
            try:
                trades.extend(runner.run(sym, df, cfg).trades)
            except Exception:  # noqa: BLE001 - 종목 하나의 오류가 스윕을 막지 않게
                log.exception("스윕 오류 %s/%s", name, sym)
        # 실전은 롱 전용이므로 롱 방향 성적만 채점
        closed = [t for t in trades if t.exit is not None and t.side == "long"]
        if not closed:
            results[name] = {"trades": 0}
            continue
        rs = [t.r_multiple(costs) for t in closed]
        accs = [t.account_pct(costs, risk_pct) for t in closed]
        wins = sum(1 for r in rs if r > 0)
        results[name] = {
            "trades": len(closed),
            "win_rate": round(100 * wins / len(closed), 1),
            "avg_r": round(sum(rs) / len(rs), 2),
            "account_pct": round(sum(accs), 2),
        }
    out = {"run_ts": datetime.now(KST).isoformat(timespec="seconds"),
           "symbols": len(dfs), "side": "long", "rules": results}
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False))
    log.info("주간 기법 스윕 완료: %d종목 × %d규칙", len(dfs), len(results))
    return out


def latest() -> dict:
    try:
        return json.loads(OUT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


async def loop(check_sec: int = 60) -> None:
    """매주 토요일 09시(KST)에 1회 실행. 마지막 실행일 기준 중복 방지."""
    while True:
        try:
            now = datetime.now(KST)
            cfg = settings.CONFIG.get("sweep", {})
            if (
                cfg.get("enabled", True)
                and settings.KIWOOM_APP_KEY
                and now.weekday() == cfg.get("weekday", 5)   # 5=토요일
                and now.hour == cfg.get("hour", 9)
                and latest().get("run_ts", "")[:10] != now.date().isoformat()
            ):
                await asyncio.to_thread(run_sweep)
        except Exception:  # noqa: BLE001
            log.exception("주간 스윕 루프 오류")
        await asyncio.sleep(check_sec)
