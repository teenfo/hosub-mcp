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

## intraday(장중 방향) 관측 — 판정 미사용, 원장만 (2026-08-04 편입)

`intraday_bias`/`intraday_med` 는 감시목록 '시가 대비 현재가' 등락률 중앙값
(임계는 gap 과 동일 ±0.5%)이다. **유효국면 합성에 넣지 않는다** — 편입 계기는
2026-08-04 실측: 개장 갭업→하락 전환일에 유효국면이 아침 강세(night_bias)에
갇혀 인버스 신호 3건이 차단됐다(09:29 114800 1,137 → 10:00 고점 +3.0%).
장중 전환을 반영할 입력이 없다는 구조적 공백의 첫 비용 실측이다. 반대 방향
(휩쏘일에 전환을 따라가 손해)의 비용은 아직 못 쟀으므로 게이트에 넣지 않는다.

**판정 기준 (사전 확정 — 결과를 본 뒤 바꾸면 측정 폐기), 2026-09-01경:**
- 표본: 장중 관측이 아침 유효국면과 **어긋난 전환일** ≥ 15일. 미달이면 연기 보고.
- 반사실 평가: 전환 감지 시각 이후 (a) 차단됐던 인버스 신호를 허용했을 때의
  브래킷 실현 R 합 − (b) 전환이 거짓이었던 날(종가가 아침 방향으로 회귀)
  허용분의 손실 합. 합이 양수이고 t≥2 면 게이트 입력 승격을 **제안**(적용은
  사용자 결정), 아니면 관측 폐기·measurement.md 기록.
- dedup 은 intraday_bias 까지 포함 5값 기준 — 중앙값(원시)은 전이 시점 값만
  남는다. 경계 진동 시 하루 수십 행까지 가능하나 관측 전용이라 허용.
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
    # 장중 방향 관측(2026-08-04) — 기존 컬럼 의미 불변, 추가만(연속성 규약).
    for ddl in ("ALTER TABLE regime_log ADD COLUMN intraday_bias TEXT",
                "ALTER TABLE regime_log ADD COLUMN intraday_med REAL"):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass                      # 이미 추가됨
    return conn


def record(night_bias: str, base_regime: str, gap_bias: str,
           effective: str, now: datetime | None = None,
           intraday_bias: str | None = None,
           intraday_med: float | None = None) -> bool:
    """값을 append. 직전과 같으면 쓰지 않는다. 쓴 경우 True.

    dedup 은 intraday_bias 까지 포함한 5값 기준 — 장중 방향 전이도 이력에
    남아야 반사실 평가(전환 감지 시각)가 가능하다. intraday_med(원시 %)는
    비교에 넣지 않는다: 매 사이클 미세하게 변해 dedup 이 무력화된다.
    """
    now = now or datetime.now(KST)
    date = now.date().isoformat()
    row = (night_bias, base_regime, gap_bias, effective, intraday_bias)
    try:
        with _conn() as conn:
            last = conn.execute(
                "SELECT night_bias, base_regime, gap_bias, effective,"
                " intraday_bias FROM regime_log"
                " WHERE date=? ORDER BY id DESC LIMIT 1", (date,)).fetchone()
            if last and tuple(last) == row:
                return False
            conn.execute(
                "INSERT INTO regime_log (date, ts, night_bias, base_regime,"
                " gap_bias, effective, intraday_bias, intraday_med)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (date, now.isoformat(timespec="seconds"), *row, intraday_med))
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


def daily_first() -> list[dict]:
    """날짜별 **그날의 첫 판정** — '개장 전에 무엇을 예측했는가' 관점.

    `daily()`(마지막 값)는 gap_bias 가 당일 시가로 갱신된 뒤라, effective 를
    그 값으로 채점하면 **당일 정보로 당일을 맞히는 오염**이 생긴다. 예측력
    채점은 이쪽이 정직하다(night_bias·전일 breadth 는 어차피 사전 정보라 양쪽
    이 같다). 두 관점을 나란히 노출하고 9/29 판정은 사전 확정대로 daily()
    기준을 유지한다 — 기준을 바꾸는 게 아니라 관점을 추가하는 것이다.
    """
    with _conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM regime_log WHERE id IN"
            " (SELECT MIN(id) FROM regime_log GROUP BY date) ORDER BY date")]


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
