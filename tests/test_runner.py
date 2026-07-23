"""SubprocessRunner 실동작 검증 — 특히 타임아웃 시 무한 블로킹 회귀 방지."""

from __future__ import annotations

import time

from src.runner import SubprocessRunner


def test_basic_success():
    r = SubprocessRunner().run(["bash", "-lc", "echo hi"], timeout=5, shell=True)
    assert r.ok and r.stdout.strip() == "hi"


def test_missing_binary():
    r = SubprocessRunner().run(["/no/such/bin"], timeout=5)
    assert r.exit_code == 127 and not r.ok


def test_timeout_returns_promptly():
    start = time.monotonic()
    r = SubprocessRunner().run(["bash", "-lc", "sleep 30"], timeout=1, shell=True)
    elapsed = time.monotonic() - start
    assert r.timed_out
    assert elapsed < 10  # 1s 타임아웃 + 5s 유예 안에 반환


def test_timeout_with_escaped_daemon_does_not_hang():
    """setsid 로 프로세스 그룹을 탈출한 자식이 파이프를 쥐고 있어도
    무한 대기 없이 반환해야 한다 (run_command 교착 회귀 방지)."""
    start = time.monotonic()
    # setsid sleep 은 새 세션으로 탈출 → killpg 로 안 죽고 stdout 파이프를 쥔다.
    # bash 는 즉시 종료하지만 파이프 EOF 가 오지 않아 communicate 가 막힌다.
    r = SubprocessRunner().run(
        ["bash", "-lc", "setsid sleep 30 & echo started"], timeout=2, shell=True
    )
    elapsed = time.monotonic() - start
    # 수정 전: 손자 sleep 이 끝나는 ~30s(또는 영원히)까지 블로킹.
    # 수정 후: 2s 타임아웃 + 5s 유예 = ~7s 안에 반환.
    assert elapsed < 15, f"communicate 가 여전히 블로킹됨 ({elapsed:.1f}s)"
