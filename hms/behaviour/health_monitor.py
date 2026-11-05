"""1.3.9 Health Monitor: keep the latest reported health of each ADC and classify it.

Secret: health fields and classification rules.
"""
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hms.behaviour import timing_policy as tp
from hms.behaviour.link_monitor import LinkStatus
from hms.sw.event_store import EventType

_REPORTED = {"OK", "WARNING", "FAULT"}


class HealthStatus(Enum):
    OK = "OK"
    WARNING = "WARNING"
    FAULT = "FAULT"
    INVALID = "INVALID"
    NOT_CURRENT = "NOT_CURRENT"
    AWAITING_DATA = "AWAITING_DATA"


@dataclass(frozen=True)
class HealthView:
    adc_id: str
    status: HealthStatus
    details: object
    reported_at: datetime | None


class HealthMonitor:
    def __init__(self, link_monitor, store):
        self._links = link_monitor
        self._store = store
        self._lock = threading.Lock()
        self._latest: dict[str, tuple] = {}  # adc -> (report, t)

    def update(self, adc_id: str, report, t: datetime) -> None:
        if report is None:
            return
        with self._lock:
            prev = self._latest.get(adc_id)
            self._latest[adc_id] = (report, t)
        if prev is None or prev[0].overall != report.overall:
            self._store.append(EventType.HEALTH_CHANGED, adc_id, t,
                               details={"overall": report.overall, "faults": list(report.faults)})

    def health(self, adc_id: str, now: datetime) -> HealthView:
        with self._lock:
            latest = self._latest.get(adc_id)
        if latest is None:
            return HealthView(adc_id, HealthStatus.AWAITING_DATA, None, None)
        report, t = latest
        if self._links.link_status(adc_id) is not LinkStatus.CONNECTED or now is None or now - t > tp.STALE_LIMIT:
            return HealthView(adc_id, HealthStatus.NOT_CURRENT, report, t)
        if report.overall not in _REPORTED:
            return HealthView(adc_id, HealthStatus.INVALID, report, t)
        return HealthView(adc_id, HealthStatus(report.overall), report, t)
