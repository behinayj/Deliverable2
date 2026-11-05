"""1.1.3 ADC Simulator: synthetic ADC traffic and faults through the same interface as the ADC Link.

Secret: simulated device state, cadence (STATUS every 5 s) and injected faults.
"""
import json
import os
import queue
import threading
from datetime import timedelta
from enum import Enum

from hms.hw.adc_link import AdcLink, ChannelId, RawFrame


class SimFault(Enum):
    ALARM = "ALARM"
    CLEAR_ALARM = "CLEAR_ALARM"
    LOW_STOCK = "LOW_STOCK"
    BAD_QUANTITY = "BAD_QUANTITY"
    HEALTH_FAULT = "HEALTH_FAULT"
    HEALTH_OK = "HEALTH_OK"
    RESTOCK = "RESTOCK"
    LINK_LOSS = "LINK_LOSS"
    RESTORE = "RESTORE"
    DUPLICATE = "DUPLICATE"
    DAMAGED = "DAMAGED"
    UNREGISTERED_ID = "UNREGISTERED_ID"


_PERIOD = timedelta(seconds=5)
# LOW_STOCK alarms are raised and cleared by the LOW_STOCK / RESTOCK injections, so they stay consistent with inventory.
ALARM_TYPES = ("DOOR_FORCED", "DOOR_AJAR", "TEMPERATURE_OUT_OF_RANGE", "DISPENSE_FAILURE", "POWER_FAILURE",
               "HARDWARE_FAULT", "UNAUTHORIZED_ACCESS")
_STOCK = {"MED-ACET-500": 40, "MED-MORPH-10": 20}


