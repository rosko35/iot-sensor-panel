"""Telemetry payload schema and validation.

Every producer (Python simulator, ESP32 firmware) publishes the same JSON
document to ``sensors/<device_id>/telemetry``::

    {
      "v": 1,                      # schema version (optional, defaults to 1)
      "device_id": "sim-living",   # must match the topic segment
      "ts": 1731930000.5,          # Unix seconds; optional, ingest time is used if absent
      "seq": 42,                   # optional monotonically increasing counter
      "temperature": 21.4,         # degrees Celsius
      "humidity": 45.2,            # relative humidity, percent
      "pressure": 1013.2,          # hPa (BME280 only)
      "motion": false,             # PIR sensor state
      "rssi": -61,                 # WiFi signal strength, dBm
      "fw": "esp32-1.0.0"          # firmware / producer version
    }

At least one sensor metric (temperature, humidity, pressure, motion) is required.
Validation is deliberately strict: a malformed or physically impossible reading
is rejected instead of polluting the time series.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = 1
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_PAYLOAD_BYTES = 2048
# Accept timestamps at most this far in the future (clock skew) or in the past
# (devices buffering readings while offline).
MAX_FUTURE_SKEW_S = 300.0
MAX_PAST_AGE_S = 7 * 24 * 3600.0

# Physical ranges of the supported sensors (DHT22: -40..80 C, BME280: -40..85 C,
# 300..1100 hPa). Anything outside is a wiring fault or a forged message.
RANGES: dict[str, tuple[float, float]] = {
    "temperature": (-40.0, 85.0),
    "humidity": (0.0, 100.0),
    "pressure": (300.0, 1100.0),
    "rssi": (-120.0, 0.0),
}
FLOAT_METRICS = ("temperature", "humidity", "pressure")
METRICS = (*FLOAT_METRICS, "motion")
KNOWN_KEYS = {"v", "device_id", "ts", "seq", "rssi", "fw", *METRICS}


class PayloadError(ValueError):
    """Raised when a telemetry payload is rejected."""


@dataclass(frozen=True)
class Telemetry:
    device_id: str
    ts: float
    temperature: float | None = None
    humidity: float | None = None
    pressure: float | None = None
    motion: bool | None = None
    rssi: int | None = None
    seq: int | None = None
    fw: str | None = None
    ts_from_device: bool = True
    extra_keys: tuple[str, ...] = field(default_factory=tuple)

    def metrics(self) -> dict[str, float]:
        """Numeric metric values present in this reading (motion as 0/1)."""
        out: dict[str, float] = {}
        for name in FLOAT_METRICS:
            value = getattr(self, name)
            if value is not None:
                out[name] = float(value)
        if self.motion is not None:
            out["motion"] = 1.0 if self.motion else 0.0
        return out


def device_id_from_topic(topic: str) -> str:
    """Extract ``<device_id>`` from ``sensors/<device_id>/telemetry``."""
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "sensors" or parts[2] != "telemetry":
        raise PayloadError(f"unexpected topic {topic!r}")
    if not DEVICE_ID_RE.match(parts[1]):
        raise PayloadError(f"invalid device id in topic {topic!r}")
    return parts[1]


def _number(data: dict[str, Any], key: str) -> float | None:
    if key not in data or data[key] is None:
        return None
    value = data[key]
    # bool is a subclass of int; "true" is not a temperature.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise PayloadError(f"{key} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise PayloadError(f"{key} must be finite")
    low, high = RANGES[key]
    if not low <= value <= high:
        raise PayloadError(f"{key}={value} outside physical range [{low}, {high}]")
    return value


def parse_payload(
    raw: bytes | str,
    *,
    now: float,
    topic_device_id: str | None = None,
) -> Telemetry:
    """Parse and validate a raw MQTT payload.

    ``now`` is the receive time (Unix seconds); it is used when the producer
    did not send a timestamp and to reject wildly skewed ones.
    ``topic_device_id`` - when given, the payload must claim the same device as
    the topic it was published on (prevents one device spoofing another even
    when the broker ACL is not enforced).
    """
    if isinstance(raw, bytes | bytearray):
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise PayloadError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
        try:
            text = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PayloadError("payload is not valid UTF-8") from exc
    else:
        text = raw
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise PayloadError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"invalid JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise PayloadError("payload must be a JSON object")

    version = data.get("v", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise PayloadError(f"unsupported schema version {version!r}")

    device_id = data.get("device_id")
    if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
        raise PayloadError("device_id must match [A-Za-z0-9_-]{1,32}")
    if topic_device_id is not None and device_id != topic_device_id:
        raise PayloadError(f"device_id {device_id!r} does not match topic ({topic_device_id!r})")

    ts_from_device = "ts" in data and data["ts"] is not None
    if ts_from_device:
        ts = data["ts"]
        if isinstance(ts, bool) or not isinstance(ts, int | float) or not math.isfinite(ts):
            raise PayloadError("ts must be a Unix timestamp in seconds")
        ts = float(ts)
        if ts > now + MAX_FUTURE_SKEW_S:
            raise PayloadError("ts is too far in the future")
        if ts < now - MAX_PAST_AGE_S:
            raise PayloadError("ts is too old")
    else:
        ts = now

    temperature = _number(data, "temperature")
    humidity = _number(data, "humidity")
    pressure = _number(data, "pressure")

    motion = data.get("motion")
    if motion is not None and not isinstance(motion, bool):
        raise PayloadError("motion must be a boolean")

    rssi_value = _number(data, "rssi")
    rssi = int(rssi_value) if rssi_value is not None else None

    seq = data.get("seq")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 0):
        raise PayloadError("seq must be a non-negative integer")

    fw = data.get("fw")
    if fw is not None and (not isinstance(fw, str) or len(fw) > 64):
        raise PayloadError("fw must be a string of at most 64 characters")

    if temperature is None and humidity is None and pressure is None and motion is None:
        raise PayloadError("payload contains no sensor metric")

    extra = tuple(sorted(set(data) - KNOWN_KEYS))
    return Telemetry(
        device_id=device_id,
        ts=ts,
        temperature=temperature,
        humidity=humidity,
        pressure=pressure,
        motion=motion,
        rssi=rssi,
        seq=seq,
        fw=fw,
        ts_from_device=ts_from_device,
        extra_keys=extra,
    )
