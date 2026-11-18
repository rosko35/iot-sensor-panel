"""Clock abstraction so time-dependent logic can be tested with a fake clock."""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Return the current time as Unix seconds."""
        ...


class SystemClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    """Manually advanced clock for tests and deterministic simulations."""

    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self._now = float(start)

    def now(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot go backwards")
        self._now += seconds

    def set(self, value: float) -> None:
        self._now = float(value)
