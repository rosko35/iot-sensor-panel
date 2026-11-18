#!/usr/bin/env python3
"""Generate a local certificate authority and a TLS server certificate for Mosquitto.

Cross-platform (Windows / macOS / Linux) alternative to ``gen_certs.sh`` that
only needs the ``cryptography`` package::

    python scripts/gen_certs.py                         # mosquitto, localhost, 127.0.0.1
    python scripts/gen_certs.py --ip 192.168.1.50 --dns my-pc.local   # + LAN access for ESP32

Writes to ``mosquitto/certs/``:

* ``ca.key`` / ``ca.crt``         - the local CA (keep ``ca.key`` private!)
* ``server.key`` / ``server.crt`` - the broker certificate signed by that CA

An existing CA is reused unless ``--force`` is given, so devices that already
trust ``ca.crt`` keep working when the server certificate is re-issued (e.g.
to add a new IP address). None of these files may be committed; they are
git-ignored.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_OUT = Path(__file__).resolve().parents[1] / "mosquitto" / "certs"
DEFAULT_DNS = ("mosquitto", "localhost")
DEFAULT_IPS = ("127.0.0.1", "::1")
ORG = "IoT Sensor Panel (local dev)"


def new_key(key_type: str):
    if key_type == "ec":
        return ec.generate_private_key(ec.SECP256R1())
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _write(path: Path, data: bytes, private: bool) -> None:
    path.write_bytes(data)
    if private:
        # Owner-only on POSIX; on Windows chmod only toggles the read-only bit.
        os.chmod(path, 0o600)


def _key_pem(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def build_ca(key, days: int) -> x509.Certificate:
    name = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
            x509.NameAttribute(NameOID.COMMON_NAME, "IoT Sensor Panel Local CA"),
        ]
    )
    now = _now()
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )


def build_server_cert(
    key, ca_key, ca_cert: x509.Certificate, dns: Sequence[str], ips: Sequence[str], days: int
) -> x509.Certificate:
    sans: list[x509.GeneralName] = [x509.DNSName(d) for d in dns]
    sans += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    now = _now()
    return (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(NameOID.ORGANIZATION_NAME, ORG),
                    x509.NameAttribute(NameOID.COMMON_NAME, dns[0] if dns else ips[0]),
                ]
            )
        )
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=isinstance(key, rsa.RSAPrivateKey),
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )


def generate(
    out: Path,
    *,
    dns: Sequence[str] = DEFAULT_DNS,
    ips: Sequence[str] = DEFAULT_IPS,
    days: int = 825,
    ca_days: int = 3650,
    key_type: str = "rsa",
    force: bool = False,
) -> dict[str, Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = {name: out / name for name in ("ca.key", "ca.crt", "server.key", "server.crt")}
    for ip in ips:
        ipaddress.ip_address(ip)  # fail early on typos

    if not force and paths["ca.key"].exists() and paths["ca.crt"].exists():
        ca_key = serialization.load_pem_private_key(paths["ca.key"].read_bytes(), password=None)
        ca_cert = x509.load_pem_x509_certificate(paths["ca.crt"].read_bytes())
        print(f"reusing existing CA {paths['ca.crt']}")
    else:
        ca_key = new_key(key_type)
        ca_cert = build_ca(ca_key, ca_days)
        _write(paths["ca.key"], _key_pem(ca_key), private=True)
        _write(paths["ca.crt"], ca_cert.public_bytes(serialization.Encoding.PEM), private=False)
        print(f"created CA {paths['ca.crt']}")

    server_key = new_key(key_type)
    server_cert = build_server_cert(server_key, ca_key, ca_cert, dns, ips, days)
    _write(paths["server.key"], _key_pem(server_key), private=True)
    _write(paths["server.crt"], server_cert.public_bytes(serialization.Encoding.PEM), private=False)
    print(f"created server certificate {paths['server.crt']}")
    print("  subjectAltName: " + ", ".join([*(f"DNS:{d}" for d in dns), *(f"IP:{i}" for i in ips)]))
    return paths


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument(
        "--dns", action="append", default=[], help="extra DNS subjectAltName (repeatable)"
    )
    parser.add_argument(
        "--ip", action="append", default=[], help="extra IP subjectAltName (repeatable)"
    )
    parser.add_argument("--days", type=int, default=825, help="server certificate validity")
    parser.add_argument("--ca-days", type=int, default=3650, help="CA validity")
    parser.add_argument("--key-type", choices=("rsa", "ec"), default="rsa")
    parser.add_argument("--force", action="store_true", help="also regenerate the CA")
    args = parser.parse_args(argv)

    dns = list(dict.fromkeys([*DEFAULT_DNS, *args.dns]))
    ips = list(dict.fromkeys([*DEFAULT_IPS, *args.ip]))
    try:
        generate(
            args.out,
            dns=dns,
            ips=ips,
            days=args.days,
            ca_days=args.ca_days,
            key_type=args.key_type,
            force=args.force,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Never commit these files. Copy only ca.crt to clients (ESP32 secrets.h).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
