"""1.2.1 Event Store: durably record and retrieve safety-relevant events.

Secret: in-memory log persisted to an append-only JSON-lines file (data/events.jsonl),
in-memory write buffer (CAP = 1000), retention purge.
"""
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum

from hms.behaviour import timing_policy as tp

_CAP = 1000


class EventType(Enum):
    ALARM_RECEIVED = "ALARM_RECEIVED"
    ALARM_CLEARED = "ALARM_CLEARED"
    INVALID_MESSAGE = "INVALID_MESSAGE"
    COMM_ESTABLISHED = "COMM_ESTABLISHED"
    COMM_LOST = "COMM_LOST"
    COMM_RESTORED = "COMM_RESTORED"
    INVENTORY_ATTENTION = "INVENTORY_ATTENTION"
    INVENTORY_INVALID = "INVENTORY_INVALID"
    HEALTH_CHANGED = "HEALTH_CHANGED"
    ACCESS_DENIED = "ACCESS_DENIED"
    LOGIN_SUCCESS = "LOGIN_SUCCESS"
    LOGIN_FAILURE = "LOGIN_FAILURE"
    MODE_CHANGED = "MODE_CHANGED"
    STATUS_RECEIVED = "STATUS_RECEIVED"
    ADC_EVENT = "ADC_EVENT"
    LOGOUT = "LOGOUT"
    REPORT_EXPORTED = "REPORT_EXPORTED"
    ADC_REGISTERED = "ADC_REGISTERED"
    THRESHOLD_CHANGED = "THRESHOLD_CHANGED"
    USER_CHANGED = "USER_CHANGED"
    MODE_EVENT_REJECTED = "MODE_EVENT_REJECTED"


class StorageHealth(Enum):
    AVAILABLE = "AVAILABLE"
    BUFFERING = "BUFFERING"
    FULL = "FULL"


class InvalidRange(Exception):
    pass


class HistoryUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Event:
    event_id: int
    type: EventType
    source: str
    t_detect: datetime
    user: str | None = None
    details: dict = field(default_factory=dict)


class EventStore:
    def __init__(self, clock, path: str = "data/events.jsonl"):
        self._clock = clock
        self._path = path
        self._lock = threading.Lock()
        self._log: list[Event] = []
        self._buffer: list[Event] = []
        self._dropped = 0
        self._next_id = 1
        self._load()

    def append(self, event_type: EventType, source: str, t_detect: datetime, user: str | None = None,
               details: dict | None = None) -> None:
        with self._lock:
            e = Event(self._next_id, event_type, source or "unknown", t_detect, user, dict(details or {}))
            self._next_id += 1
            if len(self._buffer) >= _CAP:
                self._dropped += 1
                return
            self._buffer.append(e)
            self._flush_locked()

    def query(self, start: date, end: date, adc_id: str | None = None, types: set | None = None) -> list[Event]:
        if start > end:
            raise InvalidRange("invalid range")
        with self._lock:
            if self._buffer:
                raise HistoryUnavailable("unavailable")
            return [e for e in self._log
                    if start <= e.t_detect.date() <= end
                    and (adc_id is None or e.source == adc_id)
                    and (types is None or e.type in types)]

    def purge(self, now) -> int:
        with self._lock:
            keep = [e for e in self._log if now - e.t_detect <= tp.RETENTION]
            n = len(self._log) - len(keep)
            if n:
                self._log = keep
                self._rewrite_locked()
            return n

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def storage_health(self) -> StorageHealth:
        with self._lock:
            if self._dropped or len(self._buffer) >= _CAP:
                return StorageHealth.FULL
            return StorageHealth.BUFFERING if self._buffer else StorageHealth.AVAILABLE

    def _flush_locked(self):
        if not self._buffer:
            return
        try:
            with open(self._path, "a", encoding="utf-8") as f:
                for e in self._buffer:
                    f.write(json.dumps(self._to_json(e)) + "\n")
        except OSError:
            return
        self._log.extend(self._buffer)
        self._buffer = []

    def _rewrite_locked(self):
        try:
            with open(self._path, "w", encoding="utf-8") as f:
                for e in self._log:
                    f.write(json.dumps(self._to_json(e)) + "\n")
        except OSError:
            pass

    def _load(self):
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        if not os.path.exists(self._path):
            return
        with open(self._path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    e = Event(d["id"], EventType(d["type"]), d["source"], datetime.fromisoformat(d["t"]), d.get("user"), d.get("details", {}))
                except (ValueError, KeyError):
                    continue
                self._log.append(e)
                self._next_id = max(self._next_id, e.event_id + 1)

    def _to_json(self, e):
        return {"id": e.event_id, "type": e.type.value, "source": e.source, "t": e.t_detect.isoformat(), "user": e.user, "details": e.details}
