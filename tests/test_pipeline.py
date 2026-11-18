"""Simulator -> ingest -> alert engine, wired together in-process with a fake clock."""

from __future__ import annotations

import json
from types import SimpleNamespace

from iot_panel.alerts import AlertEngine, load_alert_config
from iot_panel.alerts.notifiers import AlertDispatcher
from iot_panel.ingest import IngestService
from iot_panel.simulator import Simulator, parse_silence, parse_spike


def test_simulated_faults_produce_expected_alert_lifecycle(root, clock, writer):
    config = load_alert_config(root / "config/alerts.yaml")
    events = []
    service = IngestService(
        writer,
        bucket="sensors",
        org="iot",
        clock=clock,
        engine=AlertEngine(config, clock),
        dispatcher=AlertDispatcher([SimpleNamespace(notify=events.append)]),
    )
    sim = Simulator(
        ["sim-server-room", "sim-garage"],
        [parse_spike("sim-server-room:60:120:9"), parse_silence("sim-garage:100:120")],
        interval=5,
        seed=11,
        start_ts=clock.now(),
    )
    for _ in range(80):  # 400 simulated seconds
        for topic, body in sim.tick(clock.now()):
            service.on_message(None, None, SimpleNamespace(topic=topic, payload=json.dumps(body)))
        service.check_offline()
        clock.advance(5)

    lifecycle = [(e.device_id, e.rule_id, e.kind) for e in events]
    assert lifecycle == [
        ("sim-server-room", "temp_high", "raised"),
        ("sim-garage", "offline", "raised"),
        ("sim-server-room", "temp_high", "resolved"),
        ("sim-garage", "offline", "resolved"),
    ]
    assert service.stats["rejected"] == 0
