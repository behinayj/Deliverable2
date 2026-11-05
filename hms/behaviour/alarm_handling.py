"""1.3.3 Alarm Handling: validate incoming alarms and keep the active alarm list (PS-ALM).

Secret: alarm types, severity mapping, clearing rule, list representation.
"""
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hms.hw.adc_protocol import ParsedMessage, ReceiptResult
from hms.sw.event_store import EventType


class Severity(Enum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


_SEVERITY = {"DOOR_FORCED": Severity.CRITICAL, "TEMPERATURE_OUT_OF_RANGE": Severity.CRITICAL,
             "DISPENSE_FAILURE": Severity.CRITICAL, "POWER_FAILURE": Severity.CRITICAL,
             "HARDWARE_FAULT": Severity.CRITICAL, "UNAUTHORIZED_ACCESS": Severity.CRITICAL,
             "DOOR_AJAR": Severity.WARNING, "LOW_STOCK": Severity.WARNING}


@dataclass(frozen=True)
class Alarm:
    adc_id: str
    alarm_id: str
    alarm_type: str
    severity: Severity
    raised_at: str | None
    received_at: datetime


class AlarmHandler:
    def __init__(self, registry, store):
        self._registry = registry
        self._store = store
        self._lock = threading.Lock()
        self._active: dict[tuple[str, str], Alarm] = {}

    def handle_alarm(self, msg: ParsedMessage, t: datetime) -> ReceiptResult:
        if not self._registry.is_registered(msg.adc_id):  # ALM-R1
            self._store.append(EventType.INVALID_MESSAGE, msg.raw_id or "unknown", t, details={"reason": "unregistered ADC"})
            return ReceiptResult.REJECTED
        if msg.alarm_id is None or msg.alarm_type not in _SEVERITY or msg.alarm_state != "ACTIVE":  # ALM-R3
            self._store.append(EventType.INVALID_MESSAGE, msg.adc_id, t, details={"reason": "malformed alarm"})
            return ReceiptResult.REJECTED
        key = (msg.adc_id, msg.alarm_id)
        with self._lock:
            if key in self._active:  # ALM-R4
                return ReceiptResult.ACCEPTED
            self._active[key] = Alarm(msg.adc_id, msg.alarm_id, msg.alarm_type, _SEVERITY[msg.alarm_type], msg.sent_at, t)  # ALM-R5
        self._store.append(EventType.ALARM_RECEIVED, msg.adc_id, t,
                           details={"alarmId": msg.alarm_id, "alarmType": msg.alarm_type, "raisedAt": msg.sent_at})
        return ReceiptResult.ACCEPTED

    def clear_alarm(self, msg: ParsedMessage, t: datetime) -> ReceiptResult:
        if not self._registry.is_registered(msg.adc_id):  # ALM-R1
            self._store.append(EventType.INVALID_MESSAGE, msg.raw_id or "unknown", t, details={"reason": "unregistered ADC"})
            return ReceiptResult.REJECTED
        if msg.alarm_id is None or msg.alarm_type not in _SEVERITY:  # ALM-R3
            self._store.append(EventType.INVALID_MESSAGE, msg.adc_id, t, details={"reason": "malformed alarm"})
            return ReceiptResult.REJECTED
        with self._lock:
            removed = self._active.pop((msg.adc_id, msg.alarm_id), None)
        if removed:  # ALM-R6; otherwise ALM-R7
            self._store.append(EventType.ALARM_CLEARED, msg.adc_id, t, details={"alarmId": msg.alarm_id})
        return ReceiptResult.ACCEPTED

    def active_alarms(self) -> list[Alarm]:
        with self._lock:
            return sorted(self._active.values(), key=lambda a: (a.severity is not Severity.CRITICAL, a.received_at))

    def restore(self, events: list) -> None:
        with self._lock:
            for e in events:
                key = (e.source, e.details.get("alarmId"))
                if e.type is EventType.ALARM_RECEIVED and e.details.get("alarmType") in _SEVERITY:
                    self._active[key] = Alarm(e.source, key[1], e.details["alarmType"], _SEVERITY[e.details["alarmType"]],
                                              e.details.get("raisedAt"), e.t_detect)
                elif e.type is EventType.ALARM_CLEARED:
                    self._active.pop(key, None)
