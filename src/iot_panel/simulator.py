"""Virtual sensor fleet that publishes realistic telemetry to MQTT.

Each virtual device produces:

* temperature following a diurnal (24 h) sine curve, plus a slow
  mean-reverting random drift (Ornstein-Uhlenbeck process) and Gaussian noise;
* relative humidity that is anti-correlated with temperature;
* barometric pressure (only for "BME280" profiles) with a slow random walk;
* PIR motion events whose probability depends on the time of day.

Fault injection makes the alerting pipeline demonstrable:

* ``--silence DEVICE:START:DURATION[:EVERY]`` - the device stops publishing
  (it stays connected, like a hung sensor task), so offline detection fires;
* ``--spike DEVICE:START:DURATION:DELTA[:EVERY]`` - adds ``DELTA`` degrees
  Celsius to the temperature, so threshold alarms fire and later recover.

``START``/``DURATION``/``EVERY`` are seconds since the simulator started.

Examples::

    python -m iot_panel.simulator --dry-run --count 5
    python -m iot_panel.simulator --dry-run --fast --count 60 --interval 10 \
        --spike sim-server-room:120:120:9 --silence sim-garage:200:200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import os
import random
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import __version__
from .clock import Clock, FakeClock, SystemClock
from .mqtt import build_client, describe, status_topic, telemetry_topic
from .payload import DEVICE_ID_RE, SCHEMA_VERSION
from .settings import MqttSettings, env_float, env_int, env_str

log = logging.getLogger("iot_panel.simulator")

FIRMWARE = f"sim-{__version__}"
DAY_S = 24 * 3600.0


@dataclass(frozen=True)
class DeviceProfile:
    device_id: str
    base_temp: float = 21.0  # daily mean, deg C
    temp_amplitude: float = 2.0  # half of the day/night swing
    base_humidity: float = 45.0
    humidity_per_degree: float = 2.5  # RH drop per deg C above the mean
    motion_rate: float = 0.15  # chance of motion per sample at peak activity
    has_pressure: bool = False
    noise: float = 0.15  # sensor noise (std dev, deg C)


DEFAULT_PROFILES: dict[str, DeviceProfile] = {
    "sim-living-room": DeviceProfile(
        "sim-living-room", base_temp=21.5, temp_amplitude=1.8, base_humidity=45, motion_rate=0.3
    ),
    "sim-server-room": DeviceProfile(
        "sim-server-room",
        base_temp=24.0,
        temp_amplitude=0.8,
        base_humidity=35,
        humidity_per_degree=1.0,
        motion_rate=0.03,
        has_pressure=True,
    ),
    "sim-greenhouse": DeviceProfile(
        "sim-greenhouse",
        base_temp=22.0,
        temp_amplitude=6.0,
        base_humidity=72,
        humidity_per_degree=3.0,
        motion_rate=0.05,
        has_pressure=True,
    ),
    "sim-garage": DeviceProfile(
        "sim-garage", base_temp=11.0, temp_amplitude=3.5, base_humidity=60, motion_rate=0.08
    ),
}


def profile_for(device_id: str) -> DeviceProfile:
    """Return the built-in profile or derive a stable one from the device id."""
    if device_id in DEFAULT_PROFILES:
        return DEFAULT_PROFILES[device_id]
    if not DEVICE_ID_RE.match(device_id):
        raise ValueError(f"invalid device id {device_id!r}")
    digest = hashlib.sha256(device_id.encode()).digest()
    return DeviceProfile(
        device_id,
        base_temp=18.0 + digest[0] / 255 * 6.0,
        temp_amplitude=1.0 + digest[1] / 255 * 3.0,
        base_humidity=35.0 + digest[2] / 255 * 30.0,
        motion_rate=0.02 + digest[3] / 255 * 0.2,
        has_pressure=bool(digest[4] & 1),
    )


@dataclass(frozen=True)
class Fault:
    kind: str  # "silence" or "spike"
    device_id: str
    start: float
    duration: float
    every: float | None = None
    delta: float = 0.0

    def active(self, elapsed: float) -> bool:
        if elapsed < self.start:
            return False
        offset = elapsed - self.start
        if self.every:
            offset %= self.every
        return offset < self.duration


def _parse_fault(kind: str, spec: str) -> Fault:
    parts = spec.strip().split(":")
    expected = "DEVICE:START:DURATION" + (":DELTA" if kind == "spike" else "") + "[:EVERY]"
    needed = 4 if kind == "spike" else 3
    if len(parts) not in (needed, needed + 1):
        raise ValueError(f"{kind} spec {spec!r} must look like {expected}")
    device_id = parts[0]
    if not DEVICE_ID_RE.match(device_id):
        raise ValueError(f"invalid device id in {kind} spec {spec!r}")
    try:
        start, duration = float(parts[1]), float(parts[2])
        delta = float(parts[3]) if kind == "spike" else 0.0
        every = float(parts[needed]) if len(parts) == needed + 1 else None
    except ValueError as exc:
        raise ValueError(f"{kind} spec {spec!r} must look like {expected}") from exc
    if start < 0 or duration <= 0:
        raise ValueError(f"{kind} spec {spec!r}: START must be >= 0 and DURATION > 0")
    if every is not None and every <= duration:
        raise ValueError(f"{kind} spec {spec!r}: EVERY must be larger than DURATION")
    return Fault(kind, device_id, start, duration, every, delta)


def parse_silence(spec: str) -> Fault:
    return _parse_fault("silence", spec)


def parse_spike(spec: str) -> Fault:
    return _parse_fault("spike", spec)


def _split_specs(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item for item in (s.strip() for s in raw.replace(";", ",").split(",")) if item]


class SensorModel:
    """Stateful signal generator for one virtual device."""

    def __init__(self, profile: DeviceProfile, rng: random.Random) -> None:
        self.profile = profile
        self.rng = rng
        self.drift = 0.0  # OU process state, deg C
        self.pressure = 1013.25 + rng.uniform(-8, 8)
        self.motion_hold = 0  # samples left for which motion stays "true"
        self.seq = 0

    @staticmethod
    def diurnal(virtual_ts: float) -> float:
        """Sine in [-1, 1] that peaks at 15:00 and bottoms out at 03:00 (UTC)."""
        hour = (virtual_ts % DAY_S) / 3600.0
        return math.sin(2 * math.pi * (hour - 9.0) / 24.0)

    @staticmethod
    def activity(virtual_ts: float) -> float:
        """Rough human activity factor in [0.1, 1]: low at night, high in the evening."""
        hour = (virtual_ts % DAY_S) / 3600.0
        if hour < 6 or hour >= 23:
            return 0.1
        if 17 <= hour < 23:
            return 1.0
        return 0.5

    def sample(self, virtual_ts: float, dt: float, spike: float = 0.0) -> dict[str, Any]:
        p, rng = self.profile, self.rng
        # Ornstein-Uhlenbeck drift: mean-reverting with ~1 h time constant.
        theta, sigma = 1.0 / 3600.0, 0.02
        step = max(dt, 0.0)
        self.drift += -theta * self.drift * step + sigma * math.sqrt(step) * rng.gauss(0, 1)
        self.drift = max(-2.5, min(2.5, self.drift))

        ambient = p.base_temp + p.temp_amplitude * self.diurnal(virtual_ts) + self.drift
        temperature = ambient + spike + rng.gauss(0, p.noise)
        humidity = (
            p.base_humidity
            - p.humidity_per_degree * (ambient - p.base_temp)
            + rng.gauss(0, 0.6)
            - 1.5 * spike  # hot air holds more water -> relative humidity drops
        )
        humidity = max(0.0, min(100.0, humidity))

        if self.motion_hold > 0:
            self.motion_hold -= 1
            motion = True
        else:
            motion = rng.random() < p.motion_rate * self.activity(virtual_ts)
            if motion:
                self.motion_hold = rng.randint(0, 3)

        self.seq += 1
        reading: dict[str, Any] = {
            "temperature": round(max(-40.0, min(85.0, temperature)), 2),
            "humidity": round(humidity, 1),
            "motion": motion,
            "rssi": int(rng.gauss(-62, 4)),
        }
        if p.has_pressure:
            self.pressure += rng.gauss(0, 0.05) * math.sqrt(step) / 10
            self.pressure = max(960.0, min(1050.0, self.pressure))
            reading["pressure"] = round(self.pressure + rng.gauss(0, 0.1), 1)
        return reading


@dataclass
class Simulator:
    """Produces (topic, payload) pairs for every non-silent device per tick."""

    device_ids: Sequence[str]
    faults: Sequence[Fault] = ()
    interval: float = 5.0
    time_scale: float = 1.0
    seed: int | None = None
    start_ts: float | None = None
    models: dict[str, SensorModel] = field(init=False)
    _last_tick: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.interval <= 0:
            raise ValueError("interval must be positive")
        if self.time_scale <= 0:
            raise ValueError("time_scale must be positive")
        if not self.device_ids:
            raise ValueError("at least one device is required")
        unknown = {f.device_id for f in self.faults} - set(self.device_ids)
        if unknown:
            raise ValueError(f"faults reference unknown devices: {sorted(unknown)}")
        rng = random.Random(self.seed)
        self.models = {
            device_id: SensorModel(profile_for(device_id), random.Random(rng.random()))
            for device_id in self.device_ids
        }

    def elapsed(self, now: float) -> float:
        if self.start_ts is None:
            self.start_ts = now
        return now - self.start_ts

    def is_silent(self, device_id: str, elapsed: float) -> bool:
        return any(
            f.kind == "silence" and f.device_id == device_id and f.active(elapsed)
            for f in self.faults
        )

    def spike(self, device_id: str, elapsed: float) -> float:
        return sum(
            f.delta
            for f in self.faults
            if f.kind == "spike" and f.device_id == device_id and f.active(elapsed)
        )

    def tick(self, now: float) -> list[tuple[str, dict[str, Any]]]:
        elapsed = self.elapsed(now)
        dt = self.interval if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        assert self.start_ts is not None
        # Accelerated "virtual" time drives the day/night cycle; the published
        # timestamp is always the real time so ingest accepts it.
        virtual_ts = self.start_ts + elapsed * self.time_scale
        messages = []
        for device_id, model in self.models.items():
            reading = model.sample(virtual_ts, dt * self.time_scale, self.spike(device_id, elapsed))
            if self.is_silent(device_id, elapsed):
                continue
            payload = {
                "v": SCHEMA_VERSION,
                "device_id": device_id,
                "ts": round(now, 3),
                "seq": model.seq,
                **reading,
                "fw": FIRMWARE,
            }
            messages.append((telemetry_topic(device_id), payload))
        return messages


# --------------------------------------------------------------------------- publishing


class StdoutPublisher:
    """Dry-run sink: one ``<topic> <json>`` line per message."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def start(self, device_ids: Iterable[str]) -> None:
        pass

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.stream.write(f"{topic} {json.dumps(payload, separators=(',', ':'))}\n")
        self.stream.flush()

    def stop(self) -> None:
        pass


