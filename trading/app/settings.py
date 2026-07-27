"""환경변수(.env)와 config.yaml 을 읽어 앱 전역 설정을 만든다."""
import os
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(BASE_DIR / ".env")

_REST_BASES = {
    "mock": "https://mockapi.kiwoom.com",
    "real": "https://api.kiwoom.com",
}
_WS_BASES = {
    "mock": "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
    "real": "wss://api.kiwoom.com:10000/api/dostk/websocket",
}

KIWOOM_ENV = os.environ.get("KIWOOM_ENV", "mock").lower()
if KIWOOM_ENV not in _REST_BASES:
    KIWOOM_ENV = "mock"
KIWOOM_APP_KEY = os.environ.get("KIWOOM_APP_KEY", "")
KIWOOM_SECRET_KEY = os.environ.get("KIWOOM_SECRET_KEY", "")
KIWOOM_ACCOUNT = os.environ.get("KIWOOM_ACCOUNT", "")

REST_BASE = _REST_BASES[KIWOOM_ENV]
WS_BASE = _WS_BASES[KIWOOM_ENV]

ENV_FILE = BASE_DIR / ".env"


def masked() -> dict:
    """화면 표시용 현황. 시크릿 원문은 절대 반환하지 않는다."""
    def _mask(v: str) -> str:
        if not v:
            return ""
        return v[:4] + "…" + v[-4:] if len(v) >= 10 else "설정됨"

    return {
        "env": KIWOOM_ENV,
        "app_key_masked": _mask(KIWOOM_APP_KEY),
        "has_secret": bool(KIWOOM_SECRET_KEY),
        "account": KIWOOM_ACCOUNT,          # 계좌번호는 마스킹하지 않음(사용자 요청)
        "account_masked": KIWOOM_ACCOUNT,   # 하위호환
    }


def apply_keys(env: str | None = None, app_key: str | None = None,
               secret_key: str | None = None, account: str | None = None) -> None:
    """런타임 전역을 갱신한다. None/빈값은 '변경 없음'."""
    global KIWOOM_ENV, KIWOOM_APP_KEY, KIWOOM_SECRET_KEY, KIWOOM_ACCOUNT
    global REST_BASE, WS_BASE
    if env:
        env = env.lower()
        if env not in _REST_BASES:
            raise ValueError(f"env 는 mock/real 만 가능: {env}")
        KIWOOM_ENV = env
        REST_BASE = _REST_BASES[env]
        WS_BASE = _WS_BASES[env]
    if app_key:
        KIWOOM_APP_KEY = app_key.strip()
    if secret_key:
        KIWOOM_SECRET_KEY = secret_key.strip()
    if account:
        KIWOOM_ACCOUNT = account.strip()


def save_keys(env: str | None = None, app_key: str | None = None,
              secret_key: str | None = None, account: str | None = None) -> None:
    """런타임 적용 + .env 영속화(기존 항목 유지, 권한 600)."""
    apply_keys(env=env, app_key=app_key, secret_key=secret_key, account=account)
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update(
        {
            "KIWOOM_ENV": KIWOOM_ENV,
            "KIWOOM_APP_KEY": KIWOOM_APP_KEY,
            "KIWOOM_SECRET_KEY": KIWOOM_SECRET_KEY,
            "KIWOOM_ACCOUNT": KIWOOM_ACCOUNT,
        }
    )
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in existing.items()))
    ENV_FILE.chmod(0o600)

DASH_PASSWORD = os.environ.get("DASH_PASSWORD", "change-me")
SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-secret")
# hosub-mcp 대시보드 프록시용 공유 시크릿 (HOSUB_TRADING_TOKEN 과 동일 값 설정)
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")

