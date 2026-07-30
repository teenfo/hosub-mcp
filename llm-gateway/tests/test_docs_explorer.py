"""브라우저용 API 탐색기(`/v1/docs`).

이 페이지는 **무인증**이다 — 브라우저는 최상위 내비게이션에 Bearer 헤더를 싣지
못하므로 껍데기를 열어 줘야 토큰 입력 폼을 보여줄 수 있다. 그래서 여기서 지켜야
하는 경계가 있다: **껍데기에 서버 데이터가 없어야 한다.** 역할 이름·모델 이름이
새면 인증 없이 내부 구조를 알려 주는 셈이고, 그건 이 예외를 정당화하는 근거를
무너뜨린다. 아래 테스트가 그 경계를 지킨다.
"""

from __future__ import annotations

import re

import pytest
from starlette.testclient import TestClient

from app.main import STATIC_DIR, SWAGGER_DIR, build_app
from app.scheduler import Scheduler
from tests.conftest import ROLES_RAW, TOKEN_ALPHA, FakeOllama

H = {"Authorization": f"Bearer {TOKEN_ALPHA}"}


@pytest.fixture
def client(store, roles, services):
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        yield c


def test_page_opens_without_a_token(client):
    """무인증이어야 토큰 입력 폼을 보여줄 수 있다."""
    r = client.get("/v1/docs")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")


def test_page_leaks_no_server_data(client):
    """⚠️ 이 경계가 무인증 공개를 정당화한다 — 역할·모델 이름이 없어야 한다.

    단어 경계로 찾는다. 픽스처 역할명이 짧아(`mid`·`vec`) 부분 문자열로 보면
    무관한 CSS·SVG 속성값(`middle` 등)에 걸린다.
    """
    html = client.get("/v1/docs").text
    names = set(ROLES_RAW["roles"]) | {
        s["model"] for s in ROLES_RAW["roles"].values()}
    leaked = [n for n in names
              if re.search(rf"\b{re.escape(n)}\b", html)]
    assert leaked == [], f"역할·모델 이름이 껍데기에 새어 나왔다: {leaked}"
    # 관리 경로 이름도 없어야 한다
    assert "/v1/admin" not in html


def test_page_boots_from_a_live_fetch_not_a_bundled_copy(client):
    """스펙 사본을 페이지에 굽지 않는다 — 항상 실행 중인 게이트웨이에서 받는다."""
    html = client.get("/v1/docs").text
    assert "spec: spec" in html, "url: 로 부팅하면 토큰이 안 붙는다"
    assert 'SPEC_URL = "openapi.json"' in html
    assert "requestInterceptor" in html
    # 스펙을 직접 박아 넣은 흔적이 없어야 한다
    assert '"openapi": "3.1' not in html and "openapi: '3.1" not in html


def test_page_warns_about_production_token_use(client):
    """브라우저 토큰 금지는 프로덕션 코드에 여전히 적용된다 — 화면에 적는다."""
    html = client.get("/v1/docs").text
    assert "프로덕션" in html
    assert "sessionStorage" in html
    assert "레이트리밋" in html


def test_page_loads_no_external_asset(client):
    """CDN 금지(레포 정책). 모든 자산은 같은 출처의 상대경로여야 한다."""
    html = client.get("/v1/docs").text
    external = re.findall(r'(?:src|href)\s*=\s*"(//|https?://)[^"]*"', html)
    assert external == [], f"외부 자산 참조: {external}"
    assert 'href="docs/static/swagger-ui.css"' in html
    assert 'src="docs/static/swagger-ui-bundle.js"' in html


def test_asset_paths_survive_the_llm_prefix(client):
    """상대경로여야 공개 경로(/llm/v1/docs)에서도 맞는다.

    Caddy 가 /llm 을 벗겨 넘기므로 절대경로(`/v1/...`)를 쓰면 브라우저가
    /v1/... 을 그대로 요청해 404 를 맞는다.
    """
    html = client.get("/v1/docs").text
    assert 'href="/v1/' not in html and 'src="/v1/' not in html
    assert 'SPEC_URL = "/v1/' not in html


@pytest.mark.parametrize("name", ["swagger-ui-bundle.js", "swagger-ui.css"])
def test_vendored_assets_are_served_with_a_long_cache(client, name):
    r = client.get(f"/v1/docs/static/{name}")
    assert r.status_code == 200
    assert r.content == (SWAGGER_DIR / name).read_bytes()
    assert "max-age=604800" in r.headers["cache-control"]


def test_vendored_assets_are_actually_vendored():
    """자산이 레포에 있어야 이미지에 들어간다(CDN 을 대신 쓰지 않는다)."""
    assert (SWAGGER_DIR / "swagger-ui-bundle.js").is_file()
    assert (SWAGGER_DIR / "swagger-ui.css").is_file()
    assert (SWAGGER_DIR / "LICENSE").is_file(), "서드파티 라이선스를 함께 둔다"
    assert (STATIC_DIR / "docs.html").is_file()


def test_vendored_css_has_no_external_url(client):
    """CSS 의 url() 이 전부 data: 여야 브라우저가 외부로 나가지 않는다."""
    css = (SWAGGER_DIR / "swagger-ui.css").read_text()
    urls = re.findall(r'url\((["\']?)([^)"\']+)', css)
    external = [u for _, u in urls if u.startswith(("http", "//"))]
    assert external == [], f"CSS 가 외부 자산을 참조한다: {external[:3]}"


def test_missing_assets_do_not_take_down_the_gateway(store, roles, services,
                                                     monkeypatch, tmp_path):
    """자산이 없어도 추론은 계속돼야 한다 — 탐색기만 404 를 준다."""
    monkeypatch.setattr("app.main.SWAGGER_DIR", tmp_path / "gone")
    monkeypatch.setattr("app.main.DOCS_PAGE", tmp_path / "gone.html")
    fake = FakeOllama()
    sched = Scheduler(store, roles, fake, poll_interval=0.01)
    app = build_app(roles=roles, services=services, store=store,
                    client=fake, scheduler=sched)
    with TestClient(app) as c:
        assert c.get("/healthz").status_code == 200
        gen = c.post("/v1/generate", json={"role": "fast", "prompt": "x", "wait": 5},
                     headers=H)
        assert gen.status_code == 200
        r = c.get("/v1/docs")
    assert r.status_code == 404
    assert "재빌드" in r.json()["detail"]


def test_explorer_is_in_the_spec_as_a_public_path(client):
    spec = client.get("/v1/openapi.json", headers=H).json()
    op = spec["paths"]["/v1/docs"]["get"]
    assert op["security"] == [], "무인증임을 스펙에 적어야 한다"
    meta = client.get("/v1/meta", headers=H).json()
    entry = next(e for e in meta["endpoints"] if e["path"] == "/v1/docs")
    assert entry["auth"] is False
    # 라우트가 생겼으므로 링크도 함께 나타난다
    assert meta["links"]["docs"] == "/v1/docs"
    assert meta["client"]["docs"] == "/v1/docs"
