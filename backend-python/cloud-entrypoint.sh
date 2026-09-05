#!/bin/sh
set -eu

: "${MQTT_USERNAME:?MQTT_USERNAME is required}"
: "${MQTT_PASSWORD:?MQTT_PASSWORD is required}"

password_file=/tmp/domos-mosquitto-passwd
mosquitto_passwd -b -c "$password_file" "$MQTT_USERNAME" "$MQTT_PASSWORD"
chown mosquitto:mosquitto "$password_file"
chmod 600 "$password_file"

mosquitto -c /app/mosquitto-cloud.conf &
broker_pid=$!
gateway_pid=""

shutdown() {
    for pid in "$broker_pid" "$gateway_pid"; do
        if [ -n "$pid" ]; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap shutdown INT TERM EXIT

python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" &
gateway_pid=$!

while kill -0 "$broker_pid" 2>/dev/null && kill -0 "$gateway_pid" 2>/dev/null; do
    sleep 2
done

exit 1
