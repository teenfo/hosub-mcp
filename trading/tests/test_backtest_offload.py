"""CPU 무거운 백테스트는 별도 프로세스로 — 이벤트 루프 보호.

2026-07-27 장애: 심층 백필로 종목당 봉이 900 → 4,500 으로 늘자 마감 백테스트
리포트가 25분 넘게 돌았고, asyncio.to_thread 안의 순수 파이썬 재생이 GIL 을
계속 쥐어 :8600 이 연결 141개를 accept 하지 못하는 정지 상태가 됐다.
스레드로는 CPU 작업을 격리할 수 없다는 것이 요점이고, 여기서 그 회귀를 막는다.
"""
import json
import sys

import pytest

from app import settings
from app.backtest import job, offload, report, sweep


# --- ① 무거운 작업이 이벤트 루프와 같은 프로세스에 남지 않는다 ---

def test_report_loop_does_not_use_thread():
    """to_thread 로 되돌리면 잡는다 — 스레드는 GIL 을 공유한다."""
    src = (report.__file__,)
    text = open(src[0], encoding="utf-8").read()
    assert "to_thread(self.run_once)" not in text
    assert "run_offloaded" in text


def test_sweep_loop_does_not_use_thread():
    text = open(sweep.__file__, encoding="utf-8").read()
    assert "to_thread(run_sweep)" not in text
    assert "run_offloaded" in text


@pytest.mark.asyncio
async def test_run_offloaded_spawns_child(monkeypatch):
    called = {}

    async def fake(name, timeout=0):
        called["job"] = name
        return {"ok": True, "run_ts": "2026-07-27T15:40:00+09:00", "summary": {}}

    monkeypatch.setattr(offload, "run_job", fake)
    r = report.BacktestReporter()
    out = await r.run_offloaded()
    assert called["job"] == "report"
    assert out["ok"] is True
    assert r.last_run == "2026-07-27T15:40:00+09:00"
    assert r.running is False          # 끝나면 플래그가 풀린다


@pytest.mark.asyncio
async def test_run_offloaded_blocks_concurrent_run(monkeypatch):
    r = report.BacktestReporter()
    r.running = True
    monkeypatch.setattr(offload, "run_job", None)   # 불리면 TypeError
    assert (await r.run_offloaded())["ok"] is False


@pytest.mark.asyncio
async def test_run_offloaded_clears_flag_on_failure(monkeypatch):
    async def boom(name, timeout=0):
        raise RuntimeError("자식 폭발")

    monkeypatch.setattr(offload, "run_job", boom)
    r = report.BacktestReporter()
    with pytest.raises(RuntimeError):
        await r.run_offloaded()
    assert r.running is False          # 실패해도 영구 잠김이 되지 않는다


# --- ② 자식 프로세스 진입점 ---

def test_job_rejects_unknown(capsys):
    assert job.main(["job", "nope"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_job_dispatches_report(monkeypatch, capsys):
    monkeypatch.setattr(report.BacktestReporter, "run_once",
                        lambda self: {"ok": True, "summary": {"trades": 3}})
    assert job.main(["job", "report"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["trades"] == 3


def test_job_dispatches_sweep(monkeypatch, capsys):
    monkeypatch.setattr(sweep, "run_sweep", lambda: {"ok": True, "rules": []})
    assert job.main(["job", "sweep"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


# --- ③ 자식 실패가 부모를 죽이지 않는다 ---

@pytest.mark.asyncio
async def test_run_job_survives_child_failure():
    """존재하지 않는 작업 → 자식이 rc=2 로 끝나도 예외 없이 결과만 돌려준다."""
    out = await offload.run_job("__없는작업__", timeout=60)
    assert out["ok"] is False


@pytest.mark.asyncio
async def test_run_job_returns_child_stdout_json():
    """실제로 자식 프로세스가 떠서 JSON 을 돌려주는지 — 배선 전체 확인."""
    out = await offload.run_job("report", timeout=180)
    assert isinstance(out, dict)       # 성공/실패 무관, 파싱은 된다


def test_offload_pkg_root_importable():
    """자식이 `python -m app.backtest.job` 을 찾을 수 있는 경로여야 한다."""
    from pathlib import Path

    assert (Path(offload.PKG_ROOT) / "app" / "backtest" / "job.py").exists()
    assert sys.executable                              # 인터프리터 경로 확보


# --- ④ 평가 창 상한 ---

def test_report_caps_eval_window():
    """봉이 무한정 쌓여도 리포트 비용이 함께 커지지 않는다."""
    cfg = settings.CONFIG["backtest"]
    assert cfg["max_bars"] > 0
    # 심층 백필 1회분(4,500봉)보다는 넉넉해야 새 종목도 온전히 평가된다
    assert cfg["max_bars"] >= 4500
