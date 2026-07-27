"""모델 카탈로그 — 대시보드의 "모델 찾기" 목록.

ollama.com 을 스크레이핑하지 않는다. 공개 검색 API 가 없어 HTML 파싱에 기대야
하고, 그러면 게이트웨이가 남의 사이트 개편에 끌려 죽는다. 대신 큐레이션한
`config/catalog.yaml` 을 읽고, 목록에 없는 모델은 사용자가 이름을 직접 입력해
설치한다(같은 이름 검증·크기 게이트를 거친다).

파일은 compose 가 `:ro` 로 바인드하는 `config/` 안에 있다 — `git pull` 만으로
갱신되고 이미지 재빌드가 필요 없다.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("llmgw.catalog")


class Catalog:
    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else None
        self._models: list[dict] = []
        self._mtime: float | None = None
        self._error: str | None = None

    def _load(self) -> None:
        """mtime 이 바뀌었을 때만 다시 읽는다."""
        if self._path is None or not self._path.is_file():
            self._models, self._mtime = [], None
            return
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            return
        if self._mtime == mtime:
            return
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            models = self._parse(raw)
        except Exception as exc:
            # 카탈로그가 깨져도 /v1/status·설치 화면이 죽으면 안 된다.
            # 직전 정상본을 그대로 쓰고 로그와 error 필드로만 알린다.
            self._error = f"{type(exc).__name__}: {exc}"
            log.warning("카탈로그 파싱 실패 — 직전 값 유지: %s", self._error)
            self._mtime = mtime
            return
        self._models, self._mtime, self._error = models, mtime, None

    @staticmethod
    def _parse(raw: dict) -> list[dict]:
        out = []
        for m in (raw.get("models") or []):
            if not isinstance(m, dict) or not m.get("name"):
                continue
            tags = []
            for t in (m.get("tags") or []):
                if not isinstance(t, dict) or not t.get("tag"):
                    continue
                tags.append({
                    "tag": str(t["tag"]),
                    "size_gb": float(t["size_gb"]) if t.get("size_gb") else None,
                    "context": int(t["context"]) if t.get("context") else None,
                })
            if not tags:
                continue
            out.append({
                "name": str(m["name"]),
                "family": str(m.get("family") or ""),
                "kinds": [str(k) for k in (m.get("kinds") or ["generate"])],
                "description": str(m.get("description") or ""),
                "tags": tags,
            })
        return out

    def models(self) -> list[dict]:
        self._load()
        return list(self._models)

    def sizes(self) -> dict[str, float]:
        """태그 → 크기(GB). RoleConfig.set_catalog_sizes 에 넘긴다."""
        return {t["tag"]: t["size_gb"]
                for m in self.models() for t in m["tags"] if t["size_gb"]}

    def search(self, query: str = "", *, kind: str | None = None) -> list[dict]:
        q = (query or "").strip().lower()
        out = []
        for m in self.models():
            if kind and kind not in m["kinds"]:
                continue
            haystack = " ".join([m["name"], m["family"], m["description"],
                                 *(t["tag"] for t in m["tags"])]).lower()
            if q and q not in haystack:
                continue
            out.append(m)
        return out

    @property
    def error(self) -> str | None:
        return self._error
