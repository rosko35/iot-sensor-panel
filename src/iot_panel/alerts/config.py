"""Alert rule configuration (YAML) with per-device overrides."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from ..payload import DEVICE_ID_RE, METRICS

SEVERITIES = ("info", "warning", "critical")
RULE_KEYS = {"id", "metric", "above", "below", "hysteresis", "severity", "enabled"}


class AlertConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ThresholdRule:
    id: str
    metric: str
    above: float | None = None
    below: float | None = None
    hysteresis: float = 0.0
    severity: str = "warning"
    enabled: bool = True

    def __post_init__(self) -> None:
        if not self.id or not DEVICE_ID_RE.match(self.id):
            raise AlertConfigError(f"invalid rule id {self.id!r}")
        if self.metric not in METRICS:
            raise AlertConfigError(f"rule {self.id}: unknown metric {self.metric!r}")
        if (self.above is None) == (self.below is None):
            raise AlertConfigError(f"rule {self.id}: set exactly one of 'above' or 'below'")
        for name in ("above", "below", "hysteresis"):
            value = getattr(self, name)
            if (value is not None or name == "hysteresis") and (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise AlertConfigError(f"rule {self.id}: {name} must be a number")
        if self.hysteresis < 0:
            raise AlertConfigError(f"rule {self.id}: hysteresis must be >= 0")
        if self.severity not in SEVERITIES:
            raise AlertConfigError(f"rule {self.id}: severity must be one of {SEVERITIES}")

    @property
    def threshold(self) -> float:
        return float(self.above if self.above is not None else self.below)  # type: ignore[arg-type]

    @property
    def direction(self) -> str:
        return "above" if self.above is not None else "below"

    @property
    def clear_level(self) -> float:
        """Value the reading has to cross back over for the alert to resolve."""
        if self.above is not None:
            return self.above - self.hysteresis
        return self.threshold + self.hysteresis

    def evaluate(self, value: float, currently_active: bool) -> bool:
        """Return the new alert state for ``value`` given the current state.

        Raising uses the threshold itself; resolving requires crossing back
        over ``clear_level``. Between the two the state does not change, which
        is what prevents flapping.
        """
        if self.above is not None:
            if currently_active:
                return value >= self.clear_level
            return value > self.above
        if currently_active:
            return value <= self.clear_level
        return value < self.threshold


@dataclass(frozen=True)
class AlertConfig:
    rules: tuple[ThresholdRule, ...] = ()
    offline_timeout: float = 60.0
    check_interval: float = 5.0
    offline_severity: str = "critical"
    expected_devices: tuple[str, ...] = ()
    device_rules: Mapping[str, tuple[ThresholdRule, ...]] = field(default_factory=dict)
    device_offline_timeout: Mapping[str, float] = field(default_factory=dict)

    def rules_for(self, device_id: str) -> tuple[ThresholdRule, ...]:
        rules = self.device_rules.get(device_id, self.rules)
        return tuple(r for r in rules if r.enabled)

    def offline_timeout_for(self, device_id: str) -> float:
        return self.device_offline_timeout.get(device_id, self.offline_timeout)


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise AlertConfigError(f"{name} must be a positive number")
    return float(value)


def _rule_from_mapping(data: Mapping[str, Any], *, rule_id: str | None = None) -> ThresholdRule:
    if not isinstance(data, Mapping):
        raise AlertConfigError("each rule must be a mapping")
    unknown = set(data) - RULE_KEYS
    if unknown:
        raise AlertConfigError(f"rule {data.get('id', rule_id)}: unknown keys {sorted(unknown)}")
    values = dict(data)
    if rule_id is not None:
        values["id"] = rule_id
    if "id" not in values or "metric" not in values:
        raise AlertConfigError(f"rule {values.get('id')}: 'id' and 'metric' are required")
    return ThresholdRule(**values)


def _apply_override(base: ThresholdRule, override: Mapping[str, Any]) -> ThresholdRule:
    unknown = set(override) - RULE_KEYS
    if unknown:
        raise AlertConfigError(f"override for {base.id}: unknown keys {sorted(unknown)}")
    changes = dict(override)
    changes.pop("id", None)
    # Switching direction: setting `below` on an `above` rule (or vice versa)
    # replaces the other bound instead of failing validation.
    if "above" in changes and "below" not in changes:
        changes["below"] = None
    if "below" in changes and "above" not in changes:
        changes["above"] = None
    return replace(base, **changes)


def parse_alert_config(data: Mapping[str, Any] | None) -> AlertConfig:
    data = data or {}
    if not isinstance(data, Mapping):
        raise AlertConfigError("alert config must be a mapping")
    unknown = set(data) - {"offline", "rules", "devices"}
    if unknown:
        raise AlertConfigError(f"unknown top-level keys {sorted(unknown)}")

    offline = data.get("offline") or {}
    timeout = _positive(offline.get("timeout_seconds", 60), "offline.timeout_seconds")
    interval = _positive(offline.get("check_interval_seconds", 5), "offline.check_interval_seconds")
    severity = offline.get("severity", "critical")
    if severity not in SEVERITIES:
        raise AlertConfigError(f"offline.severity must be one of {SEVERITIES}")
    expected = tuple(offline.get("expected_devices") or ())
    for device_id in expected:
        if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
            raise AlertConfigError(f"invalid expected device id {device_id!r}")

    rules = tuple(_rule_from_mapping(r) for r in data.get("rules") or ())
    ids = [r.id for r in rules]
    if len(ids) != len(set(ids)):
        raise AlertConfigError("rule ids must be unique")

    device_rules: dict[str, tuple[ThresholdRule, ...]] = {}
    device_timeouts: dict[str, float] = {}
    for device_id, device_cfg in (data.get("devices") or {}).items():
        if not isinstance(device_id, str) or not DEVICE_ID_RE.match(device_id):
            raise AlertConfigError(f"invalid device id {device_id!r}")
        device_cfg = device_cfg or {}
        if "offline_timeout_seconds" in device_cfg:
            device_timeouts[device_id] = _positive(
                device_cfg["offline_timeout_seconds"],
                f"devices.{device_id}.offline_timeout_seconds",
            )
        overrides = device_cfg.get("rules") or {}
        if not isinstance(overrides, Mapping):
            raise AlertConfigError(f"devices.{device_id}.rules must be a mapping by rule id")
        merged = {r.id: r for r in rules}
        for rule_id, override in overrides.items():
            override = override or {}
            if rule_id in merged:
                merged[rule_id] = _apply_override(merged[rule_id], override)
            elif "metric" in override:
                merged[rule_id] = _rule_from_mapping(override, rule_id=rule_id)
            else:
                raise AlertConfigError(
                    f"devices.{device_id}: rule {rule_id!r} is not a global rule; "
                    "give a full definition (with 'metric') to add a device-only rule"
                )
        if overrides:
            device_rules[device_id] = tuple(merged.values())

    return AlertConfig(
        rules=rules,
        offline_timeout=timeout,
        check_interval=interval,
        offline_severity=severity,
        expected_devices=expected,
        device_rules=device_rules,
        device_offline_timeout=device_timeouts,
    )


def load_alert_config(path: str | Path) -> AlertConfig:
    with Path(path).open(encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            raise AlertConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_alert_config(data)
