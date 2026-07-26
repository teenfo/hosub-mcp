FROM python:3.12-slim

WORKDIR /app

# 의존성 먼저 (레이어 캐시)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV LLMGW_CONFIG_DIR=/app/config \
    LLMGW_DB=/data/llmgw.db \
    PYTHONUNBUFFERED=1

EXPOSE 8603
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8603"]
