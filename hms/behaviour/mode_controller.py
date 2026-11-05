"""1.3.10 Mode Controller: hold the HMS operating mode and apply the mode transition table.

Secret: modes, events and the TRANSITIONS table.
"""
import threading
from datetime import datetime
from enum import Enum

from hms.sw.event_store import EventType, StorageHealth


class Mode(Enum):
    INITIALIZING = "INITIALIZING"
    NORMAL = "NORMAL"
    DEGRADED = "DEGRADED"
    MAINTENANCE = "MAINTENANCE"
    FAULT = "FAULT"


class ModeEvent(Enum):
    SELF_CHECK_OK = "SELF_CHECK_OK"
    SELF_CHECK_FAIL = "SELF_CHECK_FAIL"
    STORAGE_DOWN = "STORAGE_DOWN"
    STORAGE_UP = "STORAGE_UP"
    ENTER_MAINTENANCE = "ENTER_MAINTENANCE"
    EXIT_MAINTENANCE = "EXIT_MAINTENANCE"
    CRITICAL_FAULT = "CRITICAL_FAULT"
    RESTART = "RESTART"


_IGNORE, _ILLEGAL, _GUARDED = "Ignore", "Illegal", "Guarded"
_M, _E = Mode, ModeEvent


def storage_available(store) -> bool:
    return store.storage_health() is StorageHealth.AVAILABLE


TRANSITIONS = {
    (_M.INITIALIZING, _E.SELF_CHECK_OK): _M.NORMAL, (_M.INITIALIZING, _E.SELF_CHECK_FAIL): _M.FAULT,
    (_M.INITIALIZING, _E.STORAGE_DOWN): _IGNORE, (_M.INITIALIZING, _E.STORAGE_UP): _IGNORE,
    (_M.INITIALIZING, _E.ENTER_MAINTENANCE): _ILLEGAL, (_M.INITIALIZING, _E.EXIT_MAINTENANCE): _ILLEGAL,
    (_M.INITIALIZING, _E.CRITICAL_FAULT): _M.FAULT, (_M.INITIALIZING, _E.RESTART): _ILLEGAL,

    (_M.NORMAL, _E.SELF_CHECK_OK): _ILLEGAL, (_M.NORMAL, _E.SELF_CHECK_FAIL): _ILLEGAL,
    (_M.NORMAL, _E.STORAGE_DOWN): _M.DEGRADED, (_M.NORMAL, _E.STORAGE_UP): _IGNORE,
    (_M.NORMAL, _E.ENTER_MAINTENANCE): _M.MAINTENANCE, (_M.NORMAL, _E.EXIT_MAINTENANCE): _IGNORE,
    (_M.NORMAL, _E.CRITICAL_FAULT): _M.FAULT, (_M.NORMAL, _E.RESTART): _ILLEGAL,

    (_M.DEGRADED, _E.SELF_CHECK_OK): _ILLEGAL, (_M.DEGRADED, _E.SELF_CHECK_FAIL): _ILLEGAL,
    (_M.DEGRADED, _E.STORAGE_DOWN): _IGNORE, (_M.DEGRADED, _E.STORAGE_UP): _M.NORMAL,
    (_M.DEGRADED, _E.ENTER_MAINTENANCE): _M.MAINTENANCE, (_M.DEGRADED, _E.EXIT_MAINTENANCE): _IGNORE,
    (_M.DEGRADED, _E.CRITICAL_FAULT): _M.FAULT, (_M.DEGRADED, _E.RESTART): _ILLEGAL,

    (_M.MAINTENANCE, _E.SELF_CHECK_OK): _ILLEGAL, (_M.MAINTENANCE, _E.SELF_CHECK_FAIL): _ILLEGAL,
    (_M.MAINTENANCE, _E.STORAGE_DOWN): _IGNORE, (_M.MAINTENANCE, _E.STORAGE_UP): _IGNORE,
    (_M.MAINTENANCE, _E.ENTER_MAINTENANCE): _IGNORE, (_M.MAINTENANCE, _E.EXIT_MAINTENANCE): _GUARDED,
    (_M.MAINTENANCE, _E.CRITICAL_FAULT): _M.FAULT, (_M.MAINTENANCE, _E.RESTART): _ILLEGAL,

    (_M.FAULT, _E.SELF_CHECK_OK): _ILLEGAL, (_M.FAULT, _E.SELF_CHECK_FAIL): _ILLEGAL,
    (_M.FAULT, _E.STORAGE_DOWN): _IGNORE, (_M.FAULT, _E.STORAGE_UP): _IGNORE,
    (_M.FAULT, _E.ENTER_MAINTENANCE): _ILLEGAL, (_M.FAULT, _E.EXIT_MAINTENANCE): _ILLEGAL,
    (_M.FAULT, _E.CRITICAL_FAULT): _IGNORE, (_M.FAULT, _E.RESTART): _M.INITIALIZING,
}


class ModeController:
    def __init__(self, store, clock):
        self._store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._mode = Mode.INITIALIZING

    def handle(self, event: ModeEvent, now: datetime, user: str | None = None) -> bool:
        if not isinstance(event, ModeEvent):
            return False
        with self._lock:
            old = self._mode
            entry = TRANSITIONS[(old, event)]
            if entry == _GUARDED:
                entry = Mode.NORMAL if storage_available(self._store) else Mode.DEGRADED
            if isinstance(entry, Mode):
                self._mode = entry
        if entry == _ILLEGAL:
            self._store.append(EventType.MODE_EVENT_REJECTED, "HMS", now, user, {"mode": old.value, "event": event.value})
            return False
        if isinstance(entry, Mode) and entry is not old:
            self._store.append(EventType.MODE_CHANGED, "HMS", now, user, {"from": old.value, "to": entry.value, "event": event.value})
        return True

    def current_mode(self) -> Mode:
        with self._lock:
            return self._mode

    def tick(self, now: datetime) -> None:
        health = self._store.storage_health()
        if health is StorageHealth.FULL:
            self.handle(ModeEvent.CRITICAL_FAULT, now)
        elif health is StorageHealth.BUFFERING:
            self.handle(ModeEvent.STORAGE_DOWN, now)
        else:
            self.handle(ModeEvent.STORAGE_UP, now)
