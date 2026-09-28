"""장기 작업을 MCP 수명과 분리해 실행하는 도구: run_unit (High).

run_command(background=true) 잡은 이 프로세스의 자식이라, MCP 가 재시작되면
(코드 배포·재부팅) 함께 죽는다. 30분~수 시간짜리 측정(백테스트 스윕 등)은
systemd transient 유닛으로 띄워 서비스 수명과 분리해야 한다.

설계 — `--collect` 를 **쓰지 않는다**:
  --collect 는 끝난 유닛을 즉시 치워서, 성공했는지 실패했는지를 unit_status 로
  다시 볼 수 없게 된다(LoadState=not-found). 대신 RemainAfterExit=yes 로 띄워
  끝난 뒤에도 상태가 남게 한다. 실서버(systemd 255)에서 확인한 결과:
    성공     → active/exited, Result=success, ExecMainStatus=0
    실패     → failed,        Result=exit-code, ExecMainStatus=<코드>
    시간초과 → failed,        Result=timeout (RuntimeMaxSec)
  남은 유닛은 같은 이름으로 다시 run_unit 할 때 stop + reset-failed 로 정리한다.

상태·로그 조회는 unit_status / unit_logs (Low).
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..policy import check_confirm
from .readonly import now_kst, show_unit

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_UNIT_PREFIX = "mcp-"
_TIMEOUT_DEFAULT = 7200
_TIMEOUT_MAX = 86400
# 이 상태면 "아직 돌고 있다" — 덮어쓰지 않는다.
_BUSY_STATES = {"active", "activating", "deactivating", "reloading"}


def build_argv(
    unit: str,
    command: str,
    *,
    workdir: str | None,
    env: dict[str, str],
    uid: str,
    timeout: int,
) -> list[str]:
    argv = [
        "sudo", "-n", "systemd-run",
        f"--unit={unit}",
        f"--uid={uid}",
        "--property=RemainAfterExit=yes",
        f"--property=RuntimeMaxSec={timeout}",
        f"--description=MCP run_unit: {command[:80]}",
    ]
    if workdir:
        argv.append(f"--working-directory={workdir}")
    for k, v in env.items():
        argv.append(f"--setenv={k}={v}")
    # 명령은 셸 문자열이다(파이프·&& 허용 — run_command 와 같은 표현력).
    # '--' 뒤라 systemd-run 옵션으로 해석되지 않는다.
    argv += ["--", "bash", "-lc", command]
    return argv


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def run_unit(
        name: str,
        command: str,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        uid: str = "hosub",
        timeout: int = _TIMEOUT_DEFAULT,
        confirm: bool = False,
    ) -> dict:
        """장기 작업을 systemd transient 유닛(mcp-<name>.service)으로 실행한다.

        MCP 재시작·배포와 무관하게 끝까지 돈다. 30분 넘는 작업에 쓴다.
        name: 유닛 이름 접미사 (영숫자·_.-, 64자). 유닛은 mcp-<name>.service.
              같은 이름이 아직 실행 중이면 거부, 끝난 상태면 정리 후 다시 띄운다.
        command: bash -lc 로 실행할 명령. 출력은 journald 로 간다.
        workdir: 작업 디렉터리 (예: /opt/hosub-trading).
        env: 환경변수 (예: {"PYTHONPATH": "trading"}).
        uid: 실행 사용자 (기본 hosub).
        timeout: 최대 실행 초 (기본 7200, 상한 86400). 넘으면 Result=timeout.
        confirm: 위험도 High — 사용자 승인 후 true 로 재호출해야 실행된다.

        이후 확인: unit_status("mcp-<name>", path="<결과 파일>"),
                   unit_logs("mcp-<name>", since="-1h").
        """
        env = dict(env or {})
        if not _NAME_RE.match(name or ""):
            return {"status": "rejected", "reason": f"올바르지 않은 name: {name!r} (영숫자·_.-, 64자)"}
        if not _USER_RE.match(uid or ""):
            return {"status": "rejected", "reason": f"올바르지 않은 uid: {uid!r}"}
        bad = [k for k in env if not _ENV_KEY_RE.match(str(k))]
        if bad:
            return {"status": "rejected", "reason": f"올바르지 않은 환경변수 이름: {bad}"}
        if any("\n" in str(v) for v in env.values()):
            return {"status": "rejected", "reason": "환경변수 값에 줄바꿈을 넣을 수 없습니다."}
        if workdir is not None and not workdir.startswith("/"):
            return {"status": "rejected", "reason": "workdir 는 절대경로여야 합니다."}
        if not command.strip():
            return {"status": "rejected", "reason": "command 가 비어 있습니다."}
        timeout = max(1, min(int(timeout), _TIMEOUT_MAX))
        unit = f"{_UNIT_PREFIX}{name}.service"

        env_desc = " ".join(f"{k}=…" for k in env)
        action = (
            f"systemd-run {unit} (uid={uid}, timeout={timeout}s"
            + (f", cwd={workdir}" if workdir else "")
            + (f", env: {env_desc}" if env else "")
            + f"): {command}"
        )
        # 환경변수 값은 토큰일 수 있으니 감사 로그에는 키만 남긴다.
        params = {"name": name, "command": command, "workdir": workdir,
                  "env_keys": sorted(env), "uid": uid, "timeout": timeout}
        denial = check_confirm("run_unit", confirm, action)
        if denial:
            ctx.audit.log(tool="run_unit", params=params, confirm=False, risk="high",
                          outcome="approval_required")
            return denial

        # 같은 이름의 이전 유닛 처리
        prev = show_unit(ctx, unit)["props"]
        if prev.get("LoadState") == "loaded":
            state, sub = prev.get("ActiveState"), prev.get("SubState")
            if state in _BUSY_STATES and sub != "exited":
                ctx.audit.log(tool="run_unit", params=params, confirm=True, risk="high",
                              outcome="rejected", result_summary=f"{unit} 실행 중 ({state}/{sub})")
                return {
                    "status": "rejected",
                    "reason": f"{unit} 가 아직 실행 중입니다 ({state}/{sub}). 끝난 뒤 다시 하거나 다른 name 을 쓰세요.",
                    "unit": unit,
                }
            # 끝난 유닛(exited/failed)은 치우고 재사용한다
            ctx.runner.run(["sudo", "-n", "systemctl", "stop", unit], timeout=30)
            ctx.runner.run(["sudo", "-n", "systemctl", "reset-failed", unit], timeout=15)

        argv = build_argv(unit, command, workdir=workdir, env=env, uid=uid, timeout=timeout)
        res = ctx.runner.run(argv, timeout=30)
        outcome = "started" if res.ok else "error"
        ctx.audit.log(tool="run_unit", params=params, confirm=True, risk="high",
                      outcome=outcome, result_summary=res.combined_output[-300:])
        if not res.ok:
            return {"status": "error", "unit": unit, "exit_code": res.exit_code,
                    "output": res.combined_output[-4000:]}
        return {
            "status": "started",
            "unit": unit,
            "started_at": now_kst(),
            "timeout_seconds": timeout,
            "hint": (
                f'unit_status("{unit}") 로 상태(active/exited=성공, failed=실패·timeout)를, '
                f'unit_logs("{unit}") 로 출력을 확인하세요. 결과 파일이 있으면 '
                f'unit_status(..., path="<경로>") 로 크기·mtime 을 함께 봅니다.'
            ),
        }
