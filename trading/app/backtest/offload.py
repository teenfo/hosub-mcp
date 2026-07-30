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
# 60분. 리포트는 코어를 나눠 쓰지만(report._run_universe) 봉은 keep_days 만큼
# 계속 쌓이므로 여유를 둔다. 30분이던 시절 실측 87분이 걸려 매번 죽었다 —
# 그리고 죽는 데 30분을 쓰고도 결과가 0이었다.
DEFAULT_TIMEOUT = 3600.0     # 넘으면 자식을 죽이고 다음 주기에 재시도

# 잡마다 비용이 다르다. 하나의 상한을 공유하면 **격자가 늘어난 잡이 조용히
# 죽는다** — 실측 2026-07-30 16:13~17:13: 추종 스윕이 정확히 3600초에 잘려
# 결과가 0이었다. 7/29 에는 9변종이었는데 `at`·`tgt` 축이 붙어 17변종이 됐고,
# 하필 마감 리포트가 16:41까지 코어를 나눠 쓰고 있었다. 두 배가 된 작업을
# 그대로 둔 채 상한만 유지한 것이 잘못이다.
#
# 상한은 '이보다 오래 걸리면 무언가 잘못됐다' 는 뜻이어야지, 정상 작업을
# 자르는 값이면 안 된다.
JOB_TIMEOUT = {"trailing": 9000.0}       # 2.5시간


def timeout_for(job: str) -> float:
    return JOB_TIMEOUT.get(job, DEFAULT_TIMEOUT)


async def _drain(stream, job: str) -> str:
    """자식 stderr 를 **한 줄씩 흘려보내며** 로그로 남기고 꼬리를 돌려준다.

    종전에는 `proc.communicate()` 로 한 번에 받았다 — 프로세스가 끝나야 도착하므로
    진행 로그를 아무리 찍어도 **끝나기 전에는 볼 수 없었다.** 실측 2026-07-30:
    한 시간을 폴링하고도 어디까지 갔는지 몰랐고, 타임아웃에 잘린 뒤에야 알았다.
    """
    tail: list[str] = []
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip()
        if not line:
            continue
        tail.append(line)
        del tail[:-40]                       # 꼬리만 들고 있는다(메모리 상한)
        log.info("백테스트 %s: %s", job, line)
    return "\n".join(tail)[-2000:]


async def _collect(proc, job: str) -> tuple[bytes, str]:
    """stdout·stderr 를 **동시에** 읽는다. 한쪽만 기다리면 다른 파이프가 차서 막힌다."""
    out, tail = await asyncio.gather(proc.stdout.read(), _drain(proc.stderr, job))
    await proc.wait()
    return out, tail


async def run_job(job: str, timeout: float | None = None) -> dict:
    """`python -m app.backtest.job <job>` 를 띄우고 stdout JSON 을 돌려준다.

    실패해도 예외를 올리지 않고 {"ok": False, "error": ...} 로 돌려준다 —
    리포트 실패가 트레이딩 루프를 멈추게 할 이유는 없다.
    """
    timeout = timeout_for(job) if timeout is None else timeout
    env = dict(os.environ)
    env["PYTHONPATH"] = PKG_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    # 자식 로그를 버퍼링하지 않아야 진행 상황이 실시간으로 stderr 에 실린다.
    # 종전에는 한 시간을 폴링해도 어디까지 갔는지 알 길이 없었다.
    env["PYTHONUNBUFFERED"] = "1"
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
        out, tail = await asyncio.wait_for(_collect(proc, job), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        log.error("백테스트 %s 시간 초과(%.0f초) — 자식 종료", job, timeout)
        return {"ok": False, "error": f"시간 초과({timeout:.0f}초)"}
    if proc.returncode != 0:
        log.error("백테스트 %s 실패(rc=%s): %s", job, proc.returncode, tail)
        return {"ok": False, "error": f"실행 실패(rc={proc.returncode})"}
    # 진행 로그는 `_drain` 이 이미 한 줄씩 남겼다 — 여기서 다시 찍지 않는다.
    try:
        return json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError:
        log.error("백테스트 %s 응답 파싱 실패", job)
        return {"ok": False, "error": "결과 파싱 실패"}
