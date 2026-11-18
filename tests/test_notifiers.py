from __future__ import annotations

import json
import logging
import urllib.error
from types import SimpleNamespace

import pytest

from iot_panel.alerts.engine import AlertEvent
from iot_panel.alerts.notifiers import (
    AlertDispatcher,
    BackgroundNotifier,
    ConsoleNotifier,
    InfluxAlertSink,
    MqttAlertSink,
    TelegramNotifier,
    notifiers_from_env,
)

EVENT = AlertEvent(
    "raised",
    "dev-1",
    "temp_high",
    "temperature",
    "warning",
    31.5,
    30.0,
    1_750_000_000.0,
    "[WARNING] dev-1: temperature 31.5 °C is above 30.0 °C (temp_high)",
)
RESOLVED = AlertEvent(**{**EVENT.__dict__, "kind": "resolved", "message": "[RESOLVED] ok"})


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_telegram_request_format():
    calls = []

    def opener(request, timeout):
        calls.append((request, timeout))
        return FakeResponse()

    TelegramNotifier("123:ABC", "-10042", opener=opener, timeout=3).notify(EVENT)
    (request, timeout) = calls[0]
    assert request.full_url == "https://api.telegram.org/bot123:ABC/sendMessage"
    assert request.get_method() == "POST"
    assert request.get_header("Content-type") == "application/json"
    body = json.loads(request.data)
    assert body["chat_id"] == "-10042"
    assert body["text"] == EVENT.message
    assert body["disable_notification"] is False
    assert timeout == 3


def test_telegram_resolved_messages_are_silent():
    notifier = TelegramNotifier("t", "c", opener=lambda *a, **k: FakeResponse())
    assert json.loads(notifier.build_request(RESOLVED).data)["disable_notification"] is True


def test_telegram_errors_are_logged_without_leaking_the_token(caplog):
    def opener(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    with caplog.at_level(logging.ERROR):
        TelegramNotifier("SECRET-TOKEN", "c", opener=opener).notify(EVENT)
    assert "HTTP 401" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


def test_telegram_network_error_is_swallowed(caplog):
    def opener(request, timeout):
        raise urllib.error.URLError("no route to host")

    TelegramNotifier("t", "c", opener=opener).notify(EVENT)
    assert "delivery failed" in caplog.text


def test_telegram_requires_credentials():
    with pytest.raises(ValueError):
        TelegramNotifier("", "chat")


def test_console_notifier_levels(caplog):
    with caplog.at_level(logging.INFO, logger="iot_panel.alerts"):
        ConsoleNotifier().notify(EVENT)
        ConsoleNotifier().notify(RESOLVED)
    levels = [r.levelno for r in caplog.records if r.name == "iot_panel.alerts"]
    assert levels == [logging.WARNING, logging.INFO]


def test_background_notifier_delivers_on_worker_thread():
    received = []
    bg = BackgroundNotifier(SimpleNamespace(notify=received.append))
    bg.notify(EVENT)
    bg.queue.join()
    bg.close()
    assert received == [EVENT]


def test_mqtt_sink_publishes_retained_state():
    published = []
    client = SimpleNamespace(publish=lambda *a, **k: published.append((a, k)))
    MqttAlertSink(client).notify(EVENT)
    (args, kwargs) = published[0]
    assert args[0] == "alerts/dev-1/temp_high"
    assert json.loads(args[1])["active"] is True
    assert kwargs == {"qos": 1, "retain": True}


def test_influx_sink_line_protocol(writer):
    InfluxAlertSink(writer, bucket="b", org="o").notify(EVENT)
    line = writer.lines[0]
    assert line.startswith(
        "alerts,device_id=dev-1,metric=temperature,rule_id=temp_high,severity=warning "
    )
    assert "active=1i" in line
    assert "threshold=30i" not in line and "threshold=30" in line
    assert line.endswith(" 1750000000000000000")


def test_dispatcher_isolates_failing_sinks(caplog):
    received = []

    class Broken:
        def notify(self, event):
            raise RuntimeError("boom")

    AlertDispatcher([Broken(), SimpleNamespace(notify=received.append)]).dispatch([EVENT])
    assert received == [EVENT]
    assert "Broken failed" in caplog.text


def test_notifiers_from_env():
    assert [type(n).__name__ for n in notifiers_from_env({})] == ["ConsoleNotifier"]
    partial = notifiers_from_env({"TELEGRAM_BOT_TOKEN": "x"})
    assert [type(n).__name__ for n in partial] == ["ConsoleNotifier"]
    both = notifiers_from_env({"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "1"})
    assert [type(n).__name__ for n in both] == ["ConsoleNotifier", "BackgroundNotifier"]
    both[1].close()
