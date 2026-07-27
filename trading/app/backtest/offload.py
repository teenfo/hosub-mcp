"""CPU 무거운 백테스트를 **별도 프로세스**로 돌린다.

asyncio.to_thread 로는 부족하다. 백테스트 재생은 순수 파이썬 루프라 실행 내내
GIL 을 쥐고, 같은 프로세스의 이벤트 루프는 그동안 굶는다. 실제로 2026-07-27
심층 백필로 종목당 봉이 900 → 4,500 으로 늘자 마감 리포트가 25분 넘게 돌면서
:8600 이 연결 141개를 accept 하지 못하는 사실상의 정지 상태가 됐다.

별도 프로세스는 GIL 을 공유하지 않으므로 리포트가 몇 분을 돌아도 시세 수신·
주문 감시·API 응답이 멈추지 않는다. 결과는 자식이 SQLite 에 직접 쓰고,
요약만 stdout JSON 으로 돌려받는다.
"""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

# app 패키지의 부모 = trading/ (자식 프로세스의 sys.path 루트)
PKG_ROOT = str(Path(__file__).resolve().parents[2])
DEFAULT_TIMEOUT = 1800.0     # 30분 — 넘으면 자식을 죽이고 다음 주기에 재시도


async def run_job(job: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """`python -m app.backtest.job <job>` 를 띄우고 stdout JSON 을 돌려준다.

    실패해도 예외를 올리지 않고 {"ok": False, "error": ...} 로 돌려준다 —
    리포트 실패가 트레이딩 루프를 멈추게 할 이유는 없다.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.backtest.job", job,
            cwd=PKG_ROOT, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except Exception as e:  # noqa: BLE001
        log.exception("백테스트 자식 프로세스 기동 실패: %s", job)
        return {"ok": False, "error": f"기동 실패: {e}"}
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("백테스트 %s 시간 초과(%.0f초) — 자식 종료", job, timeout)
        return {"ok": False, "error": f"시간 초과({timeout:.0f}초)"}
    tail = (err or b"").decode("utf-8", "replace").strip()[-2000:]
    if proc.returncode != 0:
        log.error("백테스트 %s 실패(rc=%s): %s", job, proc.returncode, tail)
        return {"ok": False, "error": f"실행 실패(rc={proc.returncode})"}
    if tail:
        log.info("백테스트 %s 로그: %s", job, tail)
    try:
        return json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError:
        log.error("백테스트 %s 응답 파싱 실패", job)
        return {"ok": False, "error": "결과 파싱 실패"}
