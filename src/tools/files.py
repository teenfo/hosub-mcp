"""파일 도구: read_file(Low), write_file(High), list_directory(Low).

서버 전체 제어 계층. 임의 경로 접근을 허용하며, write_file 만 confirm 게이트를
거친다. read_file/list_directory 는 조회성이라 즉시 실행한다.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..context import AppContext
from ..policy import check_confirm

_READ_MAX = 262144  # 256 KiB 상한


def _mode_str(m: int) -> str:
    return stat.filemode(m)


def register(mcp: FastMCP, ctx: AppContext) -> None:
    @mcp.tool()
    def read_file(path: str, max_bytes: int = 65536) -> dict:
        """서버의 텍스트 파일 내용을 읽는다.

        path: 읽을 파일의 절대경로.
        max_bytes: 최대 읽기 바이트 (기본 65536, 상한 262144). 초과 시 잘림 표시.
        """
        max_bytes = max(1, min(int(max_bytes), _READ_MAX))
        p = Path(path)
        if not p.is_absolute():
            return {"status": "rejected", "reason": "절대경로만 허용됩니다.", "path": path}
        if not p.exists():
            return {"status": "not_found", "path": path}
        if p.is_dir():
            return {"status": "rejected", "reason": "디렉터리입니다. list_directory 를 사용하세요.", "path": path}
        try:
            raw = p.read_bytes()[: max_bytes + 1]
        except OSError as exc:
            ctx.audit.log(tool="read_file", params={"path": path}, outcome="error", risk="low")
            return {"status": "error", "path": path, "error": str(exc)}
        truncated = len(raw) > max_bytes
        raw = raw[:max_bytes]
        try:
            content = raw.decode("utf-8")
            is_binary = False
        except UnicodeDecodeError:
            content = ""
            is_binary = True
        ctx.audit.log(tool="read_file", params={"path": path}, outcome="ok", risk="low")
        return {
            "status": "ok",
            "path": path,
            "binary": is_binary,
            "truncated": truncated,
            "content": content,
            "note": "바이너리 파일이라 내용을 표시하지 않습니다." if is_binary else None,
        }

    @mcp.tool()
    def list_directory(path: str, show_hidden: bool = False) -> dict:
        """디렉터리의 항목 목록(이름/종류/크기/권한)을 조회한다.

        path: 디렉터리의 절대경로.
        show_hidden: 숨김 파일(.으로 시작) 포함 여부.
        """
        p = Path(path)
        if not p.is_absolute():
            return {"status": "rejected", "reason": "절대경로만 허용됩니다.", "path": path}
        if not p.exists():
            return {"status": "not_found", "path": path}
        if not p.is_dir():
            return {"status": "rejected", "reason": "디렉터리가 아닙니다.", "path": path}
        entries = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name):
                if not show_hidden and child.name.startswith("."):
                    continue
                try:
                    st = child.lstat()
                    kind = (
                        "dir" if child.is_dir()
                        else "link" if child.is_symlink()
                        else "file"
                    )
                    entries.append(
                        {
                            "name": child.name,
                            "type": kind,
                            "size": st.st_size,
                            "mode": _mode_str(st.st_mode),
                        }
                    )
                except OSError:
                    entries.append({"name": child.name, "type": "unknown"})
        except OSError as exc:
            ctx.audit.log(tool="list_directory", params={"path": path}, outcome="error", risk="low")
            return {"status": "error", "path": path, "error": str(exc)}
        ctx.audit.log(tool="list_directory", params={"path": path}, outcome="ok", risk="low")
        return {"status": "ok", "path": path, "entries": entries}

    @mcp.tool()
    def write_file(
        path: str,
        content: str,
        mode: str = "overwrite",
        make_backup: bool = True,
        confirm: bool = False,
    ) -> dict:
        """서버의 파일에 내용을 쓴다 (서버 전체 제어).

        path: 쓸 파일의 절대경로.
        content: 기록할 텍스트 내용.
        mode: "overwrite"(덮어쓰기) 또는 "append"(끝에 추가).
        make_backup: overwrite 시 기존 파일을 .bak 로 백업할지 여부.
        confirm: 위험도 High — 사용자 승인 후 true 로 재호출해야 실행된다.
        """
        p = Path(path)
        if not p.is_absolute():
            return {"status": "rejected", "reason": "절대경로만 허용됩니다.", "path": path}
        if mode not in ("overwrite", "append"):
            return {"status": "rejected", "reason": "mode 는 overwrite 또는 append 여야 합니다."}
        action = f"{mode} 파일 쓰기: {path} ({len(content)} bytes)"
        denial = check_confirm("write_file", confirm, action)
        if denial:
            ctx.audit.log(
                tool="write_file",
                params={"path": path, "mode": mode},
                confirm=False,
                risk="high",
                outcome="approval_required",
            )
            return denial

        backup_path = None
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if mode == "overwrite" and make_backup and p.exists() and p.is_file():
                backup_path = str(p) + ".bak"
                shutil.copy2(p, backup_path)
            if mode == "append":
                with p.open("a", encoding="utf-8") as f:
                    f.write(content)
            else:
                with p.open("w", encoding="utf-8") as f:
                    f.write(content)
            written = os.path.getsize(p)
        except OSError as exc:
            ctx.audit.log(
                tool="write_file",
                params={"path": path, "mode": mode},
                confirm=True,
                risk="high",
                outcome="error",
                result_summary=str(exc),
            )
            return {"status": "error", "path": path, "error": str(exc)}

        ctx.audit.log(
            tool="write_file",
            params={"path": path, "mode": mode},
            confirm=True,
            risk="high",
            outcome="ok",
            result_summary=f"{written} bytes, backup={backup_path}",
        )
        return {
            "status": "ok",
            "path": path,
            "mode": mode,
            "bytes_on_disk": written,
            "backup": backup_path,
        }
