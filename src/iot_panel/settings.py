"""Environment-driven settings shared by the simulator and the ingest service.

Everything is configured through environment variables so the same image can
run in the insecure and secure compose setups without code changes.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def env_bool(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be a boolean, got {raw!r}")


def env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def env_str(env: Mapping[str, str], name: str, default: str | None = None) -> str | None:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip()


@dataclass(frozen=True)
class MqttSettings:
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_cert: str | None = None
    client_id: str = "iot-panel"
    keepalive: int = 30

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, client_id: str = "iot-panel"
    ) -> MqttSettings:
        env = os.environ if env is None else env
        tls = env_bool(env, "MQTT_TLS", False)
        return cls(
            host=env_str(env, "MQTT_HOST", "localhost") or "localhost",
            port=env_int(env, "MQTT_PORT", 8883 if tls else 1883),
            username=env_str(env, "MQTT_USERNAME"),
            password=env_str(env, "MQTT_PASSWORD"),
            tls=tls,
            ca_cert=env_str(env, "MQTT_CA_CERT"),
            client_id=env_str(env, "MQTT_CLIENT_ID", client_id) or client_id,
            keepalive=env_int(env, "MQTT_KEEPALIVE", 30),
        )


@dataclass(frozen=True)
class InfluxSettings:
    url: str = "http://localhost:8086"
    token: str = ""
    org: str = "iot"
    bucket: str = "sensors"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> InfluxSettings:
        env = os.environ if env is None else env
        return cls(
            url=env_str(env, "INFLUX_URL", "http://localhost:8086") or "http://localhost:8086",
            token=env_str(env, "INFLUX_TOKEN", "") or "",
            org=env_str(env, "INFLUX_ORG", "iot") or "iot",
            bucket=env_str(env, "INFLUX_BUCKET", "sensors") or "sensors",
        )
