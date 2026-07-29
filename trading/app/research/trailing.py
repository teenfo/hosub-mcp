"""라인 추종 룰 타당성 검증 — 같은 진입에 청산 정책만 바꿔 재생한다.

## 무엇을 재는가

사용자 지침(2026-07-29)으로 들어간 `desk.update_lines` 가 **고정 손절·목표보다
나은가**, 그리고 `lock_gain_pct`(상승분의 몇 %를 손절선으로 확정할지)의 **적정값**이
얼마인지를 축적된 1분봉으로 되돌려 잰다.

    fixed      : 지금까지의 동작 — 진입 시 정한 손절·목표 고정
    trail      : 추종만 (같은 갭으로 따라 올리고, 내려도 유지). 확정·상한 없음
    lock{N}    : 추종 + 목표갭 50% 도달 시 **상승분의 N%** 확정 + 익절 상한 3%

**청산 정책만 바꾼다.** 진입 신호·체결가·비용은 `backtest.runner` 와 같은 코드를
쓰고 통계도 `runner.Result.stats` 를 그대로 쓴다. 차이가 정책에서 온 것임이
분명해야 한다.

## 스윕이 싸야 한다 — 신호는 한 번만 계산한다

`rules.evaluate_all` 을 확장 창으로 도는 것이 전체 비용의 대부분이다(종목당
4,500봉 × 규칙 수). 정책마다 다시 돌리면 N배가 되고, 실제로 그렇게 돌렸다가
장중 서버를 먹었다(2026-07-29).

그래서 **신호 스캔을 한 번 하고 그 결과로 정책별 재생만 반복**한다. 재생은
산술 연산뿐이라 사실상 공짜다. 스윕 12개가 단일 재생 1회에 가까운 비용이 된다.

## 모델링 — 어디서 낙관이 새는지 미리 못박는다

1분봉으로 초 단위 루프를 재생할 수는 없다. 세 가지를 **불리한 쪽**으로 고정했다.

- **라인 갱신은 봉 종가로만 한다.** 실제 데스크는 고가 부근에서도 갱신하지만,
  봉 안의 순서를 모르는 채 고가로 갱신하면 "고점에서 끌어올린 손절선에 같은 봉
  저가가 닿는" 불가능한 이익을 만든다. 종가 갱신은 추종을 **과소평가**한다.
- **청산 판정이 갱신보다 먼저다.** 같은 봉에서 손절과 목표가 함께 닿으면 손절.
- **틱이 아니라 분이다.** 데스크는 2초마다 보는데 여기서는 60초마다 본다.

반대 방향의 낙관도 하나 있다: **손절선에 닿으면 그 가격 그대로 체결된다고
가정한다.** 실제로는 breach 를 보고 시장가를 던지므로 체결은 그보다 나쁘다.
손절 청산 건수는 N 이 오를수록 늘어나므로(실측 1,643 → 3,068건) 이 낙관도
**N 이 클수록 커진다.** 비용 모델의 슬리피지가 일부 상쇄하지만 전부는 아니다.

(초판 주석에서 이 낙관을 '봉 안에서 손절선을 스치고 되돌아온 경우를 못 잡는다'
라고 적었는데 **틀렸다.** 재생은 `bar.low <= stop` 으로 그 경우를 잡는다.
실제 낙관은 위의 체결가 가정이다.)

## 최적값을 고를 때의 함정

12거래일 남짓한 표본에서 12개 값 중 최고를 고르면 그건 **표본에 맞춘 것**이다.
1.5단계에서 이미 같은 실수를 피한 적이 있다(in-sample vs walk-forward).
그래서 이 모듈은 최고값을 '정답'으로 내지 않는다 — **곡선의 모양**과 인접값과의
차이를 함께 낸다. 이웃한 값들이 고르게 좋은 구간이 뾰족한 최고점보다 믿을 만하다.
"""
import json
import logging
import multiprocessing as mp
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .. import settings
from ..backtest import runner
from ..data import store
from ..signals import rules
from ..trade import desk

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")

