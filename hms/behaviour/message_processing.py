"""1.3.7 Message Processing: route each incoming message to the right module, one ADC at a time (PS-ISO).

Secret: routing order, per-ADC lastSeq table, replay handling.
"""
import threading
from datetime import datetime

from hms.hw.adc_link import RawFrame
from hms.hw.adc_protocol import MsgKind, ReceiptResult
from hms.sw.event_store import EventType


class MessageProcessor:
    def __init__(self, link, protocol, registry, link_monitor, alarms, inventory, health, store, clock):
        self._link, self._protocol, self._registry = link, protocol, registry
        self._links, self._alarms, self._inventory, self._health = link_monitor, alarms, inventory, health
        self._store, self._clock = store, clock
        self._lock = threading.Lock()
        self._last_seq: dict[str, int] = {}

    def receive(self, frame: RawFrame) -> ReceiptResult:
        with self._lock:
            return self._receive_locked(frame)

    def poll(self) -> int:
        n = 0
        while (frame := self._link.next_message()) is not None:
            self.receive(frame)
            n += 1
        return n

    def tick(self, now: datetime) -> None:
        for adc in self._registry.registered():
            self._links.refresh(adc, now)

    def _receive_locked(self, frame):
        m = self._protocol.decode(frame.data)
        t, a = frame.arrived_at, m.adc_id
        if m.kind is MsgKind.UNKNOWN or not self._registry.is_registered(a) or m.seq is None:  # ISO-R1
            reason = "unreadable" if m.kind is MsgKind.UNKNOWN else ("unregistered ADC" if not self._registry.is_registered(a) else "missing seq")
            self._store.append(EventType.INVALID_MESSAGE, m.raw_id or "unknown", t, details={"reason": reason})
            if m.kind is MsgKind.HELLO:
                self._link.send(frame.channel, self._protocol.encode_hello_ack(a, False, t))
            else:
                self._link.send(frame.channel, self._protocol.encode_receipt(a, m.seq, ReceiptResult.REJECTED, reason))
            return ReceiptResult.REJECTED
        if m.kind is MsgKind.HELLO:  # ISO-R2
            self._last_seq[a] = m.seq
            self._links.record_arrival(a, t, False)
            self._links.refresh(a, t)
            self._link.send(frame.channel, self._protocol.encode_hello_ack(a, True, t))
            return ReceiptResult.ACCEPTED
        if m.seq <= self._last_seq.get(a, 0):  # ISO-R3
            self._link.send(frame.channel, self._protocol.encode_receipt(a, m.seq, ReceiptResult.IGNORED))
            return ReceiptResult.IGNORED
        # ISO-R4
        self._last_seq[a] = m.seq
        fresh_status = m.kind is MsgKind.STATUS and not m.replayed
        self._links.record_arrival(a, t, fresh_status)
        self._links.refresh(a, t)
        result = ReceiptResult.ACCEPTED
        if m.kind is MsgKind.STATUS:
            if fresh_status:
                reported = {i.med_id for i in (m.inventory or [])}
                for item in m.inventory or []:
                    self._inventory.refresh(a, item.med_id, item, t)
                for row in self._inventory.snapshot(a, t):
                    if row.med_id not in reported:
                        self._inventory.refresh(a, row.med_id, None, t)
                self._health.update(a, m.health, t)
            self._store.append(EventType.STATUS_RECEIVED, a, t, details={
                "replayed": m.replayed, "inventory": {i.med_id: i.qty for i in (m.inventory or []) if i.med_id},
                "health": m.health.overall if m.health else None})
        elif m.kind is MsgKind.ALARM:
            result = self._alarms.clear_alarm(m, t) if m.alarm_state == "CLEARED" else self._alarms.handle_alarm(m, t)
        elif m.kind is MsgKind.EVENT:
            self._store.append(EventType.ADC_EVENT, a, t, details={"eventType": m.event_type, "replayed": m.replayed, **m.details})
        self._link.send(frame.channel, self._protocol.encode_receipt(a, m.seq, result))
        return result
