FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# 통합 가이드를 /v1/integration 으로 서빙한다 — 소비 프로젝트가 레포 접근 없이
# curl 한 번으로 최신 계약을 가져갈 수 있게. 이미지에 넣어 자립적으로 만든다.
COPY docs/ ./docs/
# 같은 이유로 파이썬 클라이언트와 목 서버도 이미지에 넣는다. 소비자가 실제로
# 쓰는 인터페이스는 HTTP 가 아니라 이 파일 하나이고, 여기 없으면 저장소 접근이
# 없는 소비자(roxlogy@Vercel)는 가장 좋은 길에서 배제된다.
# /v1/client/llmgw.py · /v1/client/mock_gateway.py 로 서빙한다.
COPY client/ ./client/
COPY tools/ ./tools/
# 브라우저용 API 탐색기(/v1/docs) + 벤더링한 swagger-ui.
# 레포 정책이 외부 CDN 금지라 자산을 이미지에 담는다.
COPY static/ ./static/

ENV LLMGW_CONFIG_DIR=/app/config \
    LLMGW_DB=/data/llmgw.db \
    PYTHONUNBUFFERED=1

EXPOSE 8603
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8603"]
