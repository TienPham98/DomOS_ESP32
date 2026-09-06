FROM node:22-bookworm-slim AS codex
ARG CODEX_VERSION=0.146.0
RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && install -m 755 /usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-*/vendor/*/codex/codex /codex

FROM python:3.12-slim-bookworm
COPY --from=codex /codex /usr/local/bin/codex

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends mosquitto mosquitto-clients ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .
RUN chmod +x /app/cloud-entrypoint.sh \
    && mkdir -p /data

EXPOSE 8000
EXPOSE 1883/tcp
EXPOSE 9001

ENTRYPOINT ["/app/cloud-entrypoint.sh"]
