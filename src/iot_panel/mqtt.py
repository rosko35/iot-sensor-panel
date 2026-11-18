"""MQTT helpers: topic layout and a paho-mqtt 2.x client factory with TLS support."""

from __future__ import annotations

import logging
import ssl

import paho.mqtt.client as mqtt

from .settings import MqttSettings

log = logging.getLogger(__name__)

TELEMETRY_TOPIC = "sensors/{device_id}/telemetry"
STATUS_TOPIC = "sensors/{device_id}/status"
TELEMETRY_SUBSCRIPTION = "sensors/+/telemetry"
STATUS_SUBSCRIPTION = "sensors/+/status"
ALERT_TOPIC = "alerts/{device_id}/{rule_id}"


def telemetry_topic(device_id: str) -> str:
    return TELEMETRY_TOPIC.format(device_id=device_id)


def status_topic(device_id: str) -> str:
    return STATUS_TOPIC.format(device_id=device_id)


def alert_topic(device_id: str, rule_id: str) -> str:
    return ALERT_TOPIC.format(device_id=device_id, rule_id=rule_id)


def build_client(
    settings: MqttSettings,
    *,
    client_id: str | None = None,
    username: str | None = None,
    password: str | None = None,
    will_topic: str | None = None,
    will_payload: str | None = None,
) -> mqtt.Client:
    """Create a configured (not yet connected) paho-mqtt client.

    ``username``/``password`` override the values from ``settings`` - the
    simulator uses this to log in as each virtual device in the secure setup.
    """
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id or settings.client_id,
        protocol=mqtt.MQTTv311,
    )
    user = username if username is not None else settings.username
    pwd = password if password is not None else settings.password
    if user:
        client.username_pw_set(user, pwd)
    if settings.tls:
        # Verify the broker certificate against our own CA and check the
        # hostname. There is deliberately no "insecure" switch.
        client.tls_set(
            ca_certs=settings.ca_cert,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    if will_topic:
        client.will_set(will_topic, will_payload or "offline", qos=1, retain=True)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def describe(settings: MqttSettings) -> str:
    scheme = "mqtts" if settings.tls else "mqtt"
    auth = f"{settings.username}@" if settings.username else ""
    return f"{scheme}://{auth}{settings.host}:{settings.port}"
