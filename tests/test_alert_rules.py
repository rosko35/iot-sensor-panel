"""Threshold rules, hysteresis and the alert state machine."""

from __future__ import annotations

import pytest

from iot_panel.alerts import AlertConfig, AlertEngine, ThresholdRule
from iot_panel.alerts.config import AlertConfigError
from iot_panel.payload import Telemetry

HIGH = ThresholdRule("temp_high", "temperature", above=30.0, hysteresis=1.0)
LOW = ThresholdRule("temp_low", "temperature", below=5.0, hysteresis=1.0)


def reading(device="dev-1", **metrics):
    return Telemetry(device_id=device, ts=0.0, **metrics)


def run_sequence(engine, values, device="dev-1", metric="temperature"):
    """Feed values and return the kinds of the events produced per step."""
    out = []
    for value in values:
        events = engine.process(reading(device, **{metric: value}))
        out.append(",".join(f"{e.rule_id}:{e.kind}" for e in events) or "-")
    return out


@pytest.mark.parametrize(
    ("value", "active", "expected"),
    [
        (29.9, False, False),
        (30.0, False, False),  # strictly above
        (30.1, False, True),
        (29.5, True, True),  # inside the hysteresis band: stays active
        (29.0, True, True),  # exactly at clear level: still active
        (28.9, True, False),
    ],
)
def test_above_rule_evaluation(value, active, expected):
    assert HIGH.evaluate(value, active) is expected


@pytest.mark.parametrize(
    ("value", "active", "expected"),
    [
        (5.0, False, False),
        (4.9, False, True),
        (5.5, True, True),
        (6.0, True, True),
        (6.1, True, False),
    ],
)
def test_below_rule_evaluation(value, active, expected):
    assert LOW.evaluate(value, active) is expected


def test_clear_levels():
    assert HIGH.clear_level == 29.0
    assert LOW.clear_level == 6.0


def test_hysteresis_prevents_flapping(clock):
    engine = AlertEngine(AlertConfig(rules=(HIGH,)), clock)
    noisy = [29.8, 30.2, 29.9, 30.3, 29.6, 30.1, 29.4, 28.8, 29.5, 30.05]
    assert run_sequence(engine, noisy) == [
        "-",
        "temp_high:raised",
        "-",
        "-",
        "-",
        "-",
        "-",
        "temp_high:resolved",
        "-",
        "temp_high:raised",
    ]


def test_without_hysteresis_the_same_signal_flaps(clock):
    rule = ThresholdRule("temp_high", "temperature", above=30.0, hysteresis=0.0)
    engine = AlertEngine(AlertConfig(rules=(rule,)), clock)
    events = run_sequence(engine, [29.8, 30.2, 29.9, 30.3, 29.6])
    assert events.count("temp_high:raised") == 2
    assert events.count("temp_high:resolved") == 2


def test_event_contents(clock):
    engine = AlertEngine(AlertConfig(rules=(HIGH,)), clock)
    (raised,) = engine.process(reading(temperature=31.26))
    assert raised.active and raised.severity == "warning"
    assert raised.value == 31.26 and raised.threshold == 30.0
    assert raised.ts == clock.now()
    assert raised.message == "[WARNING] dev-1: temperature 31.3 °C is above 30.0 °C (temp_high)"
    assert engine.active_alerts() == [raised]

    clock.advance(10)
    (resolved,) = engine.process(reading(temperature=28.0))
    assert not resolved.active
    assert "back to 28.0 °C" in resolved.message
    assert resolved.to_dict()["active"] is False
    assert engine.active_alerts() == []


def test_alerts_are_tracked_per_device(clock):
    engine = AlertEngine(AlertConfig(rules=(HIGH,)), clock)
    assert run_sequence(engine, [31], device="a") == ["temp_high:raised"]
    assert run_sequence(engine, [31], device="b") == ["temp_high:raised"]
    assert run_sequence(engine, [20], device="a") == ["temp_high:resolved"]
    assert [e.device_id for e in engine.active_alerts()] == ["b"]


def test_readings_without_the_metric_do_not_change_state(clock):
    engine = AlertEngine(AlertConfig(rules=(HIGH,)), clock)
    engine.process(reading(temperature=35))
    assert engine.process(reading(humidity=50.0)) == []
    assert len(engine.active_alerts()) == 1


def test_high_and_low_rules_can_both_apply(clock):
    engine = AlertEngine(AlertConfig(rules=(HIGH, LOW)), clock)
    assert run_sequence(engine, [3.0, 7.0, 31.0]) == [
        "temp_low:raised",
        "temp_low:resolved",
        "temp_high:raised",
    ]


def test_motion_rule(clock):
    rule = ThresholdRule("motion_seen", "motion", above=0.5, severity="info")
    engine = AlertEngine(AlertConfig(rules=(rule,)), clock)
    assert run_sequence(engine, [False, True, True, False], metric="motion") == [
        "-",
        "motion_seen:raised",
        "-",
        "motion_seen:resolved",
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"above": 1.0, "below": 0.0},
        {},
        {"above": 1.0, "hysteresis": -1.0},
        {"above": "hot"},
        {"above": 1.0, "severity": "panic"},
        {"above": 1.0, "metric": "voltage"},
        {"above": 1.0, "hysteresis": None},
    ],
)
def test_invalid_rules(kwargs):
    params = {"id": "r", "metric": "temperature", **kwargs}
    with pytest.raises(AlertConfigError):
        ThresholdRule(**params)
