from __future__ import annotations

import base64
import io
import re

import pytest

import make_passwd

ENTRY_RE = re.compile(r"^\$7\$(\d+)\$([A-Za-z0-9+/=]+)\$([A-Za-z0-9+/=]+)$")


def test_hash_format_matches_mosquitto_pbkdf2_sha512():
    encoded = make_passwd.hash_password("correct horse battery", iterations=101)
    match = ENTRY_RE.match(encoded)
    assert match, encoded
    assert match.group(1) == "101"
    assert len(base64.b64decode(match.group(2))) == 12  # salt length used by mosquitto
    assert len(base64.b64decode(match.group(3))) == 64  # SHA-512 output


def test_known_vector():
    # Published PBKDF2-HMAC-SHA512 test vector (P="password", S="salt", c=1):
    # 867f70cf1ade02cff3752599a3a53dc4af34c7a669815ae5d513554e1c8cf252...
    encoded = make_passwd.hash_password("password", iterations=1, salt=b"salt")
    _, _, _, salt_b64, hash_b64 = encoded.split("$")
    assert base64.b64decode(salt_b64) == b"salt"
    assert base64.b64decode(hash_b64).hex() == (
        "867f70cf1ade02cff3752599a3a53dc4af34c7a669815ae5d513554e1c8cf252"
        "c02d470a285a0501bad999bfe943c08f050235d7d68b1da55e63f73b60a57fce"
    )


def test_verify_password():
    encoded = make_passwd.hash_password("s3cret-passw0rd")
    assert make_passwd.verify_password(encoded, "s3cret-passw0rd")
    assert not make_passwd.verify_password(encoded, "wrong-password")
    assert not make_passwd.verify_password("$6$abc$def", "x")
    assert not make_passwd.verify_password("garbage", "x")


def test_salts_are_random():
    assert make_passwd.hash_password("same-password") != make_passwd.hash_password("same-password")


@pytest.mark.parametrize(
    ("user", "password"),
    [("bad user", "long-enough-password"), ("ok", "short"), ("ok", "change-me-ingest-password")],
)
def test_validation(user, password):
    with pytest.raises(ValueError):
        make_passwd.validate(user, password)


def test_users_from_env():
    users = make_passwd.users_from_env(
        {
            "MQTT_INGEST_PASSWORD": "ingest-password-123",
            "MQTT_MONITOR_PASSWORD": "monitor-password-123",
            "SIM_DEVICE_PASSWORD": "device-password-123",
            "SIM_DEVICES": "sim-a, sim-b",
        }
    )
    assert users == {
        "ingest": "ingest-password-123",
        "monitor": "monitor-password-123",
        "sim-a": "device-password-123",
        "sim-b": "device-password-123",
    }
    with pytest.raises(ValueError, match="MQTT_INGEST_PASSWORD"):
        make_passwd.users_from_env({"SIM_DEVICE_PASSWORD": "x", "SIM_DEVICES": "a"})


def test_cli_from_env_then_update_single_user(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        'MQTT_INGEST_PASSWORD="ingest-password-123"\n'
        "SIM_DEVICE_PASSWORD=device-password-123\n"
        "SIM_DEVICES=sim-a,sim-b\n",
        encoding="utf-8",
    )
    out = tmp_path / "passwd"
    assert make_passwd.main(["--from-env", str(env_file), "--out", str(out)]) == 0
    entries = make_passwd.read_passwd(out)
    assert set(entries) == {"ingest", "sim-a", "sim-b"}
    assert make_passwd.verify_password(entries["ingest"], "ingest-password-123")

    monkeypatch.setattr("sys.stdin", io.StringIO("esp32-device-pass\n"))
    args = ["--user", "esp32-kitchen", "--password-stdin", "--out", str(out)]
    assert make_passwd.main(args) == 0
    entries = make_passwd.read_passwd(out)
    assert set(entries) == {"ingest", "sim-a", "sim-b", "esp32-kitchen"}
    assert make_passwd.verify_password(entries["esp32-kitchen"], "esp32-device-pass")
    # existing users were kept as-is
    assert make_passwd.verify_password(entries["sim-a"], "device-password-123")

    assert make_passwd.main(["--user", "sim-b", "--delete", "--out", str(out)]) == 0
    assert "sim-b" not in make_passwd.read_passwd(out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert all(re.match(r"^[\w-]+:\$7\$", line) for line in lines)


def test_cli_rejects_placeholder_passwords(root, tmp_path):
    out = tmp_path / "passwd"
    assert make_passwd.main(["--from-env", str(root / ".env.example"), "--out", str(out)]) == 2
    assert not out.exists()
