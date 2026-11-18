"""Keep the ESP32 firmware and the Python payload schema in sync."""

from __future__ import annotations

import re

from iot_panel.payload import KNOWN_KEYS


def test_firmware_payload_keys_are_known_to_the_schema(root):
    source = (root / "firmware/src/main.cpp").read_text(encoding="utf-8")
    keys = set(re.findall(r'doc\["([a-z_]+)"\]', source))
    assert {"v", "device_id", "temperature", "humidity"} <= keys
    assert keys <= KNOWN_KEYS, f"unknown payload keys in firmware: {keys - KNOWN_KEYS}"


def test_firmware_topics_match_backend_layout(root):
    source = (root / "firmware/src/main.cpp").read_text(encoding="utf-8")
    assert '"sensors/%s/telemetry"' in source
    assert '"sensors/%s/status"' in source


def test_secrets_header_is_ignored_and_example_is_shipped(root):
    gitignore = (root / ".gitignore").read_text(encoding="utf-8")
    assert "firmware/include/secrets.h" in gitignore
    example = (root / "firmware/include/secrets.example.h").read_text(encoding="utf-8")
    for name in ("WIFI_SSID", "MQTT_HOST", "MQTT_USE_TLS", "MQTT_CA_CERT", "DEVICE_ID"):
        assert f"{name}" in example
    assert "PRIVATE KEY" not in example
