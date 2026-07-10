"""systemctl / journalctl 조작 헬퍼.

도구 계층과 대시보드 API 가 공유한다. 모든 명령은 고정 템플릿(argv)이며
러너를 통과한다 — 유닛 이름은 레지스트리에서 검증된 값만 들어온다.
"""

from __future__ import annotations

from .registry import Registry, ServiceEntry
from .runner import CommandRunner

_SHOW_PROPS = "ActiveState,SubState,MainPID,ExecMainStartTimestamp,UnitFileState"


def query_service(runner: CommandRunner, entry: ServiceEntry) -> dict:
    """단일 서비스 상태를 systemctl show 로 조회."""
    res = runner.run(
        [
            "systemctl",
            "show",
            entry.unit,
            f"--property={_SHOW_PROPS}",
            "--no-pager",
        ],
        timeout=10,
    )
    props: dict[str, str] = {}
    for line in res.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            props[k] = v
    return {
        "name": entry.name,
        "unit": entry.unit,
        "description": entry.description,
        "active_state": props.get("ActiveState", "unknown"),
        "sub_state": props.get("SubState", "unknown"),
        "main_pid": props.get("MainPID"),
        "since": props.get("ExecMainStartTimestamp") or None,
        "enabled": props.get("UnitFileState"),
        "query_ok": res.ok,
        "error": None if res.ok else (res.stderr.strip() or "systemctl 조회 실패"),
    }


def query_all(runner: CommandRunner, registry: Registry) -> list[dict]:
    return [query_service(runner, e) for e in registry.services]


def read_logs(runner: CommandRunner, entry: ServiceEntry, lines: int) -> dict:
    lines = max(1, min(int(lines), 1000))
    res = runner.run(
        [
            "journalctl",
            "-u",
            entry.unit,
            "-n",
            str(lines),
            "--no-pager",
            "-o",
            "short-iso",
        ],
        timeout=15,
    )
    return {
        "service_name": entry.name,
        "unit": entry.unit,
        "lines": lines,
        "log": res.stdout if res.ok else "",
        "ok": res.ok,
        "error": None if res.ok else (res.stderr.strip() or "journalctl 조회 실패"),
    }


def restart_service(runner: CommandRunner, entry: ServiceEntry) -> dict:
    res = runner.run(["sudo", "-n", "systemctl", "restart", entry.unit], timeout=60)
    status = query_service(runner, entry)
    return {
        "status": "ok" if res.ok else "error",
        "restarted": entry.unit,
        "command_ok": res.ok,
        "message": res.combined_output or None,
        "state": status,
    }
