"""1.3.4 Link Monitor: track per-ADC communication and mark data stale (PS-STL).

Secret: freshness rule; how last-update times are kept.
"""
import threading
from datetime import datetime, timedelta
from enum import Enum

from hms.behaviour import timing_policy as tp
from hms.sw.event_store import EventType


class LinkStatus(Enum):
    AWAITING_DATA = "AWAITING_DATA"
    CONNECTED = "CONNECTED"
    LOST = "LOST"


class LinkMonitor:
    def __init__(self, registry, store, clock):
        self._registry = registry
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._last_update: dict[str, datetime] = {}
        self._last_status: dict[str, datetime] = {}
        self._status: dict[str, LinkStatus] = {}

    def record_arrival(self, adc_id: str, t: datetime, is_status: bool) -> None:
        with self._lock:
            self._last_update[adc_id] = t
            if is_status:
                self._last_status[adc_id] = t

    def refresh(self, adc_id: str, now: datetime | None) -> LinkStatus:
        with self._lock:
            old = self._status.get(adc_id, LinkStatus.AWAITING_DATA)
            last = self._last_update.get(adc_id)
            if last is None:  # STL-R1
                new = LinkStatus.AWAITING_DATA
            elif now is None or now - last > tp.STALE_LIMIT:  # STL-R2
                new = LinkStatus.LOST
            else:  # STL-R3
                new = LinkStatus.CONNECTED
            self._status[adc_id] = new
        t = now or last
        if new is LinkStatus.LOST and old is LinkStatus.CONNECTED:
            self._store.append(EventType.COMM_LOST, adc_id, t)
        elif new is LinkStatus.CONNECTED and old is LinkStatus.LOST:
            self._store.append(EventType.COMM_RESTORED, adc_id, t)
        elif new is LinkStatus.CONNECTED and old is LinkStatus.AWAITING_DATA:
            self._store.append(EventType.COMM_ESTABLISHED, adc_id, t)
        return new

    def link_status(self, adc_id) -> LinkStatus:
        with self._lock:
            return self._status.get(adc_id, LinkStatus.AWAITING_DATA)

    def data_age(self, adc_id, now) -> timedelta | None:
        with self._lock:
            last = self._last_status.get(adc_id)
        return None if last is None or now is None else now - last

    def is_data_current(self, adc_id, now) -> bool:
        age = self.data_age(adc_id, now)
        return age is not None and age <= tp.STALE_LIMIT and self.link_status(adc_id) is LinkStatus.CONNECTED
