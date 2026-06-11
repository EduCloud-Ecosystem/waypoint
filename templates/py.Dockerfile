# py.Dockerfile: canonical image for Python tenants on the EduCloud layer.
#
# Covers Dash, Flask, FastAPI, Streamlit, and Shiny for Python. Dockerfile is
# the canonical production artifact (INTEGRATION.md portability rule); nixpacks
# is for preview/throwaway only. Per tenant, provide requirements.txt and set
# START_CMD for the framework.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# curl is only for the container healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for layer caching.
COPY requirements.txt .
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install -r requirements.txt

# App source. Self-contained, relative paths only (onboarding contract).
COPY . .

# Single internal port; Traefik is the only public entrance.
EXPOSE 8000

# Persistent write path. For apps that save data (for example demo-survey),
# Coolify mounts a persistent volume at DATA_DIR; the app writes only there so
# data survives restarts and is included in backups. Harmless if unused.
ENV DATA_DIR=/data

# Start command per framework. Override START_CMD per tenant.
# RULE: the server MUST bind 0.0.0.0 (all interfaces) on the EXPOSEd port, never
# 127.0.0.1/localhost. Traefik reaches the container over the Docker network; a
# loopback-only bind is unreachable and the app appears down. Examples:
#   Dash:             gunicorn app:server -b 0.0.0.0:8000 --workers 2
#   Flask:            gunicorn app:app -b 0.0.0.0:8000 --workers 2
#   FastAPI (ASGI):   uvicorn app:app --host 0.0.0.0 --port 8000
#   Streamlit:        streamlit run app.py --server.port 8000 --server.address 0.0.0.0
#   Shiny for Python: shiny run --host 0.0.0.0 --port 8000 app.py
ENV START_CMD="uvicorn app:app --host 0.0.0.0 --port 8000"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

# Shell form so START_CMD expands at runtime.
CMD ["sh", "-c", "$START_CMD"]
