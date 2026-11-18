from __future__ import annotations

import json

import pytest

from iot_panel.payload import (
    MAX_PAYLOAD_BYTES,
    PayloadError,
    device_id_from_topic,
    parse_payload,
)

from conftest import payload

NOW = 1_750_000_000.0


def test_valid_payload_roundtrip():
    t = parse_payload(payload(pressure=1009.4), now=NOW, topic_device_id="dev-1")
    assert t.device_id == "dev-1"
    assert t.temperature == 21.5
    assert t.humidity == 40.0
    assert t.pressure == 1009.4
    assert t.motion is False
    assert t.rssi == -60
    assert t.ts == NOW and t.ts_from_device
    assert t.metrics() == {
        "temperature": 21.5,
        "humidity": 40.0,
        "pressure": 1009.4,
        "motion": 0.0,
    }


def test_missing_timestamp_uses_receive_time():
    t = parse_payload(payload(ts=...), now=NOW + 5)
    assert t.ts == NOW + 5
    assert not t.ts_from_device


def test_integer_values_are_accepted_as_numbers():
    t = parse_payload(payload(temperature=22), now=NOW)
    assert t.temperature == 22.0


def test_partial_payload_with_single_metric():
    raw = json.dumps({"device_id": "pir-1", "motion": True}).encode()
    t = parse_payload(raw, now=NOW)
    assert t.motion is True and t.temperature is None
    assert t.metrics() == {"motion": 1.0}


def test_unknown_keys_are_reported_not_rejected():
    t = parse_payload(payload(battery=3.7), now=NOW)
    assert t.extra_keys == ("battery",)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"not json", "invalid JSON"),
        (b"[1, 2]", "JSON object"),
        (b"\xff\xfe", "UTF-8"),
        (payload(device_id="bad id!"), "device_id"),
        (payload(device_id=...), "device_id"),
        (payload(v=2), "schema version"),
        (payload(temperature="21.5"), "temperature must be a number"),
        (payload(temperature=True), "temperature must be a number"),
        (payload(temperature=150.0), "outside physical range"),
        (payload(humidity=-1), "outside physical range"),
        (payload(pressure=50), "outside physical range"),
        (payload(motion="yes"), "motion must be a boolean"),
        (payload(seq=-1), "seq"),
        (payload(fw="x" * 65), "fw"),
        (payload(ts="yesterday"), "ts must be"),
        (payload(ts=NOW + 3600), "future"),
        (payload(ts=NOW - 30 * 24 * 3600), "too old"),
        (
            payload(temperature=..., humidity=..., motion=..., pressure=...),
            "no sensor metric",
        ),
    ],
)
def test_invalid_payloads_are_rejected(raw, message):
    with pytest.raises(PayloadError, match=message):
        parse_payload(raw, now=NOW)


def test_nan_is_rejected():
    raw = b'{"device_id": "dev-1", "temperature": NaN}'
    with pytest.raises(PayloadError, match="finite"):
        parse_payload(raw, now=NOW)


def test_oversized_payload_is_rejected():
    raw = payload(fw="x") + b" " * MAX_PAYLOAD_BYTES
    with pytest.raises(PayloadError, match="larger than"):
        parse_payload(raw, now=NOW)


def test_device_cannot_publish_for_another_device():
    with pytest.raises(PayloadError, match="does not match topic"):
        parse_payload(payload(device_id="dev-2"), now=NOW, topic_device_id="dev-1")


@pytest.mark.parametrize(
    "topic",
    ["sensors/dev-1", "sensors/dev-1/status", "foo/dev-1/telemetry", "sensors/a b/telemetry"],
)
def test_bad_topics(topic):
    with pytest.raises(PayloadError):
        device_id_from_topic(topic)


def test_topic_parsing():
    assert device_id_from_topic("sensors/esp32-kitchen/telemetry") == "esp32-kitchen"
