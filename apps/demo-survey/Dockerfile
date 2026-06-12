# demo-survey: instantiated from templates/py.Dockerfile.
# START_CMD set for Shiny for Python. DATA_DIR is the persistent write path; on
# the server Coolify mounts a volume there, locally compose mounts a named volume.
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
ENV DATA_DIR=/data
ENV START_CMD="shiny run --host 0.0.0.0 --port 8000 app.py"

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/ || exit 1

CMD ["sh", "-c", "$START_CMD"]
