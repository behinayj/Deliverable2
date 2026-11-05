"""1.2.2 Report Generator: select events for a date range and produce reports.

Secret: date-range rule, CSV layout (time_utc, adc_id, event_type, user, details).
"""
import csv
import io
import json
from dataclasses import dataclass
from datetime import date

from hms.sw.event_store import InvalidRange


@dataclass(frozen=True)
class ReportFile:
    filename: str
    content: bytes
    media_type: str = "text/csv"


class ReportGenerator:
    def __init__(self, store, clock):
        self._store = store
        self._clock = clock

    def history(self, start: date, end: date, adc_id: str | None = None) -> list:
        today = self._clock.now().date()
        if start > end or end > today:
            raise InvalidRange("date range invalid")
        return self._store.query(start, end, adc_id)

    def export(self, start: date, end: date, adc_id: str | None = None) -> ReportFile:
        events = self.history(start, end, adc_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["time_utc", "adc_id", "event_type", "user", "details"])
        for e in events:
            w.writerow([e.t_detect.isoformat(), e.source, e.type.value, e.user or "", json.dumps(e.details)])
        name = f"hms_report_{start}_{end}" + (f"_{adc_id}" if adc_id else "") + ".csv"
        return ReportFile(name, buf.getvalue().encode())
