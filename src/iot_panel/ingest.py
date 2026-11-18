"""Ingest service: MQTT -> validation -> InfluxDB.

Subscribes to ``sensors/+/telemetry`` and ``sensors/+/status``, validates each
telemetry payload (see :mod:`iot_panel.payload`) and writes accepted readings
to InfluxDB. Rejected messages are logged and counted, never written.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

from influxdb_client import Point, WritePrecision

from .clock import Clock, SystemClock
from .mqtt import STATUS_SUBSCRIPTION, TELEMETRY_SUBSCRIPTION, build_client, describe
from .payload import DEVICE_ID_RE, PayloadError, Telemetry, device_id_from_topic, parse_payload
from .settings import InfluxSettings, MqttSettings, env_str

log = logging.getLogger("iot_panel.ingest")

TELEMETRY_MEASUREMENT = "telemetry"
STATUS_MEASUREMENT = "device_status"


class Writer(Protocol):
    """The subset of ``influxdb_client.WriteApi`` the service relies on."""

    def write(self, bucket: str, org: str, record: Any, **kwargs: Any) -> Any: ...


def seconds_to_ns(ts: float) -> int:
    return round(ts * 1_000_000_000)


def telemetry_point(t: Telemetry) -> Point:
    point = Point(TELEMETRY_MEASUREMENT).tag("device_id", t.device_id)
    for name, value in t.metrics().items():
        # motion is stored as an integer 0/1 so Grafana can plot it as a state.
        point = point.field(name, int(value) if name == "motion" else value)
    if t.rssi is not None:
        point = point.field("rssi", t.rssi)
    if t.seq is not None:
        point = point.field("seq", t.seq)
    return point.time(seconds_to_ns(t.ts), WritePrecision.NS)


def status_point(device_id: str, online: bool, ts: float) -> Point:
    return (
        Point(STATUS_MEASUREMENT)
        .tag("device_id", device_id)
        .field("online", 1 if online else 0)
        .time(seconds_to_ns(ts), WritePrecision.NS)
    )


class IngestService:
    def __init__(
        self,
        writer: Writer,
        *,
        bucket: str,
        org: str,
        clock: Clock | None = None,
    ) -> None:
        self.writer = writer
        self.bucket = bucket
        self.org = org
        self.clock = clock or SystemClock()
        self.stats: Counter[str] = Counter()

    # -- MQTT callbacks -------------------------------------------------------------

    def on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("connected to broker, subscribing")
        client.subscribe([(TELEMETRY_SUBSCRIPTION, 1), (STATUS_SUBSCRIPTION, 1)])

    def on_message(self, client, userdata, msg) -> None:
        try:
            self.handle_message(msg.topic, msg.payload)
        except Exception:  # never let one bad message kill the network thread
            self.stats["errors"] += 1
            log.exception("unexpected error while handling %s", msg.topic)

    # -- core logic -----------------------------------------------------------------

    def handle_message(self, topic: str, payload: bytes) -> Telemetry | None:
        if topic.endswith("/status"):
            self.handle_status(topic, payload)
            return None
        now = self.clock.now()
        try:
            device_id = device_id_from_topic(topic)
            telemetry = parse_payload(payload, now=now, topic_device_id=device_id)
        except PayloadError as exc:
            self.stats["rejected"] += 1
            log.warning("rejected message on %s: %s", topic, exc)
            return None
        self.writer.write(bucket=self.bucket, org=self.org, record=telemetry_point(telemetry))
        self.stats["accepted"] += 1
        log.debug("stored %s", telemetry)
        return telemetry

    def handle_status(self, topic: str, payload: bytes) -> None:
        parts = topic.split("/")
        state = payload.decode("utf-8", "replace").strip().lower()
        valid_topic = len(parts) == 3 and DEVICE_ID_RE.match(parts[1]) is not None
        if not valid_topic or state not in {"online", "offline"}:
            self.stats["rejected"] += 1
            log.warning("ignoring malformed status message on %s", topic)
            return
        self.writer.write(
            bucket=self.bucket,
            org=self.org,
            record=status_point(parts[1], state == "online", self.clock.now()),
        )
        self.stats["status"] += 1
        log.info("device %s reported %s", parts[1], state)


def _install_signal_handlers(stop: threading.Event) -> None:
    def _handle(signum, frame):
        log.info("signal %s received, shutting down", signum)
        stop.set()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)


def main(argv: Sequence[str] | None = None) -> int:
    from influxdb_client import InfluxDBClient, WriteOptions

    logging.basicConfig(
        level=(env_str(os.environ, "LOG_LEVEL", "INFO") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    mqtt_settings = MqttSettings.from_env(client_id="ingest")
    influx_settings = InfluxSettings.from_env()
    if not influx_settings.token:
        log.error("INFLUX_TOKEN is not set")
        return 2

    influx = InfluxDBClient(
        url=influx_settings.url, token=influx_settings.token, org=influx_settings.org
    )
    write_api = influx.write_api(
        write_options=WriteOptions(
            batch_size=200, flush_interval=1_000, retry_interval=5_000, max_retries=5
        )
    )
    service = IngestService(write_api, bucket=influx_settings.bucket, org=influx_settings.org)

    client = build_client(mqtt_settings)
    client.on_connect = service.on_connect
    client.on_message = service.on_message
    log.info("connecting to %s, writing to %s", describe(mqtt_settings), influx_settings.url)
    client.connect_async(mqtt_settings.host, mqtt_settings.port, mqtt_settings.keepalive)
    client.loop_start()

    stop = threading.Event()
    _install_signal_handlers(stop)
    try:
        while not stop.wait(60):
            log.info("stats: %s", dict(service.stats))
    finally:
        client.disconnect()
        client.loop_stop()
        write_api.close()
        influx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
