"""라인 추종 룰 타당성 검증 — 같은 진입에 청산 정책만 바꿔 재생한다.

## 무엇을 재는가

사용자 지침(2026-07-29)으로 들어간 `desk.update_lines` 가 **고정 손절·목표보다
나은가**를 축적된 1분봉으로 되돌려 잰다. 비교 대상은 셋이다.

    fixed   : 지금까지의 동작 — 진입 시 정한 손절·목표 고정
    trail   : 추종만 (오르면 같은 갭으로 따라 올리고, 내려도 유지)
    desk    : 추종 + 50% 좁히기 + 익절 상한 3% + 손절선 이동 시 시간손절 재계산
              (= 실제 배포된 규칙)

**청산 정책만 바꾼다.** 진입 신호·체결가·비용은 `backtest.runner` 와 같은 코드를
쓰고, 통계도 `runner.Result.stats` 를 그대로 쓴다. 그래야 차이가 정책에서 온
것임이 분명해진다.

## 모델링 — 어디서 낙관이 새는지 미리 못박는다

1분봉으로 초 단위 루프를 재생할 수는 없다. 세 가지를 **불리한 쪽**으로 고정했다.

- **라인 갱신은 봉 종가로만 한다.** 실제 데스크는 고가 부근에서도 갱신하지만,
  봉 안의 순서를 모르는 채 고가로 갱신하면 "고점에서 끌어올린 손절선에 같은 봉
  저가가 닿는" 불가능한 이익을 만든다. 종가 갱신은 추종을 **과소평가**한다.
- **청산 판정이 갱신보다 먼저다.** 같은 봉에서 손절과 목표가 함께 닿으면 손절을
  택한다(`runner` 규약 그대로).
- **틱이 아니라 분이다.** 데스크는 2초마다 보는데 여기서는 60초마다 본다.
  추종의 반응이 그만큼 늦다 — 이 역시 추종에 불리하다.

그래서 여기서 나온 추종의 성적은 **하한**으로 읽어야 한다. 반대로 추종이 여기서
지면 실제로도 질 가능성이 높다.

## 남는 한계

- 1분봉이 있는 종목만 본다(감시목록 중심 ~200종목). 유니버스 편향이 있다
- 표본 기간이 짧다(심층 백필 12일 내외). 국면 하나만 보고 있을 수 있다
- 진입 가능 여부가 정책마다 달라진다(청산이 빨라지면 다음 신호를 잡는다).
  그래서 **같은 진입만 짝지은 비교(`paired`)** 를 함께 낸다 — 이쪽이 정책 효과를
  더 깨끗하게 보여준다
"""
import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .. import settings
from ..backtest import runner
from ..data import store
from ..signals import rules
from ..trade import desk

log = logging.getLogger(__name__)

POLICIES = ("fixed", "trail", "desk")


@dataclass(frozen=True)
class Entry:
    """정책과 무관한 진입 사실. 셋 다 이 값에서 출발한다."""
    day: object
    rule: str
    side: str
    idx: int              # 진입 봉 위치
    ts: object
    price: float
    stop: float
    target: float


def _desk_cfg(policy: str) -> dict:
    """정책별 `desk.update_lines` 설정. `fixed` 는 훅을 아예 안 부른다."""
    if policy == "trail":
        # 추종만 — 좁히기는 닿을 수 없는 값으로, 상한은 0(비활성)으로 끈다
        return {"enabled": True, "trailing": True,
                "tighten_at": 99.0, "tighten_gap": 0.0, "max_gain_pct": 0.0}
    return {"enabled": True, "trailing": True}      # desk = 배포된 값 그대로


def _lines(policy: str, entry: Entry, stop: float, target: float,
           price: float) -> tuple[float, float]:
    """정책에 따라 갱신된 (손절, 목표). `fixed` 는 그대로 돌려준다."""
    if policy == "fixed":
        return stop, target
    pos = {"side": entry.side, "entry": entry.price,
           "stop": entry.stop, "target": entry.target,
           "stop_live": stop if stop != entry.stop else None,
           "target_live": target if target != entry.target else None}
    got = desk.update_lines(pos, price)
    if got is None:
        return stop, target
    new_stop = got[0] if got[0] is not None else entry.stop
    new_target = got[1] if got[1] is not None else entry.target
    return new_stop, new_target


