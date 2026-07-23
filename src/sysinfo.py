"""psutil 기반 시스템 상태 수집. 모든 플랫폼에서 동작 (systemd 불필요)."""

from __future__ import annotations

from datetime import datetime, timezone

import psutil


def _bytes_gb(n: int) -> float:
    return round(n / (1024**3), 2)


def collect_status(top_n: int = 5) -> dict:
    """CPU/메모리/디스크/업타임/상위 프로세스 요약."""
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()

    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, OSError):
            continue
        disks.append(
            {
                "mount": part.mountpoint,
                "device": part.device,
                "fstype": part.fstype,
                "total_gb": _bytes_gb(usage.total),
                "used_gb": _bytes_gb(usage.used),
                "percent": usage.percent,
            }
        )

    try:
        load1, load5, load15 = psutil.getloadavg()
    except (OSError, AttributeError):
        load1 = load5 = load15 = None

    boot = datetime.fromtimestamp(psutil.boot_time(), tz=timezone.utc)
    uptime_seconds = int((datetime.now(timezone.utc) - boot).total_seconds())

    return {
        "cpu": {
            "percent": psutil.cpu_percent(interval=0.1),
            "cores": psutil.cpu_count(logical=True),
            "load_avg": (
                [round(load1, 2), round(load5, 2), round(load15, 2)]
                if load1 is not None
                else None
            ),
        },
        "memory": {
            "total_gb": _bytes_gb(vm.total),
            "used_gb": _bytes_gb(vm.used),
            "available_gb": _bytes_gb(vm.available),
            "percent": vm.percent,
        },
        "swap": {
            "total_gb": _bytes_gb(swap.total),
            "used_gb": _bytes_gb(swap.used),
            "percent": swap.percent,
        },
        "disks": disks,
        "uptime_seconds": uptime_seconds,
        "boot_time": boot.isoformat(),
        "top_processes": _top_processes(top_n),
    }


def _top_processes(n: int) -> list[dict]:
    procs = []
    for p in psutil.process_iter(["pid", "name", "username", "memory_percent"]):
        try:
            info = p.info
            info["cpu_percent"] = p.cpu_percent(interval=0)
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    procs.sort(key=lambda x: (x.get("memory_percent") or 0), reverse=True)
    out = []
    for info in procs[:n]:
        out.append(
            {
                "pid": info["pid"],
                "name": info["name"],
                "user": info.get("username"),
                "mem_percent": round(info.get("memory_percent") or 0, 1),
            }
        )
    return out
