"""1.3.6 Access Policy: decide which roles may perform which operations.

Secret: the role-permission matrix (PS-ACC).
"""
from enum import Enum


class Operation(Enum):
    VIEW_ADC_DATA = "VIEW_ADC_DATA"
    VIEW_ALARMS = "VIEW_ALARMS"
    MONITOR_INVENTORY = "MONITOR_INVENTORY"
    CONFIGURE_THRESHOLDS = "CONFIGURE_THRESHOLDS"
    VIEW_ADC_HEALTH = "VIEW_ADC_HEALTH"
    CONTROL_MODE = "CONTROL_MODE"
    VIEW_HISTORICAL_DATA = "VIEW_HISTORICAL_DATA"
    GENERATE_REPORT = "GENERATE_REPORT"
    REGISTER_ADC = "REGISTER_ADC"
    MANAGE_USER_ACCESS = "MANAGE_USER_ACCESS"


_O = Operation
_PERM = {
    "NURSE": frozenset({_O.VIEW_ADC_DATA, _O.VIEW_ALARMS}),
    "PHARMACIST": frozenset({_O.VIEW_ADC_DATA, _O.MONITOR_INVENTORY, _O.CONFIGURE_THRESHOLDS, _O.VIEW_HISTORICAL_DATA}),
    "TECHNICIAN": frozenset({_O.VIEW_ADC_DATA, _O.VIEW_ALARMS, _O.VIEW_ADC_HEALTH, _O.CONTROL_MODE}),
    "HOSPITAL_ADMIN": frozenset({_O.VIEW_HISTORICAL_DATA, _O.GENERATE_REPORT}),
    "IT_SPECIALIST": frozenset({_O.GENERATE_REPORT, _O.REGISTER_ADC, _O.MANAGE_USER_ACCESS, _O.CONTROL_MODE}),
}


class AccessPolicy:
    def permitted(self, role, op: Operation) -> bool:
        return op in self.permissions(role)

    def permissions(self, role) -> frozenset:
        return _PERM.get(getattr(role, "value", role), frozenset())