# 스윕 값 — 사용자 지시(2026-07-29): 30 부터 올리며 본다
LOCK_GRID = (30, 40, 50, 60, 70, 80, 90)
BASELINES = ("fixed", "trail")


def variants() -> list[str]:
    return [*BASELINES, *(f"lock{n}" for n in LOCK_GRID)]


def _cfg_of(variant: str) -> dict | None:
    """정책별 `desk` 설정. None 이면 라인을 아예 갱신하지 않는다(fixed)."""
    if variant == "fixed":
        return None
    if variant == "trail":
        # 추종만 — 확정은 닿을 수 없는 발동선으로, 상한은 0(비활성)으로 끈다
        return {"enabled": True, "trailing": True,
                "tighten_at": 99.0, "lock_gain_pct": 0.0, "max_gain_pct": 0.0}
    return {"enabled": True, "trailing": True,
            "lock_gain_pct": float(variant.removeprefix("lock"))}


# --------------------------------------------------------------------------
# 1) 신호 스캔 — 종목당 한 번만
# --------------------------------------------------------------------------
def scan(symbol: str, df, rules_cfg: dict, sides: tuple[str, ...] | None):
    """[(day, bars, [(진입봉 위치, sig), ...]), ...].

    `runner.run` 과 같은 진입 규칙이되 **보유 여부를 보지 않는다.** 어느 신호를
    실제로 잡을지는 정책마다 다르므로(청산이 빠르면 다음 신호를 잡는다) 여기서는
    후보만 모으고 판단은 재생 쪽에 맡긴다.
    """
    out = []
    days_idx = df.index.normalize()
    for day, day_df in df.groupby(days_idx):
        prev = df[days_idx < day]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        bars = list(day_df.itertuples())
        sigs = []
        for i in range(10, len(bars) - 1):
            window = day_df.iloc[: i + 1]
            for sig in rules.evaluate_all(window, rules_cfg, prev_close):
                if sides and sig.side not in sides:
                    continue
                sigs.append((i, sig))
        out.append((day, bars, sigs))
    return out


# --------------------------------------------------------------------------
# 2) 재생 — 정책만 바꿔 반복 (싸다)
# --------------------------------------------------------------------------
def _lines(entry_price: float, stop0: float, target0: float, side: str,
           stop: float, target: float, price: float):
    pos = {"side": side, "entry": entry_price, "stop": stop0, "target": target0,
           "stop_live": stop if stop != stop0 else None,
           "target_live": target if target != target0 else None}
    got = desk.update_lines(pos, price)
    if got is None:
        return stop, target
    return (got[0] if got[0] is not None else stop0,
            got[1] if got[1] is not None else target0)


def replay(symbol: str, scanned, hold_limit: int, update: bool) -> list:
    """스캔 결과를 현재 `desk` 설정으로 재생한다. `update=False` 면 fixed."""
    slip = settings.COSTS.get("slippage_bp", 5) / 10000
    trades = []
    for _day, bars, sigs in scanned:
        by_bar: dict[int, list] = {}
        for i, sig in sigs:
            by_bar.setdefault(i, []).append(sig)
        fired: set[str] = set()
        t = None
        stop = target = 0.0
        clock = None
        for i in range(10, len(bars)):
            bar = bars[i]
            if t is not None:
                if t.side == "long":
                    hit_stop, hit_target = bar.low <= stop, bar.high >= target
                else:
                    hit_stop, hit_target = bar.high >= stop, bar.low <= target
                if hit_stop:
                    t.exit, t.exit_reason, t.exit_ts = stop, "stop", bar.Index
                elif hit_target:
                    t.exit, t.exit_reason, t.exit_ts = target, "target", bar.Index
                elif hold_limit and clock is not None \
                        and bar.Index - clock >= timedelta(minutes=hold_limit):
                    t.exit, t.exit_reason, t.exit_ts = \
                        float(bar.close), "timeout", bar.Index
                if t.exit is not None:
                    trades.append(t)
                    t = None
                    continue
                if update:      # 청산이 없을 때만 갱신한다(판정이 갱신보다 먼저)
                    new_stop, new_target = _lines(
                        t.entry, t.stop, t.target, t.side, stop, target,
                        float(bar.close))
                    if new_stop != stop:
                        clock = bar.Index     # 손절선이 움직였다 → 시계 재시작
                    stop, target = new_stop, new_target
                continue
            for sig in by_bar.get(i, ()):
                if sig.rule in fired:
                    continue
                fired.add(sig.rule)
                nxt = bars[i + 1]
                fill = nxt.open * (1 + slip if sig.side == "long" else 1 - slip)
                t = runner.Trade(symbol, sig.rule, sig.side, nxt.Index,
                                 fill, sig.stop, sig.target)
                stop, target, clock = sig.stop, sig.target, nxt.Index
                break
        if t is not None and bars:
            t.exit = float(bars[-1].close)
            t.exit_reason, t.exit_ts = "eod", bars[-1].Index
            trades.append(t)
    return trades


