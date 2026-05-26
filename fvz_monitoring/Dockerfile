# Minimalistický Python image pre Render cron jobs aj lokálny dev.
# Render Blueprint vie pracovať aj bez Dockerfile (auto-detect Python),
# tento je tu pre konzistentný build prostredia.

FROM python:3.11-slim

WORKDIR /app

# System deps (psql client pre prípadné lokálne migration runy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY ingest/ ./ingest/
COPY dispatch/ ./dispatch/
COPY scripts/ ./scripts/

# Default je health-check, override-uje sa cez Render `startCommand`
CMD ["python", "-m", "ingest.orchestrator", "--health-check"]
