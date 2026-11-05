"""1.3.2 ADC Registry: say which ADCs are registered.

Secret: registration file config/adcs.json.
"""
import json
import re
import threading
from dataclasses import dataclass
from datetime import timedelta

from hms.behaviour import timing_policy as tp

_ID = re.compile(r"^ADC-\d{2,3}$")


@dataclass(frozen=True)
class AdcInfo:
    adc_id: str
    location: str
    expected_period: timedelta


class InvalidAdcId(Exception):
    pass


class AlreadyRegistered(Exception):
    pass


class AdcRegistry:
    def __init__(self, path: str = "config/adcs.json"):
        self._path = path
        self._lock = threading.Lock()
        with open(path, encoding="utf-8") as f:
            self._adcs = {d["adcId"]: AdcInfo(d["adcId"], d.get("location", ""), timedelta(seconds=d.get("expectedPeriodS", 5)))
                          for d in json.load(f)}

    def is_registered(self, adc_id: str | None) -> bool:
        with self._lock:
            return adc_id is not None and adc_id in self._adcs

    def registered(self) -> list[str]:
        with self._lock:
            return sorted(self._adcs)

    def expected_period(self, adc_id) -> timedelta:
        with self._lock:
            info = self._adcs.get(adc_id)
            return info.expected_period if info else tp.DEFAULT_STATUS_PERIOD

    def register(self, adc_id: str, location: str, expected_period: timedelta = tp.DEFAULT_STATUS_PERIOD) -> None:
        if not isinstance(adc_id, str) or not _ID.match(adc_id):
            raise InvalidAdcId(adc_id)
        with self._lock:
            if adc_id in self._adcs:
                raise AlreadyRegistered(adc_id)
            self._adcs[adc_id] = AdcInfo(adc_id, location, expected_period)
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump([{"adcId": a.adc_id, "location": a.location, "expectedPeriodS": int(a.expected_period.total_seconds())}
                           for a in self._adcs.values()], f, indent=2)
