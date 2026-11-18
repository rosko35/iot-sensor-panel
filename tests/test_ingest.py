from __future__ import annotations

from types import SimpleNamespace

from iot_panel.ingest import IngestService, telemetry_point

from conftest import payload


def make_service(writer, clock):
    return IngestService(writer, bucket="sensors", org="iot", clock=clock)


def msg(topic: str, body: bytes):
    return SimpleNamespace(topic=topic, payload=body)


def test_valid_message_is_written_as_line_protocol(writer, clock):
    svc = make_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(motion=True)))
    assert svc.stats["accepted"] == 1
    bucket, org, _ = writer.records[0]
    assert (bucket, org) == ("sensors", "iot")
    line = writer.lines[0]
    assert line.startswith("telemetry,device_id=dev-1 ")
    assert "temperature=21.5" in line
    assert "humidity=40" in line
    assert "motion=1i" in line
    assert "rssi=-60i" in line
    assert line.endswith(" 1750000000000000000")


def test_invalid_message_is_counted_and_not_written(writer, clock):
    svc = make_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(temperature=999)))
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", b"{garbage"))
    assert writer.records == []
    assert svc.stats["rejected"] == 2


def test_spoofed_device_id_is_rejected(writer, clock):
    svc = make_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-2/telemetry", payload(device_id="dev-1")))
    assert writer.records == []
    assert svc.stats["rejected"] == 1


def test_status_messages_are_recorded(writer, clock):
    svc = make_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/status", b"offline"))
    svc.on_message(None, None, msg("sensors/dev-1/status", b"weird"))
    assert len(writer.lines) == 1
    assert writer.lines[0].startswith("device_status,device_id=dev-1 online=0i")
    assert svc.stats["rejected"] == 1


def test_writer_failure_does_not_crash_the_callback(clock):
    class BrokenWriter:
        def write(self, **kwargs):
            raise ConnectionError("influx down")

    svc = make_service(BrokenWriter(), clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload()))
    assert svc.stats["errors"] == 1


def test_on_connect_subscribes_to_telemetry_and_status():
    calls = []
    client = SimpleNamespace(subscribe=lambda topics: calls.append(topics))
    rc = SimpleNamespace(is_failure=False)
    IngestService(None, bucket="b", org="o").on_connect(client, None, None, rc)
    assert calls == [[("sensors/+/telemetry", 1), ("sensors/+/status", 1)]]


def test_on_connect_failure_does_not_subscribe():
    client = SimpleNamespace(subscribe=lambda topics: (_ for _ in ()).throw(AssertionError))
    rc = SimpleNamespace(is_failure=True)
    IngestService(None, bucket="b", org="o").on_connect(client, None, None, rc)


def test_point_without_optional_fields(clock):
    from iot_panel.payload import Telemetry

    line = telemetry_point(Telemetry(device_id="d", ts=1.5, humidity=50.0)).to_line_protocol()
    assert line == "telemetry,device_id=d humidity=50 1500000000"


# --------------------------------------------------------------------------- with alerts


def make_alerting_service(writer, clock):
    from iot_panel.alerts import AlertConfig, AlertEngine, ThresholdRule
    from iot_panel.alerts.notifiers import AlertDispatcher, InfluxAlertSink, MqttAlertSink

    published = []
    mqtt_client = SimpleNamespace(publish=lambda *a, **k: published.append(a[0]))
    rule = ThresholdRule("temp_high", "temperature", above=30, hysteresis=1)
    engine = AlertEngine(AlertConfig(rules=(rule,), offline_timeout=60), clock)
    dispatcher = AlertDispatcher(
        [MqttAlertSink(mqtt_client), InfluxAlertSink(writer, bucket="sensors", org="iot")]
    )
    svc = IngestService(
        writer, bucket="sensors", org="iot", clock=clock, engine=engine, dispatcher=dispatcher
    )
    return svc, published


def test_threshold_alert_flows_to_mqtt_and_influx(writer, clock):
    svc, published = make_alerting_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(temperature=35.0)))
    assert published == ["alerts/dev-1/temp_high"]
    assert [line.split(",")[0] for line in writer.lines] == ["telemetry", "alerts"]
    assert "active=1i" in writer.lines[1]
    assert svc.stats["alerts"] == 1


def test_rejected_payload_never_reaches_the_alert_engine(writer, clock):
    svc, published = make_alerting_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(temperature=99.0)))
    assert published == [] and writer.records == []


def test_offline_check_and_recovery_through_the_service(writer, clock):
    svc, published = make_alerting_service(writer, clock)
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(ts=...)))
    clock.advance(61)
    svc.check_offline()
    assert published == ["alerts/dev-1/offline"]
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(ts=...)))
    assert published == ["alerts/dev-1/offline", "alerts/dev-1/offline"]
    assert "active=0i" in writer.lines[-1]


def test_retained_alert_state_is_restored(writer, clock):
    import json

    svc, published = make_alerting_service(writer, clock)
    state = {
        "kind": "raised",
        "device_id": "dev-1",
        "rule_id": "temp_high",
        "metric": "temperature",
        "severity": "warning",
        "value": 33.0,
        "threshold": 30.0,
        "ts": 1.0,
        "message": "x",
        "active": True,
    }
    retained = SimpleNamespace(topic="alerts/dev-1/temp_high", payload=json.dumps(state).encode())
    retained.retain = True
    svc.on_message(None, None, retained)
    svc.on_message(None, None, SimpleNamespace(topic="alerts/x/y", payload=b"{", retain=True))
    assert len(svc.engine.active_alerts()) == 1
    svc.on_message(None, None, msg("sensors/dev-1/telemetry", payload(temperature=20.0)))
    assert published == ["alerts/dev-1/temp_high"]  # resolved, not re-raised


def test_subscribes_to_alert_topics_when_alerting(writer, clock):
    svc, _ = make_alerting_service(writer, clock)
    calls = []
    svc.on_connect(
        SimpleNamespace(subscribe=calls.append), None, None, SimpleNamespace(is_failure=False)
    )
    assert ("alerts/+/+", 1) in calls[0]