def run_symbol(symbol: str, df, rules_cfg: dict | None = None,
               sides: tuple[str, ...] | None = ("long",),
               names: list[str] | None = None) -> dict:
    """한 종목을 모든 변종으로 재생. **신호 스캔은 한 번만 한다.**"""
    rules_cfg = rules_cfg or settings.RULES
    hold_limit = rules_cfg.get("max_hold_min", 0) or 0
    scanned = scan(symbol, df, rules_cfg, sides)

    out = {}
    # 런타임 오버라이드(`data/desk.json`)를 무시한다 — 실서버에서 데스크를 꺼둔
    # 채로 돌리면 전 변종이 fixed 가 되어 비교가 성립하지 않는다.
    old_file, desk.STATE_FILE = desk.STATE_FILE, Path("/nonexistent/desk.json")
    old = (settings.CONFIG.get("execution", {}) or {}).get("desk")
    try:
        for v in (names or variants()):
            cfg = _cfg_of(v)
            if cfg is None:
                out[v] = replay(symbol, scanned, hold_limit, update=False)
                continue
            settings.CONFIG.setdefault("execution", {})["desk"] = cfg
            out[v] = replay(symbol, scanned, hold_limit, update=True)
    finally:
        desk.STATE_FILE = old_file
        if old is None:
            settings.CONFIG.get("execution", {}).pop("desk", None)
        else:
            settings.CONFIG["execution"]["desk"] = old
    return out


# --------------------------------------------------------------------------
# 3) 집계
# --------------------------------------------------------------------------
def _key(t) -> tuple:
    return (t.symbol, t.rule, t.side, t.entry_ts)


def _exit_mix(trades) -> dict:
    mix: dict[str, int] = {}
    for t in trades:
        if t.exit is not None:
            mix[t.exit_reason] = mix.get(t.exit_reason, 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: -kv[1]))


def _workers(want: int = 0) -> int:
    """기본은 코어 수 − 2. 매매 프로세스와 WS 수신에 여유를 남긴다(리포트와 동일)."""
    if want > 0:
        return want
    return max(1, min(6, (os.cpu_count() or 2) - 2))


def _one(arg):
    """워커 진입점. 종목당 (스캔 1회 + 변종별 재생)."""
    sym, min_bars, limit_days, sides, names = arg
    df = store.load_bars(sym, "1m", limit=limit_days * 400)
    if df.empty or len(df) < min_bars:
        return None
    try:
        return sym, run_symbol(sym, df, sides=sides, names=names)
    except Exception:  # noqa: BLE001 - 한 종목 실패가 전체를 멈추지 않는다
        log.exception("재생 실패 %s", sym)
        return None