# TNM(뉴스·공시) 호출용 — **trading 자신의 INTERNAL_TOKEN 과 다른 값이다.**
# 서비스마다 토큰이 따로라 자기 것을 그대로 보내면 401 이 온다(실측 2026-07-27:
# 발굴 엔진 news 어댑터 첫 배포에서 그랬다). TNM 쪽 INTERNAL_TOKEN 을 넣는다.
# TNM 이 trading 을 부를 때 쓰는 TRADING_TOKEN 의 반대 방향이다.
TNM_URL = os.environ.get("TNM_URL", "http://127.0.0.1:8602")
TNM_TOKEN = os.environ.get("TNM_TOKEN", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    CONFIG: dict = yaml.safe_load(f)

WATCHLIST: dict[str, str] = {str(k): v for k, v in CONFIG.get("watchlist", {}).items()}
# 수집전용 종목코드 집합 — 감시목록에 있으나 매매(신호·주문)는 하지 않고 데이터만
# 모은다. 감시목록 DB(collect_only=1)에서 런타임으로 재구성된다(watchlist._rebuild_runtime).
COLLECT_ONLY: set[str] = set()
RISK: dict = CONFIG.get("risk", {})
RULES: dict = CONFIG.get("rules", {})
COSTS: dict = CONFIG.get("costs", {})
INVERSE_ETF: str = str(CONFIG.get("inverse_etf", ""))

# 런타임에 UI 로 바꾸는 리스크 목표는 DATA_DIR 에 영속화(재시작·재배포 후에도 유지).
RISK_FILE = DATA_DIR / "risk.json"


def _load_risk_overrides() -> None:
    import json
    if RISK_FILE.exists():
        try:
            RISK.update(json.loads(RISK_FILE.read_text()))
        except (OSError, ValueError):
            pass


# 런타임 자산 — 엔진이 잔고 동기화 때 갱신한다(설정이 아니라 관측값).
# 감시목록 편입 시 '1주라도 살 수 있는 종목인가'를 판단하는 데 쓴다.
EQUITY: float = 0.0


def set_equity(value: float) -> None:
    global EQUITY
    EQUITY = max(0.0, float(value or 0))


def tradable_price_cap(fallback: float = 0.0) -> float:
    """매매 tier 로 넣을 수 있는 최고 주가.

    한 종목에 넣을 수 있는 최대 금액(자산 × 종목당 비중 상한)이 곧 '1주 가격의
    상한'이다. 고정값으로 두면 계좌가 커져도 후보가 늘지 않고, 반대로 계좌가
    작을 때는 살 수도 없는 종목이 매매 대상으로 들어온다.
    자산 미동기화(0) 면 fallback(=config 고정값)을 돌려준다.
    """
    if EQUITY <= 0:
        return fallback
    try:
        w = float(RISK.get("max_position_weight_pct", 0) or 0)
    except (TypeError, ValueError):
        w = 0.0
    cap = EQUITY * w / 100 if 0 < w <= 100 else EQUITY
    return max(cap, 0.0) or fallback


SCAN_INTERVAL_MIN_SEC = 10       # 키움 레이트리밋 보호 하한
SCAN_INTERVAL_MAX_SEC = 300


def save_risk(daily_target_pct=None, daily_loss_limit_pct=None,
              risk_per_trade_pct=None, auto_approve=None,
              scan_interval_sec=None, max_position_weight_pct=None,
              confirm_on_close=None, max_positions=None, long_only=None,
              signal_ttl_min=None) -> None:
    """UI(기어 모달)에서 바꾸는 매매 설정을 risk.json 에 영속화한다.
    config.yaml 의 risk 블록이 기본값, 여기 저장분이 오버라이드."""
    import json
    ov: dict = {}
    if RISK_FILE.exists():
        try:
            ov = json.loads(RISK_FILE.read_text())
        except (OSError, ValueError):
            ov = {}
    for key, val in (("daily_target_pct", daily_target_pct),
                     ("daily_loss_limit_pct", daily_loss_limit_pct),
                     ("risk_per_trade_pct", risk_per_trade_pct)):
        if val is None:
            continue
        f = float(val)
        if not 0 <= f <= 50:
            raise ValueError(f"{key} 는 0~50% 범위여야 합니다")
        ov[key] = f
        RISK[key] = f
    if max_position_weight_pct is not None:
        # 0 = 상한 없음(종전 동작). 한 종목이 계좌의 몇 %까지 차지할 수 있는가.
        f = float(max_position_weight_pct)
        if not 0 <= f <= 100:
            raise ValueError("종목당 비중 상한은 0~100% 범위여야 합니다")
        ov["max_position_weight_pct"] = f
        RISK["max_position_weight_pct"] = f
    if max_positions is not None:
        n = int(max_positions)
        if not 1 <= n <= 20:
            raise ValueError("최대 동시 포지션은 1~20 범위여야 합니다")
        ov["max_positions"] = n
        RISK["max_positions"] = n
    if signal_ttl_min is not None:
        n = int(signal_ttl_min)
        if not 1 <= n <= 240:
            raise ValueError("신호 만료는 1~240분 범위여야 합니다")
        ov["signal_ttl_min"] = n
        RISK["signal_ttl_min"] = n
    for key, flag in (("confirm_on_close", confirm_on_close),
                      ("auto_approve", auto_approve),
                      ("long_only", long_only)):
        if flag is not None:
            ov[key] = bool(flag)
            RISK[key] = bool(flag)
    if scan_interval_sec is not None:
        n = int(scan_interval_sec)
        if not SCAN_INTERVAL_MIN_SEC <= n <= SCAN_INTERVAL_MAX_SEC:
            raise ValueError(
                f"감시 주기는 {SCAN_INTERVAL_MIN_SEC}~{SCAN_INTERVAL_MAX_SEC}초여야 합니다")
        ov["scan_interval_sec"] = n
        RISK["scan_interval_sec"] = n
    RISK_FILE.write_text(json.dumps(ov, ensure_ascii=False))


# 규칙(기법) 활성 여부 override — UI 토글로 바꾸면 rules.json 에 영속화되어
# 재시작·재배포 후에도 유지된다. config.yaml 의 enabled 는 기본값 역할.
RULES_FILE = DATA_DIR / "rules.json"

# 오버라이드가 덮기 전의 config 원값. _load_rules_overrides 가 RULES 를 제자리
# 수정하므로, 스냅샷을 떠 두지 않으면 '설정 기본값'이 영영 사라진다.
# 화면에서 기본값과 현재값이 어긋난 것을 보여 주려면 이게 있어야 한다 —
# 실측 2026-07-27: config 가 false 로 둔 6개를 rules.json 이 전부 켜 놓았는데
# 어디에도 그 사실이 드러나지 않았고, 그날 신호 17건 중 15건이 그 규칙들에서 나왔다.
RULES_CONFIG_ENABLED: dict[str, bool] = {
    n: bool(c.get("enabled")) for n, c in RULES.items()
    if isinstance(c, dict) and "enabled" in c
}


def _load_rules_overrides() -> None:
    import json
    if RULES_FILE.exists():
        try:
            ov = json.loads(RULES_FILE.read_text())
        except (OSError, ValueError):
            return
        for name, patch in ov.items():
            if name in RULES and isinstance(patch, dict) and "enabled" in patch:
                RULES[name]["enabled"] = bool(patch["enabled"])


def save_rule_enabled(name: str, enabled: bool) -> None:
    """기법 활성 여부를 갱신하고 rules.json 에 영속화."""
    import json
    if name not in RULES:
        raise ValueError(f"알 수 없는 규칙: {name}")
    ov: dict = {}
    if RULES_FILE.exists():
        try:
            ov = json.loads(RULES_FILE.read_text())
        except (OSError, ValueError):
            ov = {}
    ov.setdefault(name, {})["enabled"] = bool(enabled)
    RULES[name]["enabled"] = bool(enabled)
    RULES_FILE.write_text(json.dumps(ov, ensure_ascii=False))


_load_risk_overrides()
_load_rules_overrides()
