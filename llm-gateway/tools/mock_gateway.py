"""개발용 목 게이트웨이 — 맥도 도커도 없이 통합 코드를 짤 수 있게 한다.

    python tools/mock_gateway.py                # 8603 에서 기동, 토큰은 아무거나
    LLMGW_TOKEN=dev python -c "from client.llmgw import LLMGateway; \
        print(LLMGateway().run('summarize', '테스트'))"

**응답 형태는 실제 게이트웨이와 같다.** 소비 프로젝트는 이걸로 프롬프트·파싱·에러
처리를 완성해 두고, 나중에 LLMGW_URL 만 실제 주소로 바꾸면 된다.

역할 목록은 ../config/roles.yaml 을 읽는다(있으면). 없으면 내장 기본값.

옵션:
  --port 8603          바인딩 포트
  --delay 1.5          생성에 걸리는 시간(초). 큰 값을 주면 pending/폴링 경로를 시험
  --fail-rate 0.0      이 확률로 잡을 실패시킨다(0~1). 에러 처리 검증용
  --token dev          이 값만 허용. 생략하면 아무 Bearer 토큰이나 통과
  --deny summarize     이 역할은 403. 권한 오류 경로 검증용 (반복 지정 가능)

⚠️ 개발 전용이다. 인증이 사실상 없고 응답은 가짜다 — 절대 서버에 띄우지 말 것.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import secrets
import sys
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ROOT = Path(__file__).resolve().parent.parent

EMBED_DIM = 1024   # bge-m3 차원

BUILTIN_ROLES = {
    "summarize": ("qwen2.5:7b", "interactive", 120),
    "log_analyze": ("qwen2.5:14b", "interactive", 180),
    "translate": ("qwen2.5:14b", "interactive", 180),
    "code": ("qwen2.5-coder:14b", "interactive", 240),
    "general": ("qwen2.5:32b", "batch", 300),
    "analyze_workout": ("qwen2.5:14b", "batch", 240),
    "coach_feedback": ("qwen2.5:32b", "batch", 300),
    "classify_news": ("qwen2.5:7b", "interactive", 60),
}


def load_roles() -> dict[str, tuple[str, str, int]]:
    """실제 roles.yaml 이 있으면 그걸 쓴다 — 역할 이름이 어긋나지 않도록."""
    path = ROOT / "config" / "roles.yaml"
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        roles = {
            name: (str(cfg.get("model", "?")), str(cfg.get("lane", "batch")),
                   int(cfg.get("timeout", 180)))
            for name, cfg in (raw.get("roles") or {}).items()
            if isinstance(cfg, dict)
        }
        if roles:
            print(f"[mock] 역할 {len(roles)}개를 {path} 에서 읽음")
            return roles
    except Exception as exc:
        print(f"[mock] roles.yaml 을 못 읽어 내장 기본값 사용 ({exc})")
    return BUILTIN_ROLES


def shape(job: dict) -> dict:
    """실제 게이트웨이의 _job_response 와 **같은 키**를 돌려준다.

    이게 어긋나면 목으로 짠 소비자 코드가 실서버에서 깨진다.
    tests/test_client_contract.py 가 키 집합이 같은지 검사한다.
    """
    return {
        "job_id": job["id"], "status": job["status"],
        "response": job.get("response"), "error": job.get("error"),
        "role": job["role"], "model": job["model"], "lane": job["lane"],
        "attempts": 1, "metadata": job.get("metadata") or {},
        "created_at": job["created_at"], "started_at": job["created_at"],
        "finished_at": job.get("finished_at"),
        "queue_position": 0 if job["status"] == "pending" else None,
    }


def build_app(opts) -> Starlette:
    ROLES = load_roles()
    JOBS: dict[str, dict] = {}

    def auth(request: Request):
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        if not token or (opts.token and token != opts.token):
            return JSONResponse({"error": "unauthorized"}, status_code=401,
                                headers={"WWW-Authenticate": "Bearer"})
        return None

    async def finish_later(job: dict) -> None:
        await asyncio.sleep(opts.delay)
        if job["status"] != "pending":
            return                                  # 그 사이 취소됨
        if random.random() < opts.fail_rate:
            job.update(status="failed", error="[mock] 의도적으로 실패시킨 잡",
                       finished_at="2026-01-01T00:00:00+00:00")
        else:
            job.update(
                status="ok",
                response=(f"[mock:{job['model']}] {job['role']} 응답입니다.\n"
                          f"입력 {job['prompt_chars']}자를 받았습니다."),
                finished_at="2026-01-01T00:00:00+00:00",
            )

    async def healthz(request: Request):
        return JSONResponse({"ok": True, "mock": True})

    async def generate(request: Request):
        if (err := auth(request)):
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        role = str(body.get("role") or "")
        prompt = body.get("prompt") or ""
        if not role or not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse(
                {"error": "invalid_request", "detail": "role 과 prompt 가 필요합니다"},
                status_code=400)
        if role not in ROLES:
            return JSONResponse(
                {"error": "unknown_role", "known_roles": sorted(ROLES)},
                status_code=404)
        if role in opts.deny:
            return JSONResponse(
                {"error": "forbidden", "allowed": sorted(set(ROLES) - set(opts.deny))},
                status_code=403)

        model, lane, _ = ROLES[role]
        job = {
            "id": secrets.token_hex(6), "status": "pending", "role": role,
            "model": model, "lane": lane, "prompt_chars": len(prompt),
            "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        JOBS[job["id"]] = job
        task = asyncio.create_task(finish_later(job))

        try:
            wait = float(body.get("wait", 30))
        except (TypeError, ValueError):
            wait = 30
        if wait > 0:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=wait)
            except asyncio.TimeoutError:
                pass                                 # pending 으로 돌려준다
        return JSONResponse(shape(job))

    async def get_job(request: Request):
        if (err := auth(request)):
            return err
        job = JOBS.get(request.path_params["job_id"])
        if job is None:
            return JSONResponse({"error": "not_found"}, status_code=404)
        return JSONResponse(shape(job))

    async def list_jobs(request: Request):
        if (err := auth(request)):
            return err
        status = request.query_params.get("status")
        jobs = [j for j in JOBS.values() if not status or j["status"] == status]
        return JSONResponse({"jobs": [shape(j) for j in jobs[-20:]]})

    async def cancel_job(request: Request):
        if (err := auth(request)):
            return err
        job = JOBS.get(request.path_params["job_id"])
        if job is None or job["status"] != "pending":
            return JSONResponse(
                {"error": "not_cancellable",
                 "detail": "대기 중인 본인 잡만 취소할 수 있습니다"}, status_code=409)
        job.update(status="cancelled", finished_at="2026-01-01T00:00:00+00:00")
        return JSONResponse({"status": "cancelled"})

    async def embed(request: Request):
        if (err := auth(request)):
            return err
        body = await request.json()
        raw = body.get("input")
        texts = [raw] if isinstance(raw, str) else (raw or [])
        if not texts or not all(isinstance(t, str) for t in texts):
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        # 결정적 가짜 벡터 — 같은 입력이면 같은 값이라 테스트에 쓸 수 있다
        vecs = [[((hash(t) >> i) % 1000) / 1000.0 for i in range(EMBED_DIM)]
                for t in texts]
        return JSONResponse({"status": "ok", "model": "bge-m3",
                             "embeddings": vecs, "count": len(vecs),
                             "dimensions": EMBED_DIM, "duration_ms": 5})

    async def list_roles(request: Request):
        if (err := auth(request)):
            return err
        return JSONResponse({"service": "mock", "roles": [
            {"name": n, "model": m, "lane": ln, "timeout": t,
             "has_default_system": True, "max_prompt_chars": None}
            for n, (m, ln, t) in sorted(ROLES.items()) if n not in opts.deny
        ]})

    async def status(request: Request):
        if (err := auth(request)):
            return err
        return JSONResponse({
            "backend": {"base_url": "mock://ollama", "online": True, "error": None,
                        "models": sorted({m for m, _, _ in ROLES.values()}),
                        "loaded_model": None},
            "lanes": {"interactive": {"queued": 0, "running": 0},
                      "batch": {"queued": 0, "running": 0}},
            "running": {}, "mem_budget_gb": 40, "usage": [],
            "roles": [{"name": n, "model": m, "lane": ln, "timeout": t,
                       "has_default_system": True, "max_prompt_chars": None,
                       "model_available": True}
                      for n, (m, ln, t) in sorted(ROLES.items())],
            "model_requests": {"pending": 0, "pulling": {}},
            "mock": True,
        })

    async def model_requests(request: Request):
        if (err := auth(request)):
            return err
        return JSONResponse({"requests": [], "can_decide": False,
                             "available_models": sorted({m for m, _, _ in ROLES.values()})})

    return Starlette(routes=[
        Route("/healthz", healthz),
        Route("/v1/generate", generate, methods=["POST"]),
        Route("/v1/embed", embed, methods=["POST"]),
        Route("/v1/jobs", list_jobs, methods=["GET"]),
        Route("/v1/jobs/{job_id}", get_job, methods=["GET"]),
        Route("/v1/jobs/{job_id}", cancel_job, methods=["DELETE"]),
        Route("/v1/roles", list_roles),
        Route("/v1/status", status),
        Route("/v1/models/requests", model_requests),
    ])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="개발용 목 LLM 게이트웨이")
    p.add_argument("--port", type=int, default=8603)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--delay", type=float, default=1.5,
                   help="생성에 걸리는 시간(초). 크게 주면 pending/폴링 경로를 시험")
    p.add_argument("--fail-rate", type=float, default=0.0,
                   help="이 확률로 잡 실패(0~1). 에러 처리 검증용")
    p.add_argument("--token", default=None,
                   help="이 토큰만 허용. 생략하면 아무 Bearer 값이나 통과")
    p.add_argument("--deny", action="append", default=[],
                   help="403 을 돌려줄 역할 (반복 지정 가능)")
    opts = p.parse_args(argv)

    print(f"[mock] 개발 전용 게이트웨이 — http://{opts.host}:{opts.port}")
    print(f"[mock] 지연 {opts.delay}s · 실패율 {opts.fail_rate} · "
          f"토큰 {'고정' if opts.token else '아무거나'}"
          + (f" · 거부 역할 {opts.deny}" if opts.deny else ""))
    print("[mock] ⚠️ 응답은 가짜입니다. 서버에 띄우지 마세요.")
    uvicorn.run(build_app(opts), host=opts.host, port=opts.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
