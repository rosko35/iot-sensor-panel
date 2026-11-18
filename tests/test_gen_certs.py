from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import sys

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import gen_certs


def load(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def sans(cert):
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return (
        set(ext.get_values_for_type(x509.DNSName)),
        {str(ip) for ip in ext.get_values_for_type(x509.IPAddress)},
    )


@pytest.fixture
def certs(tmp_path):
    return gen_certs.generate(
        tmp_path, dns=["mosquitto", "localhost", "pi.local"], ips=["127.0.0.1", "192.168.1.50"]
    )


def test_files_are_created(certs):
    assert {p.name for p in certs.values()} == {"ca.key", "ca.crt", "server.key", "server.crt"}
    assert b"PRIVATE KEY" in certs["server.key"].read_bytes()


def test_server_certificate_is_signed_by_ca(certs):
    ca, server = load(certs["ca.crt"]), load(certs["server.crt"])
    server.verify_directly_issued_by(ca)
    assert server.issuer == ca.subject


def test_ca_extensions(certs):
    ca = load(certs["ca.crt"])
    bc = ca.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca
    assert ca.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign


def test_server_extensions_and_sans(certs):
    server = load(certs["server.crt"])
    assert not server.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    eku = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert x509.oid.ExtendedKeyUsageOID.SERVER_AUTH in eku
    dns, ips = sans(server)
    assert dns == {"mosquitto", "localhost", "pi.local"}
    assert ips == {"127.0.0.1", "192.168.1.50"}
    assert isinstance(server.public_key(), rsa.RSAPublicKey)


def test_existing_ca_is_reused_unless_forced(tmp_path):
    first = gen_certs.generate(tmp_path)
    ca_before = first["ca.crt"].read_bytes()
    server_before = first["server.crt"].read_bytes()
    gen_certs.generate(tmp_path, ips=["10.0.0.5"])
    assert first["ca.crt"].read_bytes() == ca_before
    assert first["server.crt"].read_bytes() != server_before
    load(first["server.crt"]).verify_directly_issued_by(load(first["ca.crt"]))
    gen_certs.generate(tmp_path, force=True)
    assert first["ca.crt"].read_bytes() != ca_before


def test_ec_keys(tmp_path):
    paths = gen_certs.generate(tmp_path, key_type="ec")
    assert isinstance(load(paths["server.crt"]).public_key(), ec.EllipticCurvePublicKey)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_private_keys_are_owner_only(certs):
    assert certs["ca.key"].stat().st_mode & 0o077 == 0
    assert certs["server.key"].stat().st_mode & 0o077 == 0


def test_cli_adds_extra_sans(tmp_path, capsys):
    assert gen_certs.main(["--out", str(tmp_path), "--ip", "10.1.2.3", "--dns", "box"]) == 0
    dns, ips = sans(load(tmp_path / "server.crt"))
    assert {"mosquitto", "localhost", "box"} <= dns
    assert ipaddress.ip_address("10.1.2.3") in {ipaddress.ip_address(i) for i in ips}
    assert "Never commit" in capsys.readouterr().out


def test_cli_rejects_bad_ip(tmp_path):
    assert gen_certs.main(["--out", str(tmp_path), "--ip", "999.1.1.1"]) == 2


@pytest.mark.skipif(
    not (shutil.which("bash") and shutil.which("openssl")), reason="needs bash and openssl"
)
def test_shell_script_produces_a_verifiable_chain(root, tmp_path):
    env = {**os.environ, "OUT": tmp_path.as_posix(), "EXTRA_SANS": "IP:192.168.1.50"}
    script = (root / "scripts/gen_certs.sh").as_posix()
    result = subprocess.run(
        ["bash", script], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr
    ca, server = load(tmp_path / "ca.crt"), load(tmp_path / "server.crt")
    server.verify_directly_issued_by(ca)
    dns, ips = sans(server)
    assert {"mosquitto", "localhost"} <= dns
    assert "192.168.1.50" in ips


def test_script_runs_standalone(root, tmp_path):
    result = subprocess.run(
        [sys.executable, str(root / "scripts/gen_certs.py"), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "server.crt").exists()
