"""국면 판정 이력 — **지금 기록을 시작해야 40거래일 뒤에 판정할 수 있다.**

`night_bias` 는 외부(야간 리포트)가 하루 한 번 파일로 떨어뜨리는 예측이고,
`_effective_regime()` 이 그걸 기준선 삼아 인버스 ETF 매수를 막는다. 즉 **실제
매매 결정에 관여한다.** 그런데 지금 구조는 단일 JSON 파일 1레코드라 다음 날
덮어쓰이면 전날 예측이 사라진다 — **이력이 0건이고, 적중률을 소급 평가할
방법이 없다.**

## 대조군을 같은 레코드에 함께 적재하는 이유

"야간 리포트가 맞았는가" 만으로는 아무것도 결론 낼 수 없다. 시장이 오른 날이
많으면 '강세' 라고만 해도 맞는다. 판정하려면 **같은 것을 예측하는 결정론적
기준선**과 나란히 놓아야 한다. 다행히 둘이 이미 코드 안에 있고, 셋 다 같은
`강세/중립/약세` 도메인을 쓴다.

  night_bias    외부 야간 리포트 (미국장 등)     ← 검증 대상
  base_regime   전일 breadth (60일선 상회 비율)  ← 대조군 1, 결정론
  gap_bias      감시목록 시가갭 중앙값           ← 대조군 2, 결정론
  effective     위 셋의 합성 = 실제 적용된 값

같은 시각에 넷을 함께 남기면, 40거래일 뒤 "야간 리포트가 결정론 기준선을
이겼는가" 를 물을 수 있다. **이기지 못하면 `use_night_bias` 를 끄는 것이
정직한 결론**이고, 그 판정을 위해 오늘부터 쌓는다.

## 값이 바뀔 때만 쌓는다

엔진 루프는 30초마다 도는데 매번 적재하면 하루 1,000행이 된다. `base_regime`
은 10분 캐시, `gap_bias` 는 시가 확정 후 거의 고정이라 실제로는 하루 몇 번만
바뀐다. 네 값의 조합이 직전과 같으면 건너뛴다 — 이력의 정보량은 그대로다.
"""
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")
DB_PATH = Path(settings.DATA_DIR) / "regime_log.db"

REGIMES = ("강세", "중립", "약세")
# 방향 부호 — 적중 판정에 쓴다. 중립은 방향을 걸지 않은 것이라 별도 취급.
SIGN = {"강세": 1, "중립": 0, "약세": -1}


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS regime_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL, ts TEXT NOT NULL,
            night_bias TEXT, base_regime TEXT, gap_bias TEXT, effective TEXT
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_regime_date ON regime_log (date)")
    return conn


def record(night_bias: str, base_regime: str, gap_bias: str,
           effective: str, now: datetime | None = None) -> bool:
    """네 값을 append. 직전과 같으면 쓰지 않는다. 쓴 경우 True."""
    now = now or datetime.now(KST)
    date = now.date().isoformat()
    row = (night_bias, base_regime, gap_bias, effective)
    try:
        with _conn() as conn:
            last = conn.execute(
                "SELECT night_bias, base_regime, gap_bias, effective FROM regime_log"
                " WHERE date=? ORDER BY id DESC LIMIT 1", (date,)).fetchone()
            if last and tuple(last) == row:
                return False
            conn.execute(
                "INSERT INTO regime_log (date, ts, night_bias, base_regime,"
                " gap_bias, effective) VALUES (?,?,?,?,?,?)",
                (date, now.isoformat(timespec="seconds"), *row))
    except Exception:  # noqa: BLE001 - 기록 실패가 매매를 막으면 안 된다
        # sqlite3.Error 만 잡으면 디스크·권한 문제(OSError)가 그대로 올라가
        # _effective_regime() 을 터뜨리고, 그러면 신호 평가 전체가 멈춘다.
        # 이 이력은 연구용이다 — 어떤 실패도 매매보다 우선할 수 없다.
        log.exception("국면 이력 적재 실패")
        return False
    return True


def daily() -> list[dict]:
    """날짜별 **그날의 마지막 값** — 하루 한 판정으로 보는 관점.

    장중에 gap_bias 가 바뀌면 effective 도 바뀌지만, 적중률은 '그날 무엇으로
    끝났는가' 로 재는 것이 해석하기 쉽다. 원장에는 중간 변화도 다 남아 있다.
    """
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM regime_log WHERE id IN"
            " (SELECT MAX(id) FROM regime_log GROUP BY date) ORDER BY date")]


def entries(limit: int = 500) -> list[dict]:
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM regime_log ORDER BY id DESC LIMIT ?", (limit,))]


SIGNALS = ("night_bias", "base_regime", "gap_bias", "effective")
MIN_DAYS = 40      # 이보다 적으면 수치를 내놓지 않는다


def score(rows: list[dict], market: dict[str, float]) -> dict:
    """예측 × 실현 시장수익률 → 신호별 적중률.

    market: {날짜: 그날 시장 수익률%}. 호출자가 넘긴다(일봉 소유는 이쪽이 아니다).

    **중립은 적중 계산에서 뺀다** — 방향을 걸지 않은 것을 맞았다/틀렸다로
    세면 '항상 중립' 이 100%가 되거나 0%가 된다. 대신 `calls` 로 '몇 번이나
    방향을 걸었는지' 를 함께 낸다. 자주 거는 신호와 가끔 거는 신호는 적중률만
    비교하면 안 된다.
    """
    out = {}
    for sig in SIGNALS:
        hit = miss = neutral = 0
        for r in rows:
            ret = market.get(r["date"])
            if ret is None:
                continue
            s = SIGN.get(r.get(sig) or "중립", 0)
            if s == 0:
                neutral += 1
                continue
            if (s > 0 and ret > 0) or (s < 0 and ret < 0):
                hit += 1
            else:
                miss += 1
        calls = hit + miss
        out[sig] = {
            "calls": calls, "neutral": neutral,
            "hit_rate": round(hit / calls * 100, 1) if calls else None,
            # 방향을 건 날의 평균 수익률 부호까지 맞았는지와 별개로, 표본이
            # 얇으면 수치를 믿지 않는다
            "reliable": calls >= MIN_DAYS,
        }
    return out
