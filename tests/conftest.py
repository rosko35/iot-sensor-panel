from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from iot_panel.clock import FakeClock

ROOT = Path(__file__).resolve().parents[1]


class FakeWriter:
    """Stand-in for influxdb_client's WriteApi that records line protocol."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    def write(self, bucket: str, org: str, record: Any, **kwargs: Any) -> None:
        self.records.append((bucket, org, record))

    @property
    def lines(self) -> list[str]:
        return [rec.to_line_protocol() for _, _, rec in self.records]


@pytest.fixture
def root() -> Path:
    return ROOT


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1_750_000_000.0)


@pytest.fixture
def writer() -> FakeWriter:
    return FakeWriter()


def payload(**overrides: Any) -> bytes:
    data: dict[str, Any] = {
        "v": 1,
        "device_id": "dev-1",
        "ts": 1_750_000_000.0,
        "seq": 1,
        "temperature": 21.5,
        "humidity": 40.0,
        "motion": False,
        "rssi": -60,
        "fw": "test",
    }
    for key, value in overrides.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return json.dumps(data).encode()
