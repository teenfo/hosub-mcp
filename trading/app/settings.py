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
        "account_masked": _mask(KIWOOM_ACCOUNT),
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

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    CONFIG: dict = yaml.safe_load(f)

WATCHLIST: dict[str, str] = {str(k): v for k, v in CONFIG.get("watchlist", {}).items()}
RISK: dict = CONFIG.get("risk", {})
RULES: dict = CONFIG.get("rules", {})
COSTS: dict = CONFIG.get("costs", {})
INVERSE_ETF: str = str(CONFIG.get("inverse_etf", ""))
