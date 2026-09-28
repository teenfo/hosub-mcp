"""deploy/update.sh 의 재시작 판정 검증 (실제 스크립트를 임시 git 저장소로 실행).

systemctl / sudo 는 PATH 앞쪽 스텁으로 바꿔 끼운다. 스텁은 restart 호출을
파일에 적고, MainPID 는 테스트가 정한 값을 돌려준다.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "update.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None,
    reason="git/bash 필요",
)

_SYSTEMCTL = """#!/usr/bin/env bash
log="$STUB_DIR/calls.log"
case "$1" in
  show)
    # systemctl show -p MainPID --value <unit>
    unit="${@: -1}"
    if [ "$unit" = "hosub-dash.service" ]; then cat "$STUB_DIR/dash_pid"; else cat "$STUB_DIR/mcp_pid"; fi ;;
  restart) echo "restart $2" >> "$log" ;;
  list-unit-files|is-enabled|is-active) exit 0 ;;
  status) exit 0 ;;
  *) exit 0 ;;
esac
"""

_SUDO = """#!/usr/bin/env bash
exec "$@"
"""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rev(cwd):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


class Env:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.origin = tmp / "origin.git"
        self.work = tmp / "work"  # 커밋을 만드는 쪽 (GitHub 역할)
        self.app = tmp / "app"  # 서버 클론 (/opt/hosub-mcp 역할)
        self.stub = tmp / "stub"
        self.stub.mkdir()
        for name, body in (("systemctl", _SYSTEMCTL), ("sudo", _SUDO)):
            p = self.stub / name
            p.write_text(body)
            p.chmod(0o755)
        (self.stub / "mcp_pid").write_text("1111\n")
        (self.stub / "dash_pid").write_text("2222\n")

        _git(tmp, "init", "--bare", "-b", "main", str(self.origin))
        _git(tmp, "clone", str(self.origin), str(self.work))
        for c in (self.work,):
            _git(c, "config", "user.email", "t@t")
            _git(c, "config", "user.name", "t")
        (self.work / "src").mkdir()
        (self.work / "src" / "a.py").write_text("x = 1\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-m", "init")
        _git(self.work, "push", "origin", "HEAD:main")
        _git(tmp, "clone", "-b", "main", str(self.origin), str(self.app))
        (self.app / "data").mkdir()
        self.base = _rev(self.app)

    def commit(self, path: str, content: str = "") -> str:
        p = self.work / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or f"{path} {time.time()}\n")
        _git(self.work, "add", ".")
        _git(self.work, "commit", "-m", f"change {path}")
        _git(self.work, "push", "origin", "HEAD:main")
        return _rev(self.work)

    def runstate(self, name: str, rev: str | None, active: int = 0, pid: int | None = None):
        pid = pid if pid is not None else (1111 if name == "mcp" else 2222)
        (self.app / "data" / f"runstate-{name}.json").write_text(
            json.dumps({"pid": pid, "rev": rev, "active_jobs": active, "jobs": []})
        )

    def run(self) -> tuple[str, list[str]]:
        log = self.stub / "calls.log"
        log.unlink(missing_ok=True)
        env = {
            **os.environ,
            "PATH": f"{self.stub}:{os.environ['PATH']}",
            "STUB_DIR": str(self.stub),
            "HOSUB_MCP_APP_DIR": str(self.app),
        }
        for k in ("HOSUB_RUNSTATE_MCP", "HOSUB_RUNSTATE_DASH", "HOSUB_UPDATE_MAX_DEFER_SEC"):
            env.pop(k, None)
        env.update(self.extra_env)
        out = subprocess.run(
            ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, out.stdout + out.stderr
        calls = log.read_text().split("\n") if log.exists() else []
        return out.stdout, [c for c in calls if c]

    extra_env: dict = {}


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    e.extra_env = {}
    return e


def test_docs_only_commit_pulls_without_restart(env):
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    env.commit("docs/trading/measurement.md")
    new = env.commit("trading/app/x.py")
    out, calls = env.run()
    assert calls == []
    assert "재시작 불필요" in out
    assert _rev(env.app) == new  # pull 은 됐다
    assert (env.app / "docs" / "trading" / "measurement.md").exists()


def test_src_commit_restarts_mcp_and_dash(env):
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    env.commit("src/tools/x.py")
    _, calls = env.run()
    assert calls == ["restart hosub-dash.service", "restart hosub-mcp"]


def test_static_commit_restarts_dash_only(env):
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    env.commit("static/app.js")
    _, calls = env.run()
    assert calls == ["restart hosub-dash.service"]


def test_unknown_path_is_treated_as_full(env):
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    env.commit("newdir/thing.txt")
    _, calls = env.run()
    assert "restart hosub-mcp" in calls


def test_active_job_defers_then_restarts_when_idle(env):
    env.runstate("mcp", env.base, active=1)
    env.runstate("dash", env.base)
    env.commit("src/tools/x.py")
    out, calls = env.run()
    assert "restart hosub-mcp" not in calls
    assert "연기" in out
    # 다음 주기: 원격은 그대로(LOCAL == REMOTE)여도 밀린 재시작을 기억해야 한다
    out, calls = env.run()
    assert "restart hosub-mcp" not in calls
    env.runstate("mcp", env.base, active=0)
    out, calls = env.run()
    assert "restart hosub-mcp" in calls


def test_defer_has_upper_bound(env):
    env.runstate("mcp", env.base, active=2)
    env.runstate("dash", env.base)
    env.commit("src/tools/x.py")
    env.extra_env = {"HOSUB_UPDATE_MAX_DEFER_SEC": "100"}
    _, calls = env.run()
    assert "restart hosub-mcp" not in calls
    (env.app / "data" / ".update-deferred-mcp").write_text(str(int(time.time()) - 500))
    out, calls = env.run()
    assert "restart hosub-mcp" in calls
    assert "강행" in out


def test_stale_runstate_falls_back_to_pre_pull_head(env):
    # 기록한 pid 가 현재 MainPID 가 아니면(죽은 프로세스) 무시하고 pull 직전 HEAD 기준
    env.runstate("mcp", "0" * 40, active=5, pid=9999)
    env.runstate("dash", env.base)
    env.commit("docs/x.md")
    _, calls = env.run()
    assert calls == []


def test_missing_runstate_restarts_on_src_change(env):
    # 구버전(런스테이트 미기록) → 예전처럼 이번 pull 의 변경으로 판단
    env.commit("src/tools/x.py")
    _, calls = env.run()
    assert "restart hosub-mcp" in calls


def test_restart_needed_even_if_someone_else_pulled(env):
    # deploy_service("tnm") 등이 같은 클론을 먼저 pull 해 LOCAL == REMOTE 인 경우
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    env.commit("src/tools/x.py")
    _git(env.app, "pull", "--ff-only")
    _, calls = env.run()
    assert "restart hosub-mcp" in calls


def test_up_to_date_does_nothing(env):
    env.runstate("mcp", env.base)
    env.runstate("dash", env.base)
    out, calls = env.run()
    assert calls == []
    assert "이미 최신" in out