def analyze(symbols: list[str], min_bars: int = 600, limit_days: int = 60,
            sides: tuple[str, ...] | None = ("long",),
            names: list[str] | None = None, workers: int = 0) -> dict:
    """전 종목을 전 변종으로 재생하고 비교표를 낸다.

    `paired` 는 **모든 변종이 같은 진입을 잡은 거래**만 모은 것이다. 청산이
    빨라지면 다음 신호를 잡을 수 있어 진입 자체가 달라지는데, 그 차이까지 섞이면
    청산 정책의 효과를 읽을 수 없다.

    종목 간에는 의존이 없어 코어에 나눠 돌린다 — 순차로는 200종목이 한 시간을
    넘긴다(리포트가 같은 이유로 병렬화돼 있다).
    """
    names = names or variants()
    costs, risk = settings.COSTS, settings.RISK.get("risk_per_trade_pct", 0.5)
    per = {v: [] for v in names}
    used = skipped = 0

    args = [(s, min_bars, limit_days, sides, names) for s in symbols]
    n = _workers(workers)
    if n <= 1 or len(args) <= 1:
        results = map(_one, args)
    else:
        ctx = mp.get_context("spawn")   # fork 는 부모의 열린 SQLite 핸들을 물려받는다
        with ctx.Pool(n) as pool:
            results = pool.map(_one, args, chunksize=2)
        log.info("추종 스윕: %d종목 / 워커 %d", len(symbols), n)

    for got in results:
        if got is None:
            skipped += 1
            continue
        used += 1
        for v in names:
            per[v].extend(got[1][v])

    common = set.intersection(*({_key(t) for t in per[v]} for v in names)) \
        if names else set()

    def _stats(trades):
        return runner.Result(trades=list(trades)).stats(costs, risk) | {
            "exits": _exit_mix(trades)}

    all_stats = {v: _stats(per[v]) for v in names}
    paired = {v: _stats([t for t in per[v] if _key(t) in common]) for v in names}
    locks = [v for v in names if v.startswith("lock")]
    return {
        "symbols_used": used, "symbols_skipped": skipped,
        "paired_trades": len(common),
        "all": all_stats, "paired": paired,
        "best": _pick(paired, locks) if locks else None,
        "params": {"grid": list(LOCK_GRID),
                   "tighten_at": desk._num("tighten_at"),
                   "max_gain_pct": desk._num("max_gain_pct"),
                   "max_hold_min": settings.RULES.get("max_hold_min", 0),
                   "sides": list(sides) if sides else None},
    }


def _pick(stats: dict, locks: list[str]) -> dict:
    """최고값과 **이웃 평균**을 함께 낸다.

    12거래일 표본에서 격자 중 최고를 고르면 그건 표본에 맞춘 것이다. 이웃한
    값들이 고르게 좋은 구간이 뾰족한 최고점보다 믿을 만하므로, 인접 3점 평균이
    가장 높은 값(`robust`)을 같이 내고 둘이 다르면 그 사실 자체를 경고로 읽는다.
    """
    r = {v: (stats[v].get("avg_r") or 0.0) for v in locks}
    best = max(locks, key=lambda v: r[v])
    win = {}
    for i, v in enumerate(locks):
        near = [r[locks[j]] for j in (i - 1, i, i + 1) if 0 <= j < len(locks)]
        win[v] = sum(near) / len(near)
    # 격자 양 끝은 한쪽에 이웃이 없어 근거가 약하다 — 안쪽에서만 고른다
    inner = locks[1:-1] or locks
    robust = max(inner, key=lambda v: win[v])
    return {"by_avg_r": best, "avg_r": r[best],
            "robust": robust, "robust_neighborhood_avg_r": round(win[robust], 4),
            "agree": best == robust, "curve": {v: round(r[v], 4) for v in locks}}


def run_once() -> dict:  # pragma: no cover - 자식 프로세스에서 호출
    syms = [s for s, _ in store.minute_symbols(min_days=3)]
    got = analyze(syms)
    out = Path(settings.DATA_DIR) / "trailing.json"
    out.write_text(json.dumps(got, ensure_ascii=False, default=str),
                   encoding="utf-8")
    return got


