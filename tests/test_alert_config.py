from __future__ import annotations

import pytest
import yaml

from iot_panel.alerts import AlertConfigError, load_alert_config
from iot_panel.alerts.config import parse_alert_config


def test_shipped_config_loads(root):
    config = load_alert_config(root / "config/alerts.yaml")
    assert {r.id for r in config.rules} == {"temp_high", "temp_low", "humidity_high"}
    assert config.offline_timeout == 60
    assert config.offline_timeout_for("sim-server-room") == 30
    server = {r.id: r for r in config.rules_for("sim-server-room")}
    assert server["temp_high"].above == 28.0
    assert server["temp_high"].severity == "critical"
    assert server["temp_low"].below == 5.0  # inherited untouched
    # devices without overrides use the global rules
    assert config.rules_for("esp32-anything") == config.rules


def test_override_can_disable_and_add_rules():
    config = parse_alert_config(
        yaml.safe_load(
            """
            rules:
              - {id: temp_high, metric: temperature, above: 30, hysteresis: 1}
            devices:
              fridge:
                rules:
                  temp_high: {enabled: false}
                  fridge_warm: {metric: temperature, above: 8, hysteresis: 0.5}
              lab:
                rules:
                  temp_high: {below: 15}
            """
        )
    )
    assert [r.id for r in config.rules_for("fridge")] == ["fridge_warm"]
    (lab,) = config.rules_for("lab")
    assert lab.direction == "below" and lab.threshold == 15


def test_empty_config_uses_defaults():
    config = parse_alert_config(None)
    assert config.rules == ()
    assert config.offline_timeout == 60


@pytest.mark.parametrize(
    "text",
    [
        "foo: 1",
        "offline: {timeout_seconds: 0}",
        "offline: {severity: loud}",
        "offline: {expected_devices: ['bad id']}",
        "rules: [{id: a, metric: temperature}]",
        "rules: [{id: a, metric: temperature, above: 1, colour: red}]",
        "rules: [{id: a, metric: temperature, above: 1}, {id: a, metric: humidity, above: 1}]",
        "rules: [{metric: temperature, above: 1}]",
        "devices: {d: {rules: {nope: {above: 3}}}}",
        "devices: {d: {offline_timeout_seconds: -5}}",
        "devices: {'bad id': {}}",
    ],
)
def test_invalid_configs(text):
    with pytest.raises(AlertConfigError):
        parse_alert_config(yaml.safe_load(text))


def test_invalid_yaml_file(tmp_path):
    path = tmp_path / "alerts.yaml"
    path.write_text("rules: [unclosed", encoding="utf-8")
    with pytest.raises(AlertConfigError, match="invalid YAML"):
        load_alert_config(path)
