"""사전 등록 판정의 입력 스냅샷 — 원장 보관 기간보다 오래 남는 판정 근거 (2026-11-02).

## 왜

원장마다 보관 기간이 있다 — scout `signals` 60일, `datasets/*/features.csv` 30일.
4주 넘게 지난 판정을 '누적 표본' 으로 다시 하려 하면 원 데이터가 이미 없다.
실측 2026-10-26 flow 재판정: 8/3~ 원 표본을 다시 계산할 수 없어 8/31 공개 요약치
(평균·t·n)로 합산했다 — sd 를 평균·t·n 에서 역산해야 했고 t 반올림 오차가 섞였다.
판정을 돌릴 때 **쌍·관측 단위 입력**을 여기 남기면 다음 재판정이 원 데이터로 다시
계산할 수 있다. 보관 기간을 늘리는 것보다 싸다(판정 하나에 수백 KB).

## 규약

- 파일: `DATA_DIR/judgments/<YYYY-MM-DD>_<name>.json`
  `{"name", "day", "saved_at", "criteria", "rows", "summary"}`
- `rows` 는 쌍·관측 단위(예: `{"code", "day", "diff_r": {...}}`) — 요약치만 남기지
  않는다. 요약치는 `summary` 에 따로.
- `criteria` 는 결과를 보기 전에 확정한 판정 기준 문장 — 나중에 '무엇으로 판정했나' 를
  파일 하나로 재현할 수 있게.
- 같은 날·같은 이름이면 덮어쓴다(재실행 멱등). 지우는 코드는 두지 않는다 — 판정 근거는
  정리 대상이 아니다.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import settings

KST = ZoneInfo("Asia/Seoul")
DIR = Path(settings.DATA_DIR) / "judgments"
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def _path(name: str, day: str) -> Path:
    if not _NAME.match(name or ""):
        raise ValueError(f"판정 이름은 소문자·숫자·_- 만: {name!r}")
    datetime.strptime(day, "%Y-%m-%d")           # 형식 검증
    return DIR / f"{day}_{name}.json"


def save(name: str, rows: list[dict], *, criteria: str = "",
         summary: dict | None = None, day: str | None = None) -> Path:
    """판정 입력을 남긴다. 반환: 쓴 파일 경로."""
    now = datetime.now(KST)
    day = day or now.date().isoformat()
    path = _path(name, day)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"name": name, "day": day,
               "saved_at": now.isoformat(timespec="seconds"),
               "criteria": criteria, "rows": rows, "summary": summary or {}}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(path)                            # 반쯤 쓴 파일이 남지 않게
    return path


def load(name: str, day: str) -> dict | None:
    path = _path(name, day)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def history(name: str) -> list[dict]:
    """그 판정의 모든 스냅샷 — 날짜 오름차순. 누적 재판정의 입력이다."""
    _path(name, "2000-01-01")                    # 이름 검증
    out = []
    for p in sorted(DIR.glob(f"*_{name}.json")):
        if p.name[:10] and p.name[10:11] == "_" and p.name[11:-5] == name:
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
    return out
