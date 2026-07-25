"""환경변수(.env)와 config.yaml 을 읽어 TNM 전역 설정을 만든다.

trading/app/settings.py 와 같은 방식: .env 자체 로더(systemd EnvironmentFile 이
우선), config.yaml 은 튜닝값, 시크릿은 .env 에만 두고 save_keys 로 되쓰기(600).
"""
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

ENV_FILE = BASE_DIR / ".env"

# --- 시크릿 (.env 전용) ---
DART_API_KEY = os.environ.get("DART_API_KEY", "")
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID", "")          # 선택 — 비우면 네이버 어댑터 비활성
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET", "")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "")                    # Mac Studio (http://<ip>:11434)
OLLAMA_FALLBACK_URL = os.environ.get("OLLAMA_FALLBACK_URL", "")  # hosub 로컬 — 비우면 폴백 생략
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL = os.environ.get("SLACK_CHANNEL", "")
DB_DSN = os.environ.get("TNM_DB_DSN", "")                        # postgresql://tnm:pw@127.0.0.1:5433/tnm
# 대시보드 프록시 공유 시크릿 (dash 의 HOSUB_TNM_TOKEN 과 동일 값)
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN", "")
# 트레이딩 서비스 연동 (감시목록·보유계좌 소비 — 키움 직접 호출 안 함)
TRADING_URL = os.environ.get("TRADING_URL", "http://127.0.0.1:8600")
TRADING_TOKEN = os.environ.get("TRADING_TOKEN", "")

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

with open(BASE_DIR / "config.yaml", encoding="utf-8") as f:
    CONFIG: dict = yaml.safe_load(f)

WATCH: dict = CONFIG.get("watch", {})
COLLECT: dict = CONFIG.get("collect", {})
DEDUP: dict = CONFIG.get("dedup", {})
LLM: dict = CONFIG.get("llm", {})
SCORE: dict = CONFIG.get("score", {})
ALERTS: dict = CONFIG.get("alerts", {})

_SECRET_KEYS = (
    "DART_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET",
    "OLLAMA_URL", "OLLAMA_FALLBACK_URL", "SLACK_BOT_TOKEN", "SLACK_CHANNEL",
)


def masked() -> dict:
    """설정 화면용 현황 — 시크릿 원문은 절대 반환하지 않는다."""
    def _mask(v: str) -> str:
        if not v:
            return ""
        return v[:4] + "…" + v[-4:] if len(v) >= 10 else "설정됨"

    return {
        "dart_key": _mask(DART_API_KEY),
        "naver_enabled": bool(NAVER_CLIENT_ID and NAVER_CLIENT_SECRET),
        "ollama_url": OLLAMA_URL,            # URL 은 시크릿 아님 (LAN 주소)
        "ollama_fallback_url": OLLAMA_FALLBACK_URL,
        "slack_token": _mask(SLACK_BOT_TOKEN),
        "slack_channel": SLACK_CHANNEL,
        "db_configured": bool(DB_DSN),
        "shadow_mode": bool(ALERTS.get("shadow_mode", True)),
    }


def apply_keys(**kv: str | None) -> None:
    """런타임 전역 갱신. None/빈값은 '변경 없음'."""
    for name, val in kv.items():
        if not val:
            continue
        key = name.upper()
        if key not in _SECRET_KEYS:
            raise ValueError(f"허용되지 않은 설정 키: {name}")
        val = val.strip()
        # 개행 포함 값은 .env 에 별도 라인으로 기록되어 허용목록 밖 키를
        # 주입할 수 있으므로 거부한다.
        if any(c in val for c in "\r\n"):
            raise ValueError(f"값에 개행을 포함할 수 없습니다: {name}")
        globals()[key] = val


def save_keys(**kv: str | None) -> None:
    """런타임 적용 + .env 영속화(기존 항목 유지, 권한 600)."""
    apply_keys(**kv)
    existing: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()
    existing.update({k: globals()[k] for k in _SECRET_KEYS})
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in existing.items()))
    ENV_FILE.chmod(0o600)