class AdcSimulator(AdcLink):
    def __init__(self, clock, adc_ids=("ADC-01", "ADC-02", "ADC-03"), state_path: str = "data/sim_state.json"):
        self._clock = clock
        self._state_path = state_path
        self._queue: queue.Queue[RawFrame] = queue.Queue()
        self._lock = threading.Lock()
        self._adcs = {a: {"channel": i + 1, "seq": 0, "lost": False, "retained": [], "last": None,
                          "inventory": dict(_STOCK), "health": "OK", "alarms": [], "next_alarm": 1,
                          "last_status_at": None, "receipts": []}
                      for i, a in enumerate(adc_ids)}
        self._started = False
        self._load_state()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for a in self._adcs:
            self._emit(a, {"type": "HELLO", "protocolVersion": "1.0"}, seq=0)
            self._emit_status(a)

    def next_message(self) -> RawFrame | None:
        self._emit_due_statuses()
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def send(self, channel: ChannelId, raw: bytes) -> None:
        try:
            reply = json.loads(raw)
        except ValueError:
            return
        with self._lock:
            for s in self._adcs.values():
                if s["channel"] == channel:
                    s["receipts"] = ([reply] + s["receipts"])[:5]

    def inject(self, adc_id: str, fault: SimFault, alarm_type: str = "DOOR_FORCED") -> bool:
        if adc_id not in self._adcs or not isinstance(fault, SimFault):
            return False
        if fault in (SimFault.ALARM, SimFault.CLEAR_ALARM) and alarm_type not in ALARM_TYPES:
            return False
        with self._lock:
            ok = self._inject_locked(adc_id, fault, alarm_type)
            self._save_state()
        return ok

    def recent_receipts(self, adc_id: str) -> list[dict]:
        with self._lock:
            s = self._adcs.get(adc_id)
            return [dict(r) for r in s["receipts"]] if s else []

    def active_alarm_types(self, adc_id: str) -> list[str]:
        with self._lock:
            s = self._adcs.get(adc_id)
            return sorted({t for _, t in s["alarms"] if t in ALARM_TYPES}) if s else []

    def _inject_locked(self, adc_id, fault, alarm_type):
        s = self._adcs[adc_id]
        if fault is SimFault.ALARM:
            self._raise_locked(adc_id, alarm_type)
        elif fault is SimFault.CLEAR_ALARM:
            if not self._clear_locked(adc_id, alarm_type):
                return False
        elif fault is SimFault.LOW_STOCK:
            s["inventory"]["MED-MORPH-10"] = 2
            self._emit_status_locked(adc_id)
            if not any(t == "LOW_STOCK" for _, t in s["alarms"]):
                self._raise_locked(adc_id, "LOW_STOCK")
        elif fault is SimFault.BAD_QUANTITY:
            self._emit_status_locked(adc_id, override={"MED-MORPH-10": "abc"})
        elif fault in (SimFault.HEALTH_FAULT, SimFault.HEALTH_OK):
            s["health"] = "FAULT" if fault is SimFault.HEALTH_FAULT else "OK"
            self._emit_status_locked(adc_id)
        elif fault is SimFault.RESTOCK:
            s["inventory"] = dict(_STOCK)
            self._emit_status_locked(adc_id)
            while self._clear_locked(adc_id, "LOW_STOCK"):
                pass
        elif fault is SimFault.LINK_LOSS:
            s["lost"] = True
        elif fault is SimFault.RESTORE:
            s["lost"] = False
            for msg in s["retained"]:
                msg["replayed"] = True
                self._put(s, msg)
            s["retained"] = []
            self._emit_status_locked(adc_id)
        elif fault is SimFault.DUPLICATE:
            if s["last"] is None or s["lost"]:
                return False
            self._queue.put(RawFrame(s["last"], self._clock.now(), s["channel"]))
        elif fault is SimFault.DAMAGED:
            if s["lost"]:
                return False
            self._queue.put(RawFrame(b'{"type":"ALARM","adcId":', self._clock.now(), s["channel"]))
        elif fault is SimFault.UNREGISTERED_ID:
            body = {"type": "ALARM", "adcId": "ADC-99", "seq": 1, "sentAt": self._iso(), "replayed": False,
                    "alarmId": "A-1", "alarmType": "DOOR_FORCED", "state": "ACTIVE"}
            self._queue.put(RawFrame(json.dumps(body).encode(), self._clock.now(), s["channel"]))
        return True

    def _raise_locked(self, adc_id, alarm_type):
        s = self._adcs[adc_id]
        aid = f"{adc_id}-A{s['next_alarm']}"
        s["next_alarm"] += 1
        s["alarms"].append((aid, alarm_type))
        self._emit_locked(adc_id, {"type": "ALARM", "alarmId": aid, "alarmType": alarm_type, "state": "ACTIVE"})

    def _clear_locked(self, adc_id, alarm_type):
        s = self._adcs[adc_id]
        match = [a for a in s["alarms"] if a[1] == alarm_type]
        if not match:
            return False
        aid, atype = match[-1]
        s["alarms"].remove((aid, atype))
        self._emit_locked(adc_id, {"type": "ALARM", "alarmId": aid, "alarmType": atype, "state": "CLEARED"})
        return True

    def _load_state(self):
        try:
            with open(self._state_path, encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        for a, d in saved.items():
            if a in self._adcs:
                self._adcs[a].update(inventory=d["inventory"], health=d["health"], next_alarm=d["next_alarm"],
                                     alarms=[tuple(x) for x in d["alarms"]])

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(self._state_path) or ".", exist_ok=True)
            with open(self._state_path, "w", encoding="utf-8") as f:
                json.dump({a: {k: s[k] for k in ("inventory", "health", "next_alarm", "alarms")} for a, s in self._adcs.items()}, f)
        except OSError:
            pass

    def _emit_due_statuses(self):
        now = self._clock.now()
        with self._lock:
            for a, s in self._adcs.items():
                if self._started and (s["last_status_at"] is None or now - s["last_status_at"] >= _PERIOD):
                    self._emit_status_locked(a)

    def _emit_status(self, adc_id):
        with self._lock:
            self._emit_status_locked(adc_id)

    def _emit_status_locked(self, adc_id, override=None):
        s = self._adcs[adc_id]
        inv = dict(s["inventory"], **(override or {}))
        health = {"overall": s["health"], "door": "CLOSED", "temperatureC": 21.5, "power": "MAINS",
                  "faults": [] if s["health"] == "OK" else ["MOTOR_STALL"]}
        msg = {"type": "STATUS", "inventory": [{"medId": m, "qty": q} for m, q in inv.items()], "health": health}
        s["last_status_at"] = self._clock.now()
        if s["lost"]:
            return  # periodic snapshots are not retained during a link loss
        self._emit_locked(adc_id, msg)

    def _emit(self, adc_id, body, seq=None):
        with self._lock:
            self._emit_locked(adc_id, body, seq)

    def _emit_locked(self, adc_id, body, seq=None):
        s = self._adcs[adc_id]
        if seq is None:
            s["seq"] += 1
            seq = s["seq"]
        else:
            s["seq"] = seq
        msg = {"adcId": adc_id, "seq": seq, "sentAt": self._iso(), "replayed": False, **body}
        if s["lost"]:
            s["retained"].append(msg)
        else:
            self._put(s, msg)

    def _put(self, s, msg):
        raw = json.dumps(msg).encode()
        s["last"] = raw
        self._queue.put(RawFrame(raw, self._clock.now(), s["channel"]))

    def _iso(self):
        t = self._clock.now()
        return t.isoformat() if t else None
