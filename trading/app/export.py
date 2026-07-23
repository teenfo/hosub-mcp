"""전종목 피처 데이터셋을 파일로 내보낸다 — 외부 스케줄러/분석기가 소비.

산출물 (DATA_DIR/datasets/):
  <date>/features.csv   종목 1행 × 피처 열 (전종목, 유동성 무관 — 분석기가 자유 필터)
  latest.json           최신 데이터셋 매니페스트 (경로·행수·열 설명·기준일)
스케줄러는 latest.json 을 읽어 features.csv 경로를 알아내고 분석한다.
CSV 는 추가 의존성 없이 pandas 로 어디서나 읽힌다.
"""
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from . import settings

log = logging.getLogger(__name__)
DATASET_DIR = Path(settings.DATA_DIR) / "datasets"

# 스케줄러가 열의 의미를 알 수 있도록 매니페스트에 싣는 설명
COLUMN_DOC = {
    "code": "종목코드(6자리)", "name": "종목명",
    "close": "종가(원)", "change_pct": "전일대비 등락률(%)",
    "volume": "거래량(주)", "vol_ratio20": "거래량/20일평균 배수",
    "trade_value": "거래대금(원, 종가×거래량)",
    "near_high20_pct": "종가/20일최고가(%)", "near_high60_pct": "종가/60일최고가(%)",
    "off_low60_pct": "60일최저가 대비 상승률(%)",
    "ma5": "5일 이평", "ma20": "20일 이평", "ma60": "60일 이평",
    "ma_aligned": "정배열(5>20>60) 여부(1/0)",
    "ma_aligned_new": "정배열 최근5일내 신규형성(1/0)",
    "ret_5d": "5일 수익률(%)", "ret_20d": "20일 수익률(%)", "ret_60d": "60일 수익률(%)",
    "rsi14": "RSI(14)", "liquid": "가격·거래대금 게이트 통과(1/0)",
    "score": "발굴 3규칙 충족 수(0~3)",
}
_ORDER = list(COLUMN_DOC.keys())


def write_dataset(date: str, rows: list[dict]) -> dict:
    """피처 행 목록 → <date>/features.csv + latest.json. 반환: 매니페스트."""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    day_dir = DATASET_DIR / date
    day_dir.mkdir(exist_ok=True)
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.drop(columns=["reasons"], errors="ignore")
        cols = [c for c in _ORDER if c in df.columns]
        df = df[cols].sort_values("trade_value", ascending=False)
    features_path = day_dir / "features.csv"
    df.to_csv(features_path, index=False, encoding="utf-8-sig")

    manifest = {
        "date": date,
        "generated_at": datetime.now(UTC).isoformat(),
        "features_file": str(features_path),
        "symbol_count": int(len(df)),
        "columns": COLUMN_DOC,
        "note": "전종목 피처 스냅샷. liquid=1 로 필터 후 분석 권장. "
        "발굴 3규칙(score)은 참고용이며, 분석기가 자유롭게 재랭킹할 수 있다.",
    }
    (DATASET_DIR / "latest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _prune(settings.CONFIG.get("export", {}).get("keep_days", 30))
    log.info("데이터셋 내보내기: %s (%d 종목)", features_path, len(df))
    return manifest


def latest_manifest() -> dict | None:
    p = DATASET_DIR / "latest.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _prune(keep_days: int) -> None:
    if keep_days <= 0 or not DATASET_DIR.exists():
        return
    days = sorted(
        [d for d in DATASET_DIR.iterdir() if d.is_dir() and len(d.name) == 10],
        reverse=True,
    )
    for d in days[keep_days:]:
        for f in d.iterdir():
            f.unlink(missing_ok=True)
        d.rmdir()
