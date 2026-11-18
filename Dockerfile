# One image for both the ingest service and the simulator.
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --no-create-home app

# Default alert rules; docker-compose.yml mounts ./config over this directory.
COPY config ./config

USER app

# Overridden per service in docker-compose.yml.
CMD ["iot-ingest"]
