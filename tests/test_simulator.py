from __future__ import annotations

import io
import json
import statistics

import pytest

from iot_panel.clock import FakeClock
from iot_panel.payload import parse_payload
from iot_panel.settings import MqttSettings
from iot_panel.simulator import (
    DEFAULT_PROFILES,
    MqttPublisher,
    SensorModel,
    Simulator,
    StdoutPublisher,
    main,
    parse_silence,
    parse_spike,
    profile_for,
    run,
)

START = 1_750_000_000.0
DEVICES = list(DEFAULT_PROFILES)


def collect(sim: Simulator, ticks: int, start: float = START):
    out = []
    for i in range(ticks):
        out.append(sim.tick(start + i * sim.interval))
    return out


def test_output_shape_and_validity():
    sim = Simulator(DEVICES, interval=5, seed=42)
    (messages,) = collect(sim, 1)
    assert len(messages) == len(DEVICES)
    for topic, body in messages:
        device_id = body["device_id"]
        assert topic == f"sensors/{device_id}/telemetry"
        assert {"v", "device_id", "ts", "seq", "temperature", "humidity", "motion"} <= body.keys()
        assert isinstance(body["motion"], bool)
        assert ("pressure" in body) == DEFAULT_PROFILES[device_id].has_pressure
        # every simulated message must pass the same validation as real ones
        raw = json.dumps(body).encode()
        parsed = parse_payload(raw, now=START, topic_device_id=device_id)
        assert parsed.temperature == body["temperature"]


def test_seed_makes_output_deterministic():
    a = collect(Simulator(DEVICES, seed=7), 5)
    b = collect(Simulator(DEVICES, seed=7), 5)
    c = collect(Simulator(DEVICES, seed=8), 5)
    assert a == b
    assert a != c


def test_sequence_numbers_increase():
    sim = Simulator(["sim-garage"], seed=1)
    seqs = [msgs[0][1]["seq"] for msgs in collect(sim, 4)]
    assert seqs == [1, 2, 3, 4]


def test_diurnal_cycle_peaks_in_the_afternoon():
    assert SensorModel.diurnal(15 * 3600) == pytest.approx(1.0)
    assert SensorModel.diurnal(3 * 3600) == pytest.approx(-1.0)


def test_greenhouse_is_warmer_by_day_than_at_night():
    import random

    model = SensorModel(DEFAULT_PROFILES["sim-greenhouse"], random.Random(0))
    day = statistics.mean(model.sample(15 * 3600 + i, 5)["temperature"] for i in range(50))
    night = statistics.mean(model.sample(3 * 3600 + i, 5)["temperature"] for i in range(50))
    assert day - night > 8  # amplitude 6 => ~12 degrees swing


def test_values_stay_within_physical_ranges_over_a_simulated_week():
    sim = Simulator(DEVICES, interval=600, seed=3)
    for messages in collect(sim, 7 * 24 * 6):
        for _, body in messages:
            assert -40 <= body["temperature"] <= 85
            assert 0 <= body["humidity"] <= 100
            if "pressure" in body:
                assert 300 <= body["pressure"] <= 1100


def test_silence_fault_suppresses_messages_then_recovers():
    fault = parse_silence("sim-garage:10:20")
    sim = Simulator(["sim-garage", "sim-living-room"], [fault], interval=5, seed=1)
    ticks = collect(sim, 8)  # elapsed 0,5,...,35
    garage = [any(b["device_id"] == "sim-garage" for _, b in msgs) for msgs in ticks]
    assert garage == [True, True, False, False, False, False, True, True]
    assert all(any(b["device_id"] == "sim-living-room" for _, b in m) for m in ticks)


def test_spike_fault_raises_temperature():
    base = collect(Simulator(["sim-server-room"], seed=5), 6)
    spiked = collect(
        Simulator(["sim-server-room"], [parse_spike("sim-server-room:10:15:12")], seed=5), 6
    )
    pairs = zip(base, spiked, strict=True)
    deltas = [s[0][1]["temperature"] - b[0][1]["temperature"] for b, s in pairs]
    assert deltas[:2] == [pytest.approx(0, abs=1e-9)] * 2
    assert all(d == pytest.approx(12, abs=0.02) for d in deltas[2:5])
    assert deltas[5] == pytest.approx(0, abs=1e-9)


def test_repeating_fault():
    fault = parse_spike("dev:100:10:5:60")
    assert [fault.active(t) for t in (99, 100, 109, 110, 160, 165, 171)] == [
        False,
        True,
        True,
        False,
        True,
        True,
        False,
    ]


@pytest.mark.parametrize(
    "spec",
    ["dev", "dev:1", "dev:a:b", "bad id:1:2", "dev:-1:5", "dev:0:0", "dev:0:10:5"],
)
def test_invalid_silence_specs(spec):
    with pytest.raises(ValueError):
        parse_silence(spec)


def test_fault_for_unknown_device_is_rejected():
    with pytest.raises(ValueError, match="unknown devices"):
        Simulator(["a"], [parse_silence("b:0:5")])


def test_unknown_device_gets_stable_generated_profile():
    assert profile_for("kitchen-01") == profile_for("kitchen-01")
    assert profile_for("kitchen-01") != profile_for("kitchen-02")


def test_run_with_fake_clock_and_stdout_publisher():
    out = io.StringIO()
    clock = FakeClock(START)
    sim = Simulator(["sim-garage", "sim-living-room"], interval=2, seed=1)
    published = run(sim, StdoutPublisher(out), clock=clock, count=3)
    lines = out.getvalue().splitlines()
    assert published == len(lines) == 6
    topic, body = lines[0].split(" ", 1)
    assert topic == "sensors/sim-garage/telemetry"
    assert json.loads(body)["ts"] == START
    assert json.loads(lines[-1].split(" ", 1)[1])["ts"] == START + 4


def test_cli_dry_run(capsys):
    assert main(["--dry-run", "--fast", "--count", "2", "--seed", "1", "--devices", "a,b"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 4
    assert {line.split(" ")[0] for line in lines} == {
        "sensors/a/telemetry",
        "sensors/b/telemetry",
    }


class FakeMqttClient:
    def __init__(self, settings, **kwargs):
        self.kwargs = kwargs
        self.published = []
        self.connected_to = None

    def connect_async(self, host, port, keepalive):
        self.connected_to = (host, port, keepalive)

    def loop_start(self):
        pass

    def loop_stop(self):
        pass

    def disconnect(self):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))

        class Info:
            rc = 0

        return Info()


def test_mqtt_publisher_uses_one_client_per_device_with_lwt_and_device_credentials():
    created: list[FakeMqttClient] = []

    def factory(settings, **kwargs):
        client = FakeMqttClient(settings, **kwargs)
        created.append(client)
        return client

    settings = MqttSettings(host="broker", port=8883, tls=True)
    pub = MqttPublisher(
        settings, per_device_auth=True, device_password="pw", client_factory=factory
    )
    pub.start(["a", "b"])
    assert [c.kwargs["username"] for c in created] == ["a", "b"]
    assert created[0].kwargs["will_topic"] == "sensors/a/status"
    assert created[0].connected_to == ("broker", 8883, 30)

    pub.publish("sensors/b/telemetry", {"device_id": "b", "temperature": 20})
    assert created[1].published[0][0] == "sensors/b/telemetry"
    pub.stop()
    assert created[0].published[-1] == ("sensors/a/status", "offline", 1, True)
