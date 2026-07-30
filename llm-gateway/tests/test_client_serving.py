"""클라이언트 원본 서빙과 그 메타데이터.

`docs/integration.md` 1절은 "클라이언트 한 파일을 자기 레포에 복사" 라고 적어
두고도, `client/` 가 이미지에 없어서 저장소 접근이 없는 소비자는 받을 수 없었다.
이 테스트가 지키는 것은 두 가지다 — **응답 바이트가 디스크와 같은지**, 그리고
**`/v1/meta` 의 sha256 이 그 바이트에서 나온 값인지**. 셋이 어긋나면 소비자는
낡은 사본을 최신이라 믿는다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.main import CLIENT_SOURCES, build_app
from app.meta import CLIENT_FILES
from app.scheduler import Scheduler
from tests.conftest import TOKEN_ALPHA, TOKEN_BETA, FakeOllama

H = {"Authorization": f"Bearer {TOKEN_ALPHA}"}
H_BETA = {"Authorization": f"Bearer {TOKEN_BETA}"}

ROOT = Path(__file__).resolve().parent.parent
SERVED = {
    "/v1/client/llmgw.py": ROOT / "client" / "llmgw.py",
    "/v1/client/mock_gateway.py": ROOT / "tools" / "mock_gateway.py",
}


@pytest.fixture
def client(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        yield c


def test_sources_require_a_token(client):
    for path in SERVED:
        assert client.get(path).status_code == 401, path


def test_any_valid_token_can_fetch(client):
    """발급된 토큰이 온보딩 키다 — 역할 권한과는 무관하다."""
    for path in SERVED:
        assert client.get(path, headers=H_BETA).status_code == 200, path


@pytest.mark.parametrize("path", sorted(SERVED))
def test_served_bytes_equal_the_file_on_disk(client, path):
    r = client.get(path, headers=H)
    assert r.status_code == 200
    assert r.content == SERVED[path].read_bytes()
    assert r.headers["content-type"].startswith("text/x-python")


@pytest.mark.parametrize("path", sorted(SERVED))
def test_content_disposition_only_on_download(client, path):
    plain = client.get(path, headers=H)
    assert "content-disposition" not in {k.lower() for k in plain.headers}
    dl = client.get(f"{path}?download=1", headers=H)
    assert "attachment" in dl.headers["content-disposition"]
    assert dl.content == plain.content


def test_meta_hash_matches_the_served_bytes(client):
    """3중 확인: 응답 바이트 == 디스크 == /v1/meta 의 sha256."""
    meta = client.get("/v1/meta", headers=H).json()["client"]
    for key, info in CLIENT_FILES.items():
        entry = meta["files"][key]
        assert entry["available"] is True, key
        served = client.get(entry["download"], headers=H).content
        on_disk = (ROOT / info["path"]).read_bytes()
        assert served == on_disk, key
        assert entry["sha256"] == hashlib.sha256(served).hexdigest(), key
        assert entry["bytes"] == len(served), key


def test_meta_describes_what_openapi_cannot(client):
    """폴링·타임아웃 확대·예외 구분은 스펙으로 표현되지 않는다."""
    python = client.get("/v1/meta", headers=H).json()["client"]["files"]["python"]
    assert python["entrypoints"] == ["LLMGateway", "AsyncLLMGateway", "Job"]
    assert set(python["exceptions"]) == {
        "AuthError", "RoleError", "GatewayError", "JobFailed", "JobTimeout"}
    assert any("재시도" in n for n in python["notes"])
    assert python["requires"] == ["httpx>=0.27"]


def test_declared_entrypoints_and_exceptions_exist_in_the_real_file(client):
    """메타데이터가 없는 이름을 약속하면 실패한다."""
    source = (ROOT / "client" / "llmgw.py").read_text()
    python = client.get("/v1/meta", headers=H).json()["client"]["files"]["python"]
    for name in python["entrypoints"]:
        assert f"class {name}" in source, name
    for name in python["exceptions"]:
        assert f"class {name}" in source, name


def test_missing_file_says_rebuild_instead_of_pretending(store, roles, services,
                                                         monkeypatch, tmp_path):
    """이미지에 파일이 없으면 available=false 이고 404 가 재빌드를 지시한다.

    있는 척하면 소비자는 404 를 받고서야 안다 — 예전 상태가 정확히 그랬다.
    """
    monkeypatch.setitem(CLIENT_SOURCES, "llmgw.py", tmp_path / "gone.py")
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        entry = c.get("/v1/meta", headers=H).json()["client"]["files"]["python"]
        assert entry["available"] is False
        assert "sha256" not in entry
        r = c.get("/v1/client/llmgw.py", headers=H)
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"
    assert "재빌드" in r.json()["detail"]


def test_hash_follows_the_file(client, monkeypatch, tmp_path):
    """파일이 바뀌면 sha256 도 바뀐다(캐시가 낡은 값을 붙들지 않는다)."""
    fake_file = tmp_path / "llmgw.py"
    fake_file.write_text("# v1\n")
    monkeypatch.setitem(CLIENT_SOURCES, "llmgw.py", fake_file)
    first = client.get("/v1/meta", headers=H).json()["client"]["files"]["python"]["sha256"]

    fake_file.write_text("# v2 — 한 줄 늘었다\n")
    second = client.get("/v1/meta", headers=H).json()["client"]["files"]["python"]["sha256"]
    assert first != second
    assert second == hashlib.sha256(fake_file.read_bytes()).hexdigest()


def test_docs_link_is_absent_until_the_explorer_exists(client):
    """없는 경로를 약속하지 않는다 — 라우트가 생기면 자동으로 나타난다."""
    body = client.get("/v1/meta", headers=H).json()
    live = {r["path"] for r in body["endpoints"]}
    if "/v1/docs" in live:
        assert body["links"]["docs"] == "/v1/docs"
    else:
        assert "docs" not in body["links"]
        assert "docs" not in body["client"]


def test_client_paths_are_in_the_spec(client):
    spec = client.get("/v1/openapi.json", headers=H).json()
    for path in SERVED:
        assert path in spec["paths"], path
        assert "404" in spec["paths"][path]["get"]["responses"]
