"""Alert delivery: console (default), Telegram, MQTT and InfluxDB sinks."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol

from influxdb_client import Point, WritePrecision

from ..mqtt import alert_topic
from ..settings import env_str
from .engine import AlertEvent

log = logging.getLogger(__name__)

ALERT_MEASUREMENT = "alerts"


class Sink(Protocol):
    def notify(self, event: AlertEvent) -> None: ...


class ConsoleNotifier:
    """Logs alerts; used on its own when no other notifier is configured."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.log = logger or logging.getLogger("iot_panel.alerts")

    def notify(self, event: AlertEvent) -> None:
        level = logging.WARNING if event.active else logging.INFO
        self.log.log(level, "ALERT %s", event.message)


class TelegramNotifier:
    """Sends alert messages through the Telegram Bot API (``sendMessage``)."""

    API = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 10.0,
    ) -> None:
        if not token or not chat_id:
            raise ValueError("Telegram token and chat id are required")
        self._url = self.API.format(token=token)
        self.chat_id = chat_id
        self.opener = opener
        self.timeout = timeout

    def build_request(self, event: AlertEvent) -> urllib.request.Request:
        body = json.dumps(
            {
                "chat_id": self.chat_id,
                "text": event.message,
                "disable_web_page_preview": True,
                # Resolved notices do not need to wake anyone up.
                "disable_notification": not event.active,
            }
        ).encode()
        return urllib.request.Request(
            self._url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

    def notify(self, event: AlertEvent) -> None:
        try:
            with self.opener(self.build_request(event), timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    log.error("Telegram API returned HTTP %s", status)
        except urllib.error.HTTPError as exc:
            # Never log the URL: it contains the bot token.
            log.error("Telegram API error: HTTP %s", exc.code)
        except (urllib.error.URLError, OSError) as exc:
            log.error("Telegram delivery failed: %s", getattr(exc, "reason", exc))


class BackgroundNotifier:
    """Runs a slow notifier (HTTP) on a worker thread so MQTT callbacks never block."""

    def __init__(self, inner: Sink, maxsize: int = 100) -> None:
        self.inner = inner
        self.queue: queue.Queue[AlertEvent | None] = queue.Queue(maxsize=maxsize)
        self.thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        self.thread.start()

    def notify(self, event: AlertEvent) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            log.error("notification queue full, dropping: %s", event.message)

    def _run(self) -> None:
        while True:
            event = self.queue.get()
            try:
                if event is None:
                    return
                self.inner.notify(event)
            except Exception:
                log.exception("notifier failed")
            finally:
                self.queue.task_done()

    def close(self, timeout: float = 5.0) -> None:
        self.queue.put(None)
        self.thread.join(timeout)


class MqttAlertSink:
    """Publishes the current state of each alert as a retained JSON message.

    Topic: ``alerts/<device_id>/<rule_id>``. Retained so a new subscriber (or
    the ingest service after a restart) immediately sees what is active.
    """

    def __init__(self, client: Any) -> None:
        self.client = client

    def notify(self, event: AlertEvent) -> None:
        self.client.publish(
            alert_topic(event.device_id, event.rule_id),
            json.dumps(event.to_dict(), ensure_ascii=False),
            qos=1,
            retain=True,
        )


def alert_point(event: AlertEvent) -> Point:
    return (
        Point(ALERT_MEASUREMENT)
        .tag("device_id", event.device_id)
        .tag("rule_id", event.rule_id)
        .tag("severity", event.severity)
        .tag("metric", event.metric)
        .field("active", 1 if event.active else 0)
        .field("value", float(event.value))
        .field("threshold", float(event.threshold))
        .field("message", event.message)
        .time(round(event.ts * 1_000_000_000), WritePrecision.NS)
    )


class InfluxAlertSink:
    """Writes every alert transition so Grafana can show state and history."""

    def __init__(self, writer: Any, *, bucket: str, org: str) -> None:
        self.writer = writer
        self.bucket = bucket
        self.org = org

    def notify(self, event: AlertEvent) -> None:
        self.writer.write(bucket=self.bucket, org=self.org, record=alert_point(event))


class AlertDispatcher:
    """Fans events out to every sink; one failing sink never blocks the others."""

    def __init__(self, sinks: Iterable[Sink]) -> None:
        self.sinks = list(sinks)

    def dispatch(self, events: Iterable[AlertEvent]) -> None:
        for event in events:
            for sink in self.sinks:
                try:
                    sink.notify(event)
                except Exception:
                    log.exception("alert sink %s failed", type(sink).__name__)


def notifiers_from_env(env: Mapping[str, str] | None = None) -> list[Sink]:
    """Console notifier always; Telegram when both token and chat id are set."""
    env = os.environ if env is None else env
    sinks: list[Sink] = [ConsoleNotifier()]
    token = env_str(env, "TELEGRAM_BOT_TOKEN")
    chat_id = env_str(env, "TELEGRAM_CHAT_ID")
    if token and chat_id:
        sinks.append(BackgroundNotifier(TelegramNotifier(token, chat_id)))
        log.info("Telegram notifications enabled for chat %s", chat_id)
    elif token or chat_id:
        log.warning("Telegram needs both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; using console")
    else:
        log.info("no notifier configured, alerts go to the console log")
    return sinks