class MqttPublisher:
    """One MQTT connection per virtual device.

    Each device has its own client id, retained ``online``/``offline`` status
    with a last will, and - in the secure setup - its own credentials, so the
    broker ACL can restrict every device to its own topics.
    """

    def __init__(
        self,
        settings: MqttSettings,
        *,
        per_device_auth: bool = False,
        device_password: str | None = None,
        client_factory: Callable[..., Any] = build_client,
    ) -> None:
        self.settings = settings
        self.per_device_auth = per_device_auth
        self.device_password = device_password
        self.client_factory = client_factory
        self.clients: dict[str, Any] = {}

    def start(self, device_ids: Iterable[str]) -> None:
        for device_id in device_ids:
            kwargs: dict[str, Any] = {
                "client_id": f"{device_id}",
                "will_topic": status_topic(device_id),
                "will_payload": "offline",
            }
            if self.per_device_auth:
                kwargs["username"] = device_id
                kwargs["password"] = self.device_password
            client = self.client_factory(self.settings, **kwargs)
            client.on_connect = self._on_connect_factory(device_id)
            client.connect_async(self.settings.host, self.settings.port, self.settings.keepalive)
            client.loop_start()
            self.clients[device_id] = client
        log.info("simulating %d devices against %s", len(self.clients), describe(self.settings))

    @staticmethod
    def _on_connect_factory(device_id: str) -> Callable[..., None]:
        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code.is_failure:
                log.error("%s: connect refused: %s", device_id, reason_code)
                return
            log.info("%s: connected", device_id)
            client.publish(status_topic(device_id), "online", qos=1, retain=True)

        return on_connect

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        client = self.clients[payload["device_id"]]
        info = client.publish(topic, json.dumps(payload, separators=(",", ":")), qos=1)
        if info.rc != 0:
            log.debug("%s: publish queued while disconnected (rc=%s)", topic, info.rc)

    def stop(self) -> None:
        for device_id, client in self.clients.items():
            info = client.publish(status_topic(device_id), "offline", qos=1, retain=True)
            # A clean disconnect suppresses the last will, so make sure the
            # explicit "offline" status is delivered before disconnecting.
            try:
                info.wait_for_publish(timeout=2)
            except (RuntimeError, ValueError):
                log.debug("%s: could not deliver offline status", device_id)
            client.disconnect()
            client.loop_stop()


