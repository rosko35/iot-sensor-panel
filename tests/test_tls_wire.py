"""What an eavesdropper sees: plain MQTT vs MQTT over TLS.

A recording TCP proxy sits between the real paho-mqtt client (configured by
:func:`iot_panel.mqtt.build_client`) and a tiny fake broker, like Wireshark on
the wire. With plain MQTT the password and telemetry are visible in the
captured bytes; with TLS they are not. This is the automated counterpart of
docs/security-demo.md.
"""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time

import pytest

from iot_panel.mqtt import build_client
from iot_panel.settings import MqttSettings

import gen_certs

PASSWORD = "TopSecret-Password-42"
PAYLOAD = '{"device_id":"sim-a","temperature":21.5}'
CONNACK_OK = b"\x20\x02\x00\x00"


def _listener() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(10)
    return sock


class FakeBroker:
    """Accepts one client, answers CONNECT with CONNACK and records plaintext MQTT."""

    def __init__(self, tls_context: ssl.SSLContext | None = None) -> None:
        self.tls_context = tls_context
        self.sock = _listener()
        self.port = self.sock.getsockname()[1]
        self.received = bytearray()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn = None
        try:
            conn, _ = self.sock.accept()
            conn.settimeout(5)
            if self.tls_context is not None:
                conn = self.tls_context.wrap_socket(conn, server_side=True)
            self.received += conn.recv(4096)
            conn.sendall(CONNACK_OK)
            while chunk := conn.recv(4096):
                self.received += chunk
        except (OSError, ssl.SSLError):
            pass
        finally:
            if conn is not None:
                conn.close()
            self.sock.close()


class RecordingProxy:
    """Forwards TCP traffic and records the client -> broker bytes (the 'capture')."""

    def __init__(self, upstream_port: int) -> None:
        self.upstream_port = upstream_port
        self.sock = _listener()
        self.port = self.sock.getsockname()[1]
        self.captured = bytearray()
        threading.Thread(target=self._serve, daemon=True).start()

    def _pump(self, src, dst, record: bool) -> None:
        try:
            while chunk := src.recv(4096):
                if record:
                    self.captured += chunk
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)

    def _serve(self) -> None:
        try:
            client, _ = self.sock.accept()
        except OSError:
            return
        upstream = socket.create_connection(("127.0.0.1", self.upstream_port), timeout=10)
        threading.Thread(target=self._pump, args=(client, upstream, True), daemon=True).start()
        threading.Thread(target=self._pump, args=(upstream, client, False), daemon=True).start()


@pytest.fixture(scope="module")
def certs(tmp_path_factory):
    return gen_certs.generate(tmp_path_factory.mktemp("certs"))


def server_context(certs) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certs["server.crt"], certs["server.key"])
    return ctx


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def connect_publish(settings: MqttSettings, port: int) -> None:
    connected = threading.Event()
    client = build_client(settings)
    client.on_connect = lambda c, u, f, rc, p=None: rc.is_failure or connected.set()
    client.connect("localhost", port, keepalive=30)
    client.loop_start()
    try:
        assert connected.wait(10), "no CONNACK received"
        client.publish("sensors/sim-a/telemetry", PAYLOAD, qos=0).wait_for_publish(5)
    finally:
        client.disconnect()
        client.loop_stop()


def test_plain_mqtt_leaks_credentials_and_payload():
    broker = FakeBroker()
    proxy = RecordingProxy(broker.port)
    settings = MqttSettings(host="localhost", port=proxy.port, username="sim-a", password=PASSWORD)
    connect_publish(settings, proxy.port)
    assert wait_for(lambda: PAYLOAD.encode() in proxy.captured)
    assert PASSWORD.encode() in proxy.captured
    assert b"sensors/sim-a/telemetry" in proxy.captured
    assert proxy.captured[0] == 0x10  # MQTT CONNECT packet in the clear


def test_tls_hides_credentials_and_payload(certs):
    broker = FakeBroker(server_context(certs))
    proxy = RecordingProxy(broker.port)
    settings = MqttSettings(
        host="localhost",
        port=proxy.port,
        tls=True,
        ca_cert=str(certs["ca.crt"]),
        username="sim-a",
        password=PASSWORD,
    )
    connect_publish(settings, proxy.port)
    # The broker received everything (so it really was sent)...
    assert wait_for(lambda: PAYLOAD.encode() in broker.received)
    assert PASSWORD.encode() in broker.received
    # ...but the wire only carried TLS records.
    assert proxy.captured[0] == 0x16  # TLS handshake record
    assert PASSWORD.encode() not in proxy.captured
    assert PAYLOAD.encode() not in proxy.captured
    assert b"sensors/sim-a" not in proxy.captured


def test_tls_rejects_broker_signed_by_another_ca(certs, tmp_path):
    rogue = gen_certs.generate(tmp_path)  # attacker-controlled CA and server cert
    broker = FakeBroker(server_context(rogue))
    settings = MqttSettings(tls=True, ca_cert=str(certs["ca.crt"]), port=broker.port)
    client = build_client(settings)
    with pytest.raises(ssl.SSLCertVerificationError):
        client.connect("localhost", broker.port)


def test_tls_checks_the_host_name(tmp_path):
    only_mosquitto = gen_certs.generate(tmp_path, dns=["mosquitto"], ips=["10.9.9.9"])
    broker = FakeBroker(server_context(only_mosquitto))
    settings = MqttSettings(tls=True, ca_cert=str(only_mosquitto["ca.crt"]), port=broker.port)
    client = build_client(settings)
    with pytest.raises(ssl.SSLCertVerificationError, match=r"match|mismatch"):
        client.connect("localhost", broker.port)
