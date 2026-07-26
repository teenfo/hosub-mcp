FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# 통합 가이드를 /v1/integration 으로 서빙한다 — 소비 프로젝트가 레포 접근 없이
# curl 한 번으로 최신 계약을 가져갈 수 있게. 이미지에 넣어 자립적으로 만든다.
COPY docs/ ./docs/

ENV LLMGW_CONFIG_DIR=/app/config \
    LLMGW_DB=/data/llmgw.db \
    PYTHONUNBUFFERED=1

EXPOSE 8603
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8603"]
