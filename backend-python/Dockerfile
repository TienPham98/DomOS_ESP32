FROM python:3.12-slim-bookworm

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
