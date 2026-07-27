"""발굴 소스 어댑터 — 각 기능이 신호를 에스컬레이션한다. 감시목록에는 쓰지 않는다."""
from .base import Source
from .intraday import GainersSource, PresurgeSource, VolumeSource
from .slow import ManualSource, NewsSource, NightlySource

# 등록 순서 = 화면 표시 순서. 엔진은 각자의 주기로 독립 실행한다.
ALL: tuple = (VolumeSource, GainersSource, PresurgeSource,
              NightlySource, NewsSource, ManualSource)

__all__ = ["ALL", "Source", "VolumeSource", "GainersSource", "PresurgeSource",
           "NightlySource", "NewsSource", "ManualSource"]
