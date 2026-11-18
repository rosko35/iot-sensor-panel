"""Device-offline detection driven by a fake clock."""

from __future__ import annotations

from iot_panel.alerts import OFFLINE_RULE_ID, AlertConfig, AlertEngine
from iot_panel.alerts.engine import AlertEvent
from iot_panel.payload import Telemetry


def seen(engine, device="dev-1"):
    return engine.process(Telemetry(device_id=device, ts=0.0, temperature=20.0))


def kinds(events):
    return [(e.device_id, e.rule_id, e.kind) for e in events]


def test_device_goes_offline_after_timeout_and_recovers(clock):
    engine = AlertEngine(AlertConfig(offline_timeout=60), clock)
    seen(engine)
    clock.advance(59)
    assert engine.check_offline() == []

    clock.advance(1)
    (event,) = engine.check_offline()
    assert (event.rule_id, event.kind, event.severity) == (OFFLINE_RULE_ID, "raised", "critical")
    assert event.value == 60 and event.threshold == 60
    assert event.message == "[CRITICAL] dev-1 is offline: no data for 60 s (timeout 60 s)"

    # no duplicate notifications while it stays offline
    clock.advance(300)
    assert engine.check_offline() == []

    (recovered,) = seen(engine)
    assert recovered.kind == "resolved" and recovered.rule_id == OFFLINE_RULE_ID
    assert recovered.value == 360
    assert "back online after 360 s" in recovered.message
    assert engine.active_alerts() == []


def test_regular_traffic_never_triggers_offline(clock):
    engine = AlertEngine(AlertConfig(offline_timeout=30), clock)
    for _ in range(100):
        seen(engine)
        clock.advance(10)
        assert engine.check_offline() == []


def test_per_device_timeouts(clock):
    config = AlertConfig(offline_timeout=60, device_offline_timeout={"fast": 15})
    engine = AlertEngine(config, clock)
    seen(engine, "fast")
    seen(engine, "slow")
    clock.advance(20)
    assert kinds(engine.check_offline()) == [("fast", "offline", "raised")]
    clock.advance(40)
    assert kinds(engine.check_offline()) == [("slow", "offline", "raised")]


def test_expected_device_that_never_reports(clock):
    engine = AlertEngine(AlertConfig(offline_timeout=60, expected_devices=("esp32-1",)), clock)
    clock.advance(30)
    assert engine.check_offline() == []
    clock.advance(30)
    (event,) = engine.check_offline()
    assert event.device_id == "esp32-1"
    assert "never reported" in event.message


def test_unknown_devices_are_not_reported_before_first_message(clock):
    engine = AlertEngine(AlertConfig(offline_timeout=10), clock)
    clock.advance(1000)
    assert engine.check_offline() == []


def test_offline_and_threshold_alerts_are_independent(clock):
    from iot_panel.alerts import ThresholdRule

    rule = ThresholdRule("temp_high", "temperature", above=30, hysteresis=1)
    engine = AlertEngine(AlertConfig(rules=(rule,), offline_timeout=60), clock)
    engine.process(Telemetry(device_id="d", ts=0.0, temperature=35.0))
    clock.advance(61)
    engine.check_offline()
    assert {e.rule_id for e in engine.active_alerts()} == {"temp_high", "offline"}
    events = engine.process(Telemetry(device_id="d", ts=0.0, temperature=20.0))
    assert kinds(events) == [("d", "offline", "resolved"), ("d", "temp_high", "resolved")]


def test_restore_resumes_active_alert_without_renotifying(clock):
    from iot_panel.alerts import ThresholdRule

    rule = ThresholdRule("temp_high", "temperature", above=30, hysteresis=1)
    engine = AlertEngine(AlertConfig(rules=(rule,)), clock)
    restored = AlertEvent("raised", "d", "temp_high", "temperature", "warning", 33, 30, 0, "x")
    engine.restore(restored)
    engine.restore(AlertEvent("raised", "d", "gone", "humidity", "info", 1, 1, 0, "x"))
    engine.restore(AlertEvent("resolved", "e", "temp_high", "temperature", "info", 1, 1, 0, "x"))
    assert engine.active_alerts() == [restored]
    # still hot: no new "raised" event
    assert engine.process(Telemetry(device_id="d", ts=0.0, temperature=31.0)) == []
    # cooled down while we were restarting: resolved normally
    assert kinds(engine.process(Telemetry(device_id="d", ts=0.0, temperature=25.0))) == [
        ("d", "temp_high", "resolved")
    ]