# --------------------------------------------------------------------------------- CLI


def build_arg_parser(env: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = dict(os.environ) if env is None else env
    p = argparse.ArgumentParser(
        prog="iot-simulator", description="Publish simulated sensor telemetry to MQTT."
    )
    p.add_argument(
        "--devices",
        default=env_str(env, "SIM_DEVICES", ",".join(DEFAULT_PROFILES)),
        help="comma-separated device ids (built-in profiles: %(default)s)",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=env_float(env, "SIM_INTERVAL", 5.0),
        help="seconds between samples (default: %(default)s)",
    )
    p.add_argument(
        "--time-scale",
        type=float,
        default=env_float(env, "SIM_TIME_SCALE", 1.0),
        help="speed-up of the day/night cycle, e.g. 288 = one day per 5 min",
    )
    p.add_argument(
        "--silence",
        action="append",
        default=_split_specs(env_str(env, "SIM_SILENCE")),
        metavar="DEVICE:START:DURATION[:EVERY]",
        help="make a device stop publishing (repeatable)",
    )
    p.add_argument(
        "--spike",
        action="append",
        default=_split_specs(env_str(env, "SIM_SPIKE")),
        metavar="DEVICE:START:DURATION:DELTA[:EVERY]",
        help="add DELTA deg C to a device's temperature (repeatable)",
    )
    seed = env_str(env, "SIM_SEED")
    p.add_argument("--seed", type=int, default=int(seed) if seed else None)
    p.add_argument(
        "--count",
        type=int,
        default=env_int(env, "SIM_COUNT", 0),
        help="stop after N ticks (0 = run forever)",
    )
    p.add_argument("--dry-run", action="store_true", help="print messages instead of publishing")
    p.add_argument(
        "--fast",
        action="store_true",
        help="do not sleep; advance a virtual clock by --interval per tick",
    )
    p.add_argument("--log-level", default=env_str(env, "LOG_LEVEL", "INFO"))
    return p


def run(
    simulator: Simulator,
    publisher: Any,
    *,
    clock: Clock,
    count: int = 0,
    sleep: Callable[[float], Any] | None = None,
    stop: threading.Event | None = None,
) -> int:
    """Main loop; returns the number of messages published."""
    stop = stop or threading.Event()
    sleep = sleep or stop.wait
    published = 0
    ticks = 0
    publisher.start(simulator.device_ids)
    try:
        while not stop.is_set():
            started = clock.now()
            for topic, payload in simulator.tick(started):
                publisher.publish(topic, payload)
                published += 1
            ticks += 1
            if count and ticks >= count:
                break
            if isinstance(clock, FakeClock):
                clock.advance(simulator.interval)
            else:
                sleep(max(0.0, simulator.interval - (clock.now() - started)))
    finally:
        publisher.stop()
    return published


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    device_ids = [d.strip() for d in args.devices.split(",") if d.strip()]
    faults = [parse_silence(s) for s in args.silence] + [parse_spike(s) for s in args.spike]
    clock: Clock = FakeClock(time.time()) if args.fast else SystemClock()
    simulator = Simulator(
        device_ids,
        faults,
        interval=args.interval,
        time_scale=args.time_scale,
        seed=args.seed,
    )
    for fault in faults:
        log.info("fault scheduled: %s", fault)

    if args.dry_run:
        publisher: Any = StdoutPublisher()
    else:
        env = os.environ
        publisher = MqttPublisher(
            MqttSettings.from_env(env, client_id="simulator"),
            per_device_auth=env.get("SIM_PER_DEVICE_AUTH", "").lower() in {"1", "true", "yes"},
            device_password=env_str(env, "SIM_DEVICE_PASSWORD"),
        )

    stop = threading.Event()

    def _handle_signal(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    published = run(simulator, publisher, clock=clock, count=args.count, stop=stop)
    log.info("published %d messages", published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