def _entries_and_exits(symbol: str, df, rules_cfg: dict, policy: str,
                       sides: tuple[str, ...] | None) -> runner.Result:
    """`runner.run` 과 같은 진입 로직 + 정책별 청산."""
    slip = settings.COSTS.get("slippage_bp", 5) / 10000
    result = runner.Result()
    days_idx = df.index.normalize()
    hold_limit = rules_cfg.get("max_hold_min", 0) or 0

    for day, day_df in df.groupby(days_idx):
        prev = df[days_idx < day]
        prev_close = float(prev["close"].iloc[-1]) if not prev.empty else None
        fired: set[str] = set()
        bars = list(day_df.itertuples())
        open_trade = None
        stop = target = 0.0
        clock = None                      # 시간손절 기준점(손절선이 움직이면 갱신)

        for i in range(10, len(bars)):
            bar = bars[i]
            if open_trade is not None:
                t = open_trade
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
                    result.trades.append(t)
                    open_trade = None
                    continue
                # 청산이 없을 때만 라인을 갱신한다(판정이 갱신보다 먼저)
                new_stop, new_target = _lines(
                    policy, t._entry_fact, stop, target, float(bar.close))
                if new_stop != stop:
                    clock = bar.Index      # 손절선이 움직였다 → 시간손절 다시 센다
                stop, target = new_stop, new_target
                continue

            window = day_df.iloc[: i + 1]
            for sig in rules.evaluate_all(window, rules_cfg, prev_close):
                if sig.rule in fired or i + 1 >= len(bars):
                    continue
                if sides and sig.side not in sides:
                    continue
                fired.add(sig.rule)
                nxt = bars[i + 1]
                fill = nxt.open * (1 + slip if sig.side == "long" else 1 - slip)
                open_trade = runner.Trade(symbol, sig.rule, sig.side, nxt.Index,
                                          fill, sig.stop, sig.target)
                open_trade._entry_fact = Entry(day, sig.rule, sig.side, i + 1,
                                               nxt.Index, fill, sig.stop, sig.target)
                stop, target, clock = sig.stop, sig.target, nxt.Index
                break

        if open_trade is not None and bars:
            open_trade.exit = float(bars[-1].close)
            open_trade.exit_reason, open_trade.exit_ts = "eod", bars[-1].Index
            result.trades.append(open_trade)
    return result


def run_symbol(symbol: str, df, rules_cfg: dict | None = None,
               sides: tuple[str, ...] | None = ("long",)) -> dict:
    """한 종목을 세 정책으로 재생. 반환: {policy: Result}."""
    rules_cfg = rules_cfg or settings.RULES
    out = {}
    # 런타임 오버라이드(`data/desk.json`)는 무시한다. 실서버에서 데스크를 꺼둔
    # 채로 돌리면 세 정책이 전부 fixed 가 되어 비교가 성립하지 않는다.
    old_file, desk.STATE_FILE = desk.STATE_FILE, Path("/nonexistent/desk.json")
    old = (settings.CONFIG.get("execution", {}) or {}).get("desk")
    try:
        for p in POLICIES:
            settings.CONFIG.setdefault("execution", {})["desk"] = _desk_cfg(p)
            out[p] = _entries_and_exits(symbol, df, rules_cfg, p, sides)
    finally:
        desk.STATE_FILE = old_file
        if old is None:
            settings.CONFIG.get("execution", {}).pop("desk", None)
        else:
            settings.CONFIG["execution"]["desk"] = old
    return out


def _key(t) -> tuple:
    return (t.symbol, t.rule, t.side, t.entry_ts)


def _exit_mix(trades) -> dict:
    mix: dict[str, int] = {}
    for t in trades:
        mix[t.exit_reason] = mix.get(t.exit_reason, 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: -kv[1]))


def analyze(symbols: list[str], min_bars: int = 600, limit_days: int = 60,
            sides: tuple[str, ...] | None = ("long",)) -> dict:
    """전 종목을 세 정책으로 재생하고 비교표를 낸다.

    `paired` 는 **세 정책이 모두 같은 진입을 잡은 거래**만 모은 것이다. 청산이
    빨라지면 다음 신호를 잡을 수 있어 진입 자체가 달라지는데, 그 차이까지 섞이면
    "청산 정책의 효과" 를 읽을 수 없다.
    """
    costs = settings.COSTS
    risk = settings.RISK.get("risk_per_trade_pct", 0.5)
    per_policy = {p: [] for p in POLICIES}
    used, skipped = 0, 0

    for sym in symbols:
        df = store.load_bars(sym, "1m", limit=limit_days * 400)
        if df.empty or len(df) < min_bars:
            skipped += 1
            continue
        used += 1
        try:
            got = run_symbol(sym, df, sides=sides)
        except Exception:  # noqa: BLE001 - 한 종목 실패가 전체를 멈추지 않는다
            log.exception("재생 실패 %s", sym)
            skipped += 1
            continue
        for p in POLICIES:
            per_policy[p].extend(got[p].trades)

    # 세 정책이 공통으로 잡은 진입
    keysets = [{_key(t) for t in per_policy[p]} for p in POLICIES]
    common = set.intersection(*keysets) if keysets else set()

    def _stats(trades):
        r = runner.Result(trades=list(trades))
        return r.stats(costs, risk) | {"exits": _exit_mix(
            [t for t in trades if t.exit is not None])}

    return {
        "symbols_used": used, "symbols_skipped": skipped,
        "paired_trades": len(common),
        "all": {p: _stats(per_policy[p]) for p in POLICIES},
        "paired": {p: _stats([t for t in per_policy[p] if _key(t) in common])
                   for p in POLICIES},
        "params": {"tighten_at": desk._num("tighten_at"),
                   "tighten_gap": desk._num("tighten_gap"),
                   "max_gain_pct": desk._num("max_gain_pct"),
                   "max_hold_min": settings.RULES.get("max_hold_min", 0),
                   "sides": list(sides) if sides else None},
    }


def main() -> None:  # pragma: no cover - CLI
    import sys

    logging.basicConfig(level=logging.WARNING)
    syms = sys.argv[1:] or [s for s, _ in store.minute_symbols(min_days=3)]
    print(json.dumps(analyze(syms), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":  # pragma: no cover
    main()