def main() -> None:  # pragma: no cover - CLI
    import sys

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    print(json.dumps(run_once(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()


# --------------------------------------------------------------------------
# 4) 실거래 투사 — 진입은 실제 체결, 청산만 정책별로 다시 본다
# --------------------------------------------------------------------------
# 사용자 제안(2026-07-29): "실거래의 진입 금액과 시점만 투사하고, 청산은 차트로
# 확인한다." 규칙이 만들어낸 가짜 진입 대신 **내가 실제로 잡은 자리**에서 청산만
# 바꿔 본다 — 진입 규칙의 성적이 청산 정책 비교에 섞이지 않는다.
#
# 다만 **봉 해상도의 낙관은 이 방식으로도 사라지지 않는다.** 낙관은 진입이 아니라
# 청산 모델에 있다(체결가 가정·봉 종가 갱신). 이 방식이 없애는 것은 진입 편향이다.
# 그리고 표본이 작다 — 원장 전체가 57건이다. 값을 고르기엔 모자라고,
# "내 거래에 켰다면 어땠나" 를 보는 용도다.


def _bars_after(symbol: str, day: str, after: str):
    """그 날의 1분봉 중 진입 시각 **이후**만. 없으면 빈 리스트."""
    df = store.load_bars(symbol, "1m", limit=4000)
    if df.empty:
        return []
    same = df[df.index.normalize() == pd.Timestamp(day, tz=KST)]
    if same.empty:
        return []
    try:
        t0 = datetime.fromisoformat(after)
    except (TypeError, ValueError):
        return []
    if t0.tzinfo is None:
        t0 = t0.replace(tzinfo=KST)
    return list(same[same.index > t0].itertuples())


def _above(pos: dict, px: float) -> bool:
    """청산가가 진입가보다 **유리한 쪽**인가(롱은 위, 숏은 아래)."""
    e = float(pos["entry"])
    return px > e if pos["side"] == "long" else px < e


def _project(pos: dict, bars, update: bool, hold_limit: int) -> tuple[float, str]:
    """한 포지션을 정책 하나로 재생 → (청산가, 사유). 봉이 없으면 실제값 그대로."""
    entry = float(pos["entry"])
    stop, target = float(pos["stop"]), float(pos["target"])
    side = pos["side"]
    clock = bars[0].Index if bars else None
    for bar in bars:
        if side == "long":
            hit_stop, hit_target = bar.low <= stop, bar.high >= target
        else:
            hit_stop, hit_target = bar.high >= stop, bar.low <= target
        if hit_stop:
            return stop, "stop"
        if hit_target:
            return target, "target"
        if hold_limit and clock is not None \
                and bar.Index - clock >= timedelta(minutes=hold_limit):
            return float(bar.close), "timeout"
        if update:
            new_stop, new_target = _lines(entry, float(pos["stop"]),
                                          float(pos["target"]), side,
                                          stop, target, float(bar.close))
            if new_stop != stop:
                clock = bar.Index
            stop, target = new_stop, new_target
    return (float(bars[-1].close), "eod") if bars else (
        float(pos["exit"] or entry), "실제")


def analyze_real(names: list[str] | None = None, day: str | None = None) -> dict:
    """실제 청산된 포지션에 정책별 청산을 투사한다.

    비교 기준은 **같은 비용 모델로 다시 계산한 실제 청산**이다(`as_executed`).
    원장에 적힌 손익과는 조금 다를 수 있는데, 그건 증권사 실측 비용이 반영돼
    있기 때문이다 — 정책 간 비교를 같은 자로 하기 위해 이렇게 둔다.
    """
    from ..trade import ledger

    names = names or variants()
    costs = settings.COSTS
    hold_limit = settings.RULES.get("max_hold_min", 0) or 0
    rows = [p for p in ledger.positions(status="closed", limit=2000)
            if p.get("exit") and p.get("opened")]
    if day:
        rows = [p for p in rows if str(p["opened"])[:10] == day]

    def _krw(pos, exit_px):
        t = runner.Trade(pos["symbol"], pos.get("rule") or "", pos["side"],
                         None, float(pos["entry"]), float(pos["stop"]),
                         float(pos["target"]), None, float(exit_px), "")
        return t.pnl_pct(costs) / 100 * float(pos["entry"]) * int(pos["qty"] or 0)

    def _blank():
        return {"krw": 0.0, "wins": 0, "exits": {}, "by_reason": {}}

    def _tally(d, reason, k, above):
        """사유별로 **건수만이 아니라 손익과 방향**을 함께 센다.

        추종을 켜면 `stop` 사유가 늘어나는데, 그중 상당수는 **진입가 위에서
        잘린 익절**이다. 사유 이름만 세면 "손절이 늘었다" 로 잘못 읽힌다
        (2026-07-29 사용자 지적). 그래서 진입가 대비 방향을 같이 남긴다.
        """
        d["exits"][reason] = d["exits"].get(reason, 0) + 1
        key = f"{reason}+" if above else f"{reason}-"
        b = d["by_reason"].setdefault(key, {"n": 0, "krw": 0.0})
        b["n"] += 1
        b["krw"] += k

    out = {v: _blank() for v in names}
    base = _blank()
    # 거래 하나가 정책을 바꾸면 **어떤 청산에서 어떤 청산으로** 갔는지.
    # 총합만 보면 "왜" 를 못 읽는다 — 사유가 바뀐 자리가 손익을 만든다.
    moves: dict[str, dict[str, dict]] = {v: {} for v in names}
    used = no_bars = 0
    old_file, desk.STATE_FILE = desk.STATE_FILE, Path("/nonexistent/desk.json")
    old = (settings.CONFIG.get("execution", {}) or {}).get("desk")
    try:
        for pos in rows:
            bars = _bars_after(pos["symbol"], str(pos["opened"])[:10],
                               str(pos["opened"]))
            if not bars:
                no_bars += 1
                continue
            used += 1
            k = _krw(pos, pos["exit"])
            base["krw"] += k
            base["wins"] += 1 if k > 0 else 0
            _tally(base, pos.get("exit_reason") or "?", k,
                   _above(pos, float(pos["exit"])))
            per_pos = {}
            for v in names:
                cfg = _cfg_of(v)
                settings.CONFIG.setdefault("execution", {})["desk"] = \
                    cfg or {"enabled": False}
                px, why = _project(pos, bars, cfg is not None, hold_limit)
                k = _krw(pos, px)
                out[v]["krw"] += k
                out[v]["wins"] += 1 if k > 0 else 0
                _tally(out[v], why, k, _above(pos, px))
                per_pos[v] = (why, k, _above(pos, px))
            if "fixed" in per_pos:
                f_why, f_k, f_up = per_pos["fixed"]
                for v, (why, k, up) in per_pos.items():
                    key = (f"{f_why}{'+' if f_up else '-'}"
                           f" → {why}{'+' if up else '-'}")
                    m = moves[v].setdefault(key, {"n": 0, "delta": 0.0})
                    m["n"] += 1
                    m["delta"] += k - f_k
    finally:
        desk.STATE_FILE = old_file
        if old is None:
            settings.CONFIG.get("execution", {}).pop("desk", None)
        else:
            settings.CONFIG["execution"]["desk"] = old

    def _fin(d):
        return {"krw": round(d["krw"], 0),
                "win_rate": round(d["wins"] / used * 100, 1) if used else 0.0,
                "exits": dict(sorted(d["exits"].items(), key=lambda kv: -kv[1])),
                # 사유 뒤 +/− 는 **진입가 대비 방향**이다. `stop+` 은 따라 올라간
                # 손절선에서 이익으로 끝난 것 — 이름만 손절이지 익절이다
                "by_reason": {k: {"n": v["n"], "krw": round(v["krw"], 0)}
                              for k, v in sorted(d["by_reason"].items())}}

    return {"trades": used, "no_bars": no_bars, "day": day,
            "as_executed": _fin(base),
            "policies": {v: _fin(out[v]) | {"moves": {
                k: {"n": m["n"], "delta": round(m["delta"], 0)}
                for k, m in sorted(moves[v].items(),
                                   key=lambda kv: kv[1]["delta"])}}
                for v in names},
            "note": "표본이 작다. 값을 고르는 용도가 아니라 '내 거래에 켰다면' 을 본다"}
