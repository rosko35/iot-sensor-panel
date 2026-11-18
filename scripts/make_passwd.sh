#!/usr/bin/env bash
# Add or update a user in mosquitto/config/passwd with the official
# mosquitto_passwd tool, run from the same image the broker uses.
#
#   scripts/make_passwd.sh esp32-kitchen          # prompts for the password
#
# Without Docker use: python scripts/make_passwd.py --user esp32-kitchen
set -euo pipefail

USER_NAME="${1:?usage: $0 <username>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="eclipse-mosquitto:2.0.20"
FILE="$ROOT/mosquitto/config/passwd"

export MSYS_NO_PATHCONV=1
touch "$FILE"
chmod 600 "$FILE"

docker run --rm -it \
  -v "$ROOT/mosquitto/config:/work" \
  "$IMAGE" mosquitto_passwd /work/passwd "$USER_NAME"

echo "updated $FILE (restart the broker to apply: docker compose restart mosquitto)"
