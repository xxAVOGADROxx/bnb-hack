# Agent runtime: node (for the twak CLI/serve) + python (the trading loop).
# Both processes run in one container supervised by entrypoint.sh — if either
# dies the container exits and docker's restart policy revives the pair.
FROM node:22-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @trustwallet/cli

WORKDIR /app
COPY pyproject.toml ./
COPY agent ./agent
COPY config ./config
COPY scripts ./scripts
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir -e .

COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TWAK_SERVE_URL=http://127.0.0.1:3000

ENTRYPOINT ["/entrypoint.sh"]
# Default: continuous DRY-RUN. Switch to --live in docker-compose.yml for
# the trading window (explicit opt-in, never the default).
CMD []
