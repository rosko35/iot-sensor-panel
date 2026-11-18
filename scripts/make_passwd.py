#!/usr/bin/env python3
"""Create or update the Mosquitto password file without needing mosquitto_passwd.

Hashes use Mosquitto 2.x's PBKDF2-SHA512 format::

    username:$7$<iterations>$<base64 salt>$<base64 hash>

Usage::

    # all service accounts + every simulated device, passwords taken from .env
    python scripts/make_passwd.py --from-env .env

    # add / update one account (e.g. a real ESP32), prompting for the password
    python scripts/make_passwd.py --user esp32-kitchen

    # non-interactive (CI): password on stdin, never on the command line
    printf '%s' "$PW" | python scripts/make_passwd.py --user esp32-kitchen --password-stdin

The output (default ``mosquitto/config/passwd``) is git-ignored. If you have
Docker, ``scripts/make_passwd.sh`` does the same with the official
``mosquitto_passwd`` tool.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import os
import re
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "mosquitto" / "config" / "passwd"
SALT_LEN = 12  # same as mosquitto_passwd
# mosquitto_passwd defaults to 101 iterations; more is cheap for a handful of
# MQTT logins and makes an offline attack on a leaked file slower.
DEFAULT_ITERATIONS = 1000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MIN_PASSWORD_LEN = 12


def hash_password(
    password: str, *, iterations: int = DEFAULT_ITERATIONS, salt: bytes | None = None
) -> str:
    if iterations < 1:
        raise ValueError("iterations must be positive")
    salt = secrets.token_bytes(SALT_LEN) if salt is None else salt
    digest = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, iterations)
    return f"$7${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(encoded: str, password: str) -> bool:
    try:
        _, kind, iterations, salt_b64, hash_b64 = encoded.split("$")
    except ValueError:
        return False
    if kind != "7":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(hash_b64)
    actual = hashlib.pbkdf2_hmac("sha512", password.encode("utf-8"), salt, int(iterations))
    return hmac.compare_digest(actual, expected)


def read_passwd(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and ":" in line:
                user, encoded = line.split(":", 1)
                entries[user] = encoded
    return entries


def write_passwd(path: Path, entries: Mapping[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(f"{user}:{encoded}\n" for user, encoded in sorted(entries.items()))
    path.write_text(content, encoding="utf-8", newline="\n")
    os.chmod(path, 0o600)


def validate(user: str, password: str) -> None:
    if not USERNAME_RE.match(user):
        raise ValueError(f"invalid username {user!r}")
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"password for {user!r} must be at least {MIN_PASSWORD_LEN} characters")
    if "change-me" in password:
        raise ValueError(f"password for {user!r} is still the placeholder from .env.example")


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


def users_from_env(env: Mapping[str, str]) -> dict[str, str]:
    """Service accounts and simulated devices for the secure compose setup."""
    users: dict[str, str] = {}

    def need(name: str) -> str:
        value = env.get(name, "")
        if not value:
            raise ValueError(f"{name} is not set")
        return value

    users["ingest"] = need("MQTT_INGEST_PASSWORD")
    if env.get("MQTT_MONITOR_PASSWORD"):
        users["monitor"] = env["MQTT_MONITOR_PASSWORD"]
    device_password = need("SIM_DEVICE_PASSWORD")
    devices = [d.strip() for d in env.get("SIM_DEVICES", "").split(",") if d.strip()]
    if not devices:
        raise ValueError("SIM_DEVICES is empty")
    for device in devices:
        users[device] = device_password
    return users


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--from-env", type=Path, metavar="ENV_FILE")
    source.add_argument("--user", help="add or update a single user")
    parser.add_argument("--password-stdin", action="store_true")
    parser.add_argument("--delete", action="store_true", help="remove --user from the file")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    args = parser.parse_args(argv)

    entries = read_passwd(args.out)
    try:
        if args.from_env:
            users = users_from_env(parse_env_file(args.from_env))
        elif args.delete:
            if entries.pop(args.user, None) is None:
                raise ValueError(f"user {args.user!r} not found in {args.out}")
            write_passwd(args.out, entries)
            print(f"removed {args.user} from {args.out}")
            return 0
        else:
            if args.password_stdin:
                password = sys.stdin.readline().rstrip("\r\n")
            else:
                password = getpass.getpass(f"Password for {args.user}: ")
                if getpass.getpass("Repeat: ") != password:
                    raise ValueError("passwords do not match")
            users = {args.user: password}
        for user, password in users.items():
            validate(user, password)
            entries[user] = hash_password(password, iterations=args.iterations)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    write_passwd(args.out, entries)
    print(f"wrote {len(entries)} account(s) to {args.out}: {', '.join(sorted(users))} updated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
