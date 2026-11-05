"""1.1.2 ADC Protocol: convert raw ADC messages to parsed values and replies to raw messages.

Secret: JSON field names, encoding, protocol version.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

PROTOCOL_VERSION = "1.0"


class MsgKind(Enum):
    HELLO = "HELLO"
    STATUS = "STATUS"
    ALARM = "ALARM"
    EVENT = "EVENT"
    UNKNOWN = "UNKNOWN"


class ReceiptResult(Enum):
    ACCEPTED = "ACCEPTED"
    IGNORED = "IGNORED"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class InventoryItem:
    med_id: str | None
    qty: object


@dataclass(frozen=True)
class HealthReport:
    overall: str | None
    door: str | None = None
    temperature_c: float | None = None
    power: str | None = None
    faults: tuple = ()


@dataclass(frozen=True)
class ParsedMessage:
    kind: MsgKind
    adc_id: str | None = None
    raw_id: str | None = None
    seq: int | None = None
    sent_at: str | None = None
    replayed: bool = False
    inventory: list | None = None
    health: HealthReport | None = None
    alarm_id: str | None = None
    alarm_type: str | None = None
    alarm_state: str | None = None
    event_type: str | None = None
    details: dict = field(default_factory=dict)
    errors: tuple = ()


def _str(v):
    return v if isinstance(v, str) and v else None


class AdcProtocol:
    def decode(self, raw: bytes) -> ParsedMessage:
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ParsedMessage(MsgKind.UNKNOWN, errors=("unreadable message",))
        if not isinstance(obj, dict):
            return ParsedMessage(MsgKind.UNKNOWN, errors=("not an object",))
        try:
            kind = MsgKind(obj.get("type"))
        except ValueError:
            kind = MsgKind.UNKNOWN
        seq = obj.get("seq")
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else None
        inventory = None
        if isinstance(obj.get("inventory"), list):
            inventory = [InventoryItem(_str(i.get("medId")), i.get("qty")) if isinstance(i, dict)
                         else InventoryItem(None, None) for i in obj["inventory"]]
        health = None
        h = obj.get("health")
        if isinstance(h, dict):
            t = h.get("temperatureC")
            health = HealthReport(_str(h.get("overall")), _str(h.get("door")),
                                  t if isinstance(t, (int, float)) else None, _str(h.get("power")),
                                  tuple(x for x in h.get("faults", []) if isinstance(x, str)) if isinstance(h.get("faults"), list) else ())
        details = obj.get("details") if isinstance(obj.get("details"), dict) else {}
        raw_id = obj.get("adcId")
        return ParsedMessage(kind=kind, adc_id=_str(raw_id), raw_id=None if raw_id is None else str(raw_id), seq=seq,
                             sent_at=_str(obj.get("sentAt")), replayed=obj.get("replayed") is True,
                             inventory=inventory, health=health, alarm_id=_str(obj.get("alarmId")),
                             alarm_type=_str(obj.get("alarmType")), alarm_state=_str(obj.get("state")),
                             event_type=_str(obj.get("eventType")), details=details)

    def encode_receipt(self, adc_id, seq, result: ReceiptResult, reason: str = "") -> bytes:
        return json.dumps({"type": "RECEIPT", "adcId": adc_id, "seq": seq, "result": result.value, "reason": reason}).encode()

    def encode_hello_ack(self, adc_id, accepted: bool, server_time: datetime) -> bytes:
        return json.dumps({"type": "HELLO_ACK", "adcId": adc_id, "accepted": accepted,
                           "serverTime": server_time.isoformat() if server_time else None}).encode()
