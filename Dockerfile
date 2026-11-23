FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

ENV PORT=8000

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY backend ./backend
COPY README.md ./

RUN mkdir -p /data

EXPOSE 8000

CMD ["sh", "-c", "uvicorn backend.webhook.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
