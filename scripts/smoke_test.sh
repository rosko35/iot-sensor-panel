#!/usr/bin/env bash
# End-to-end smoke test of the Docker stack (used by CI, works locally too).
#
#   scripts/smoke_test.sh insecure
#   scripts/smoke_test.sh secure     # needs certs, passwd and .env (see docs/security-demo.md)
#
# Brings the stack up, waits until telemetry from every simulated device has
# reached InfluxDB, checks the Grafana datasource and dashboard, then tears the
# stack down again.
set -euo pipefail

MODE="${1:-insecure}"
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi
INFLUX_ORG="${INFLUX_ORG:-iot}"
INFLUX_BUCKET="${INFLUX_BUCKET:-sensors}"
INFLUX_TOKEN="${INFLUX_TOKEN:-dev-only-influx-token-change-me}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-change-me-grafana}"
SIM_DEVICES="${SIM_DEVICES:-sim-living-room,sim-server-room,sim-greenhouse,sim-garage}"

case "$MODE" in
  insecure) FILES=(-f docker-compose.yml) ;;
  secure) FILES=(-f docker-compose.yml -f docker-compose.secure.yml) ;;
  *) echo "usage: $0 insecure|secure" >&2; exit 2 ;;
esac
dc() { docker compose "${FILES[@]}" "$@"; }

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "---- smoke test failed, container logs ----"
    dc logs --no-color --tail 80 || true
  fi
  dc down -v --remove-orphans >/dev/null 2>&1 || true
  exit $status
}
trap cleanup EXIT

dc up -d --build

expected=$(tr ',' '\n' <<<"$SIM_DEVICES" | grep -c .)
query="from(bucket: \"$INFLUX_BUCKET\")
  |> range(start: -10m)
  |> filter(fn: (r) => r._measurement == \"telemetry\" and r._field == \"temperature\")
  |> group(columns: [\"device_id\"])
  |> count()"

echo "waiting for telemetry from $expected devices..."
for _ in $(seq 1 60); do
  # Annotated CSV: one data row per device, shaped ",<result>,<table>,<device>,<count>".
  # The result column is empty when it equals the "#default" annotation, so match both.
  raw=$(dc exec -T influxdb influx query --org "$INFLUX_ORG" --token "$INFLUX_TOKEN" \
    --raw "$query" 2>&1 || true)
  seen=$(grep -Ec '^,(_result)?,[0-9]+,' <<<"$raw" || true)
  if [[ "$seen" -ge "$expected" ]]; then
    echo "ok: telemetry from $seen devices in InfluxDB"
    break
  fi
  sleep 5
done
if [[ "${seen:-0}" -lt "$expected" ]]; then
  echo "only $seen/$expected devices reached InfluxDB" >&2
  echo "---- last query output ----" >&2
  echo "${raw:-<none>}" >&2
  exit 1
fi

echo "checking Grafana..."
for _ in $(seq 1 30); do
  curl -fsS http://localhost:3000/api/health >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS -u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" \
  http://localhost:3000/api/datasources/uid/influxdb/health | tee /dev/stderr | grep -q '"OK"'
curl -fsS -u "$GRAFANA_ADMIN_USER:$GRAFANA_ADMIN_PASSWORD" \
  http://localhost:3000/api/dashboards/uid/iot-sensor-overview >/dev/null
echo "ok: Grafana datasource healthy and dashboard provisioned"

if [[ "$MODE" == "secure" ]]; then
  if (exec 3<>/dev/tcp/127.0.0.1/1883) 2>/dev/null; then
    echo "plain MQTT port 1883 is reachable in secure mode" >&2
    exit 1
  fi
  echo "ok: port 1883 closed in secure mode"
  dc logs mosquitto | grep -qi "not authori[sz]ed" && echo "note: broker logged denied clients"
fi

echo "smoke test ($MODE) passed"
