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

KIWOOM_ENV = os.environ.get("KIWOOM_ENV", "mock").lower()
KIWOOM_APP_KEY = os.environ.get("KIWOOM_APP_KEY", "")
KIWOOM_SECRET_KEY = os.environ.get("KIWOOM_SECRET_KEY", "")
KIWOOM_ACCOUNT = os.environ.get("KIWOOM_ACCOUNT", "")

REST_BASE = {
    "mock": "https://mockapi.kiwoom.com",
    "real": "https://api.kiwoom.com",
}[KIWOOM_ENV]
WS_BASE = {
    "mock": "wss://mockapi.kiwoom.com:10000/api/dostk/websocket",
    "real": "wss://api.kiwoom.com:10000/api/dostk/websocket",
}[KIWOOM_ENV]

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
