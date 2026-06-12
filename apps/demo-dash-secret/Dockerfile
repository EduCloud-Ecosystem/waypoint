# demo-dash-secret: instantiated from templates/py.Dockerfile.
# START_CMD set for Dash (gunicorn app:server). DEMO_API_KEY is NOT baked here;
# it is injected at runtime via env (Coolify env UI / compose env).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -r requirements.txt

COPY . .

EXPOSE 8000
ENV START_CMD="gunicorn app:server -b 0.0.0.0:8000 --workers 2"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

CMD ["sh", "-c", "$START_CMD"]
