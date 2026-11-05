"""1.3.5 Inventory Monitor: decide the inventory status of each medication in each ADC (PS-INV).

Secret: threshold representation (config/thresholds.json), MAX_QTY, quantity-validity rule.
"""
import json
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from hms.behaviour import timing_policy as tp
from hms.behaviour.link_monitor import LinkStatus
from hms.sw.event_store import EventType

_MAX_QTY = 10_000


class InventoryStatus(Enum):
    NORMAL = "NORMAL"
    ATTENTION_REQUIRED = "ATTENTION_REQUIRED"
    INVALID = "INVALID"
    NOT_CURRENT = "NOT_CURRENT"


@dataclass(frozen=True)
class InventoryRow:
    adc_id: str
    med_id: str
    qty: int | None
    threshold: int | None
    status: InventoryStatus
    reported_at: datetime | None


class InventoryMonitor:
    def __init__(self, link_monitor, store, thresholds_path: str = "config/thresholds.json"):
        self._links = link_monitor
        self._store = store
        self._path = thresholds_path
        self._lock = threading.Lock()
        with open(thresholds_path, encoding="utf-8") as f:
            self._threshold: dict[str, int] = json.load(f)
        self._rows: dict[tuple[str, str], dict] = {}  # (adc, med) -> qty, rpt_at, rpt_valid

    def refresh(self, adc_id: str, med_id: str, report, t: datetime) -> InventoryStatus:
        if report is None or med_id is None:
            if med_id is None and report is not None:
                self._store.append(EventType.INVENTORY_INVALID, adc_id, t, details={"reason": "missing medId"})
                return InventoryStatus.INVALID
            return self.status(adc_id, med_id, t)  # INV-R1
        before = self.status(adc_id, med_id, t)
        with self._lock:
            row = self._rows.setdefault((adc_id, med_id), {"qty": None, "rpt_at": None, "rpt_valid": False})
            row["rpt_at"] = t
            row["rpt_valid"] = self._valid(med_id, report.qty)
            if row["rpt_valid"]:  # INV-R3
                row["qty"] = report.qty
        if not row["rpt_valid"]:  # INV-R2
            self._store.append(EventType.INVENTORY_INVALID, adc_id, t, details={"medId": med_id, "qty": repr(report.qty)})
        after = self.status(adc_id, med_id, t)
        if after is InventoryStatus.ATTENTION_REQUIRED and before is not InventoryStatus.ATTENTION_REQUIRED:
            self._store.append(EventType.INVENTORY_ATTENTION, adc_id, t, details={"medId": med_id, "qty": report.qty})
        return after

    def status(self, adc_id, med_id, now) -> InventoryStatus:
        with self._lock:
            row = self._rows.get((adc_id, med_id))
            threshold = self._threshold.get(med_id)
        if (row is None or row["rpt_at"] is None or now is None or now - row["rpt_at"] > tp.STALE_LIMIT
                or self._links.link_status(adc_id) is not LinkStatus.CONNECTED):  # INV-S1
            return InventoryStatus.NOT_CURRENT
        if not row["rpt_valid"] or threshold is None:  # INV-S2
            return InventoryStatus.INVALID
        if row["qty"] < threshold:  # INV-S3
            return InventoryStatus.ATTENTION_REQUIRED
        return InventoryStatus.NORMAL  # INV-S4

    def snapshot(self, adc_id, now) -> list[InventoryRow]:
        with self._lock:
            keys = sorted(k for k in self._rows if k[0] == adc_id)
            data = {k: dict(self._rows[k]) for k in keys}
            thr = dict(self._threshold)
        return [InventoryRow(a, m, data[(a, m)]["qty"], thr.get(m), self.status(a, m, now), data[(a, m)]["rpt_at"]) for a, m in keys]

    def adcs_requiring_attention(self, now) -> list[str]:
        with self._lock:
            keys = list(self._rows)
        return sorted({a for a, m in keys if self.status(a, m, now) is InventoryStatus.ATTENTION_REQUIRED})

    def set_threshold(self, med_id: str, threshold: int) -> None:
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 0:
            raise ValueError("threshold must be a non-negative integer")
        with self._lock:
            self._threshold[med_id] = threshold
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(self._threshold, f, indent=2)

    def _valid(self, med_id, qty):
        return (med_id in self._threshold and isinstance(qty, int) and not isinstance(qty, bool)
                and 0 <= qty <= _MAX_QTY)
