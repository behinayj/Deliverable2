"""1.1.5 Clock: provide the current time."""
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime | None:
        """Current UTC time; None if no time is available."""


class SystemClock(Clock):
    def now(self) -> datetime | None:
        return datetime.now(timezone.utc)


class SimClock(Clock):
    def __init__(self, start: datetime | None = None):
        self._t = start or datetime.now(timezone.utc)

    def now(self) -> datetime | None:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += timedelta(seconds=max(0.0, seconds))
