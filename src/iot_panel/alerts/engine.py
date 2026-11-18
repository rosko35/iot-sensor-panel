"""Stateful alert evaluation: thresholds with hysteresis and device-offline detection.

The engine is pure logic: it receives validated telemetry and the current time
(through an injectable clock) and returns :class:`AlertEvent` objects. Delivery
(Telegram, console, MQTT, InfluxDB) is handled by sinks, so the state machine
can be unit-tested with a fake clock and no network.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass
from typing import Any

from ..clock import Clock, SystemClock
from ..payload import Telemetry
from .config import AlertConfig, ThresholdRule

log = logging.getLogger(__name__)

OFFLINE_RULE_ID = "offline"
RAISED = "raised"
RESOLVED = "resolved"
UNITS = {"temperature": " °C", "humidity": " %", "pressure": " hPa", "motion": ""}


@dataclass(frozen=True)
class AlertEvent:
    kind: str  # "raised" | "resolved"
    device_id: str
    rule_id: str
    metric: str  # sensor metric, or "last_seen" for offline alerts
    severity: str
    value: float
    threshold: float
    ts: float
    message: str

    @property
    def active(self) -> bool:
        return self.kind == RAISED

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["active"] = self.active
        return data


def _fmt(value: float, metric: str) -> str:
    return f"{value:.1f}{UNITS.get(metric, '')}"


class AlertEngine:
    """Tracks which alerts are active per (device, rule) and emits transitions only."""

    def __init__(self, config: AlertConfig, clock: Clock | None = None) -> None:
        self.config = config
        self.clock = clock or SystemClock()
        self.started_at = self.clock.now()
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], AlertEvent] = {}
        self._last_seen: dict[str, float] = {}

    # -- public API -----------------------------------------------------------------

    def process(self, telemetry: Telemetry) -> list[AlertEvent]:
        """Evaluate a validated reading; returns raised/resolved transitions."""
        now = self.clock.now()
        events: list[AlertEvent] = []
        device_id = telemetry.device_id
        with self._lock:
            previous = self._last_seen.get(device_id)
            self._last_seen[device_id] = now
            offline = self._active.pop((device_id, OFFLINE_RULE_ID), None)
            if offline is not None:
                silent_for = now - (previous if previous is not None else self.started_at)
                events.append(
                    AlertEvent(
                        RESOLVED,
                        device_id,
                        OFFLINE_RULE_ID,
                        "last_seen",
                        offline.severity,
                        silent_for,
                        offline.threshold,
                        now,
                        f"[RESOLVED] {device_id} is back online after {silent_for:.0f} s",
                    )
                )

            metrics = telemetry.metrics()
            for rule in self.config.rules_for(device_id):
                value = metrics.get(rule.metric)
                if value is None:
                    continue
                key = (device_id, rule.id)
                was_active = key in self._active
                is_active = rule.evaluate(value, was_active)
                if is_active and not was_active:
                    event = self._threshold_event(RAISED, device_id, rule, value, now)
                    self._active[key] = event
                    events.append(event)
                elif was_active and not is_active:
                    del self._active[key]
                    events.append(self._threshold_event(RESOLVED, device_id, rule, value, now))
        return events

    def check_offline(self) -> list[AlertEvent]:
        """Raise an offline alert for every device silent longer than its timeout."""
        now = self.clock.now()
        events: list[AlertEvent] = []
        with self._lock:
            devices = set(self._last_seen) | set(self.config.expected_devices)
            for device_id in sorted(devices):
                key = (device_id, OFFLINE_RULE_ID)
                if key in self._active:
                    continue
                last = self._last_seen.get(device_id, self.started_at)
                timeout = self.config.offline_timeout_for(device_id)
                silent_for = now - last
                if silent_for >= timeout:
                    never = device_id not in self._last_seen
                    detail = "never reported" if never else f"no data for {silent_for:.0f} s"
                    event = AlertEvent(
                        RAISED,
                        device_id,
                        OFFLINE_RULE_ID,
                        "last_seen",
                        self.config.offline_severity,
                        silent_for,
                        timeout,
                        now,
                        f"[{self.config.offline_severity.upper()}] {device_id} is offline: "
                        f"{detail} (timeout {timeout:.0f} s)",
                    )
                    self._active[key] = event
                    events.append(event)
        return events

    def restore(self, event: AlertEvent) -> None:
        """Re-seed an alert that was active before a restart (from retained MQTT state).

        Restored alerts are not re-announced; they resolve normally once the
        condition clears, which avoids alerts that stay "active" forever in the
        dashboard after the service restarted mid-incident.
        """
        if not event.active:
            return
        with self._lock:
            key = (event.device_id, event.rule_id)
            if key in self._active:
                return
            if event.rule_id != OFFLINE_RULE_ID:
                rules = {r.id for r in self.config.rules_for(event.device_id)}
                if event.rule_id not in rules:
                    log.info("dropping restored alert for unknown rule %s", event.rule_id)
                    return
            self._active[key] = event
            log.info("restored active alert %s/%s", event.device_id, event.rule_id)

    def active_alerts(self) -> list[AlertEvent]:
        with self._lock:
            return list(self._active.values())

    def last_seen(self, device_id: str) -> float | None:
        with self._lock:
            return self._last_seen.get(device_id)

    # -- helpers --------------------------------------------------------------------

    @staticmethod
    def _threshold_event(
        kind: str, device_id: str, rule: ThresholdRule, value: float, now: float
    ) -> AlertEvent:
        metric = rule.metric
        if kind == RAISED:
            message = (
                f"[{rule.severity.upper()}] {device_id}: {metric} {_fmt(value, metric)} "
                f"is {rule.direction} {_fmt(rule.threshold, metric)} ({rule.id})"
            )
        else:
            message = (
                f"[RESOLVED] {device_id}: {metric} back to {_fmt(value, metric)} "
                f"({rule.id}, cleared at {_fmt(rule.clear_level, metric)})"
            )
        return AlertEvent(
            kind, device_id, rule.id, metric, rule.severity, value, rule.threshold, now, message
        )


def event_from_dict(data: dict[str, Any]) -> AlertEvent:
    """Inverse of :meth:`AlertEvent.to_dict` (used to restore retained alert state)."""
    return AlertEvent(
        kind=str(data["kind"]),
        device_id=str(data["device_id"]),
        rule_id=str(data["rule_id"]),
        metric=str(data["metric"]),
        severity=str(data["severity"]),
        value=float(data["value"]),
        threshold=float(data["threshold"]),
        ts=float(data["ts"]),
        message=str(data["message"]),
    )
