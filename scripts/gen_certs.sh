#!/usr/bin/env bash
# Generate a local CA and a Mosquitto server certificate with openssl.
#
#   scripts/gen_certs.sh                                   # mosquitto, localhost, 127.0.0.1
#   EXTRA_SANS="DNS:my-pc.local,IP:192.168.1.50" scripts/gen_certs.sh
#   FORCE=1 scripts/gen_certs.sh                           # also regenerate the CA
#
# Output (git-ignored): mosquitto/certs/{ca.key,ca.crt,server.key,server.crt}
# Cross-platform alternative without openssl: python scripts/gen_certs.py
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${OUT:-$ROOT/mosquitto/certs}"
DAYS="${DAYS:-825}"
CA_DAYS="${CA_DAYS:-3650}"
SANS="DNS:mosquitto,DNS:localhost,IP:127.0.0.1,IP:0:0:0:0:0:0:0:1"
if [[ -n "${EXTRA_SANS:-}" ]]; then
  SANS="$SANS,$EXTRA_SANS"
fi
ORG="IoT Sensor Panel (local dev)"

command -v openssl >/dev/null || { echo "openssl not found" >&2; exit 1; }
mkdir -p "$OUT"
umask 077
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Run openssl quietly, but show its output if it fails.
ossl() {
  if ! openssl "$@" >"$TMP/openssl.log" 2>&1; then
    cat "$TMP/openssl.log" >&2
    echo "openssl $1 failed" >&2
    exit 1
  fi
}

# Subjects go through a config file instead of -subj "/O=..." because Git Bash
# on Windows rewrites an argument with a leading "/" into a file path.
subject_config() {
  printf '[req]\nprompt = no\ndistinguished_name = dn\n[dn]\nO = %s\nCN = %s\n' \
    "$ORG" "$2" >"$TMP/$1.cnf"
}

if [[ "${FORCE:-0}" == "1" || ! -f "$OUT/ca.key" || ! -f "$OUT/ca.crt" ]]; then
  printf '%s\n' \
    "basicConstraints = critical, CA:TRUE, pathlen:0" \
    "keyUsage = critical, digitalSignature, keyCertSign, cRLSign" \
    "subjectKeyIdentifier = hash" >"$TMP/ca.ext"
  subject_config ca "IoT Sensor Panel Local CA"
  ossl req -new -newkey rsa:2048 -nodes -sha256 -config "$TMP/ca.cnf" \
    -keyout "$OUT/ca.key" -out "$TMP/ca.csr"
  ossl x509 -req -in "$TMP/ca.csr" -signkey "$OUT/ca.key" -sha256 \
    -days "$CA_DAYS" -extfile "$TMP/ca.ext" -out "$OUT/ca.crt"
  echo "created CA $OUT/ca.crt"
else
  echo "reusing existing CA $OUT/ca.crt"
fi

printf '%s\n' \
  "basicConstraints = critical, CA:FALSE" \
  "keyUsage = critical, digitalSignature, keyEncipherment" \
  "extendedKeyUsage = serverAuth" \
  "subjectAltName = $SANS" \
  "subjectKeyIdentifier = hash" \
  "authorityKeyIdentifier = keyid" >"$TMP/server.ext"
subject_config server "mosquitto"
ossl req -new -newkey rsa:2048 -nodes -sha256 -config "$TMP/server.cnf" \
  -keyout "$OUT/server.key" -out "$TMP/server.csr"
ossl x509 -req -in "$TMP/server.csr" -CA "$OUT/ca.crt" -CAkey "$OUT/ca.key" \
  -set_serial "0x$(openssl rand -hex 16)" -sha256 -days "$DAYS" \
  -extfile "$TMP/server.ext" -out "$OUT/server.crt"

chmod 600 "$OUT/ca.key" "$OUT/server.key"
chmod 644 "$OUT/ca.crt" "$OUT/server.crt"

openssl verify -CAfile "$OUT/ca.crt" "$OUT/server.crt"
echo "created server certificate $OUT/server.crt"
echo "  subjectAltName: $SANS"
echo "Never commit these files. Copy only ca.crt to clients (ESP32 secrets.h)."
