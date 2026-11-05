"""1.3.8 Operator Service: perform authorized operator operations.

Secret: workflow ordering of authorization, retrieval and view content.
"""
from dataclasses import dataclass, field
from datetime import timedelta

from hms.behaviour.access_policy import Operation
from hms.behaviour.adc_registry import AlreadyRegistered, InvalidAdcId
from hms.behaviour.health_monitor import HealthStatus
from hms.behaviour.link_monitor import LinkStatus
from hms.behaviour.mode_controller import Mode, ModeEvent
from hms.sw.authentication import AuthUnavailable, InvalidCredentials, Role, RoleNotAuthorized
from hms.sw.event_store import EventType, HistoryUnavailable, InvalidRange

_BANNER = {Mode.INITIALIZING: "Starting - data not yet available", Mode.NORMAL: "",
           Mode.DEGRADED: "Historical information unavailable - events are being buffered",
           Mode.MAINTENANCE: "MAINTENANCE - not for clinical decisions",
           Mode.FAULT: "FAULT - data not trusted; contact technician"}
_USER_MODE_EVENTS = {ModeEvent.ENTER_MAINTENANCE, ModeEvent.EXIT_MAINTENANCE, ModeEvent.RESTART}


@dataclass(frozen=True)
class OpResult:
    ok: bool
    value: object = None
    message: str = ""


@dataclass(frozen=True)
class DashboardView:
    mode: str
    banner: str
    adcs: list | None = None
    alarms: list | None = None
    inventory: dict | None = None
    health: dict | None = None
    rejected_recent: list | None = None
    user: str = ""
    role: str = ""
    permissions: frozenset = field(default_factory=frozenset)


@dataclass(frozen=True)
class AdcDetailView:
    adc_id: str
    link_status: str
    data_age_s: float | None
    marking: str
    inventory: list
    health: object
    alarms: list


class OperatorService:
    def __init__(self, auth, policy, reports, modes, alarms, inventory, health, links, registry, store, clock, simulate=False):
        self._auth, self._policy, self._reports, self._modes = auth, policy, reports, modes
        self._alarms, self._inventory, self._health, self._links = alarms, inventory, health, links
        self._registry, self._store, self._clock, self._simulate = registry, store, clock, simulate

    def login(self, username, password) -> OpResult:
        try:
            s = self._auth.sign_in(username, password)
        except (AuthUnavailable, InvalidCredentials, RoleNotAuthorized) as e:
            return OpResult(False, None, str(e))
        return OpResult(True, s.token, "")

    def logout(self, token) -> OpResult:
        self._auth.sign_out(token)
        return OpResult(True)

    def dashboard(self, token) -> OpResult:
        s, denied = self._authorize(token, None)
        if denied:
            return denied
        now, perms = self._clock.now(), self._policy.permissions(s.role)
        mode = self._modes.current_mode()
        mark = (lambda a: "NOT TRUSTED") if mode is Mode.FAULT else (lambda a: "CURRENT" if self._links.is_data_current(a, now) else "NOT CURRENT")
        ids = self._registry.registered()
        adcs = alarms = inventory = health = rejected = None
        if Operation.VIEW_ADC_DATA in perms:
            adcs = [self._summary(a, now, mark(a)) for a in ids]
        if Operation.VIEW_ALARMS in perms:
            lost = {a for a in ids if self._links.link_status(a) is not LinkStatus.CONNECTED}
            alarms = [(al, "may not be current" if (al.adc_id in lost or mode in (Mode.MAINTENANCE, Mode.FAULT)) else "")
                      for al in self._alarms.active_alarms()]
        if Operation.MONITOR_INVENTORY in perms:
            inventory = {a: (self._inventory.snapshot(a, now), mark(a)) for a in ids}
        if Operation.VIEW_ADC_HEALTH in perms:
            health = {a: (self._shown_health(a, now, mode), mark(a)) for a in ids}
        if Operation.VIEW_ADC_HEALTH in perms or Operation.VIEW_HISTORICAL_DATA in perms:
            try:
                rejected = self._store.query(now.date(), now.date(), types={EventType.INVALID_MESSAGE})[-10:]
            except HistoryUnavailable:
                rejected = []
        return OpResult(True, DashboardView(mode.value, _BANNER[mode], adcs, alarms, inventory, health, rejected,
                                            s.user, s.role.value, perms))

    def adc_detail(self, token, adc_id) -> OpResult:
        s, denied = self._authorize(token, Operation.VIEW_ADC_DATA)
        if denied:
            return denied
        if not self._registry.is_registered(adc_id):
            return OpResult(False, None, "ADC not registered")
        now = self._clock.now()
        summ = self._summary(adc_id, now, "NOT TRUSTED" if self._modes.current_mode() is Mode.FAULT
                             else ("CURRENT" if self._links.is_data_current(adc_id, now) else "NOT CURRENT"))
        return OpResult(True, AdcDetailView(adc_id, summ["link"], summ["age_s"], summ["marking"],
                                            self._inventory.snapshot(adc_id, now), self._health.health(adc_id, now),
                                            [a for a in self._alarms.active_alarms() if a.adc_id == adc_id]))

    def history(self, token, start, end, adc_id=None) -> OpResult:
        s, denied = self._authorize(token, Operation.VIEW_HISTORICAL_DATA)
        if denied:
            return denied
        try:
            return OpResult(True, self._reports.history(start, end, adc_id or None))
        except (InvalidRange, HistoryUnavailable) as e:
            return OpResult(False, None, "date range invalid" if isinstance(e, InvalidRange) else "historical information unavailable")

    def report(self, token, start, end, adc_id=None) -> OpResult:
        s, denied = self._authorize(token, Operation.GENERATE_REPORT)
        if denied:
            return denied
        try:
            f = self._reports.export(start, end, adc_id or None)
        except (InvalidRange, HistoryUnavailable) as e:
            return OpResult(False, None, "date range invalid" if isinstance(e, InvalidRange) else "historical information unavailable")
        self._store.append(EventType.REPORT_EXPORTED, "HMS", self._clock.now(), s.user, {"file": f.filename})
        return OpResult(True, f)

    def set_threshold(self, token, med_id, value) -> OpResult:
        s, denied = self._authorize(token, Operation.CONFIGURE_THRESHOLDS)
        if denied:
            return denied
        try:
            self._inventory.set_threshold(med_id, value)
        except ValueError as e:
            return OpResult(False, None, str(e))
        self._store.append(EventType.THRESHOLD_CHANGED, "HMS", self._clock.now(), s.user, {"medId": med_id, "threshold": value})
        return OpResult(True)

    def mode_command(self, token, event: ModeEvent) -> OpResult:
        s, denied = self._authorize(token, Operation.CONTROL_MODE)
        if denied:
            return denied
        allowed = _USER_MODE_EVENTS | ({ModeEvent.CRITICAL_FAULT} if self._simulate else set())
        if event not in allowed or not self._modes.handle(event, self._clock.now(), s.user):
            return OpResult(False, None, "not available in the current mode")
        return OpResult(True)

    def register_adc(self, token, adc_id, location) -> OpResult:
        s, denied = self._authorize(token, Operation.REGISTER_ADC)
        if denied:
            return denied
        try:
            self._registry.register(adc_id, location)
        except (InvalidAdcId, AlreadyRegistered) as e:
            return OpResult(False, None, f"{type(e).__name__}: {e}")
        self._store.append(EventType.ADC_REGISTERED, adc_id, self._clock.now(), s.user, {"location": location})
        return OpResult(True)

    def manage_user(self, token, username, role, enabled, password=None) -> OpResult:
        s, denied = self._authorize(token, Operation.MANAGE_USER_ACCESS)
        if denied:
            return denied
        try:
            r = Role(role) if role else None
        except ValueError:
            return OpResult(False, None, "unknown role")
        self._auth.set_account(username, r, enabled, password)
        self._store.append(EventType.USER_CHANGED, "HMS", self._clock.now(), s.user, {"username": username, "role": role, "enabled": enabled})
        return OpResult(True)

    def _authorize(self, token, op):
        now = self._clock.now()
        s = self._auth.session_of(token)
        if s is None:  # ACC-A1
            self._store.append(EventType.ACCESS_DENIED, "HMS", now, None, {"op": op.value if op else "DASHBOARD"})
            return None, OpResult(False, None, "access denied")
        if op is not None and not self._policy.permitted(s.role, op):  # ACC-A2
            self._store.append(EventType.ACCESS_DENIED, "HMS", now, s.user, {"op": op.value})
            return None, OpResult(False, None, "access denied")
        return s, None  # ACC-A3

    def _summary(self, a, now, marking):
        age = self._links.data_age(a, now)
        return {"adc_id": a, "link": self._links.link_status(a).value, "age_s": None if age is None else round(age.total_seconds()),
                "marking": marking, "attention": a in self._inventory.adcs_requiring_attention(now)}

    def _shown_health(self, a, now, mode):
        h = self._health.health(a, now)
        if mode is Mode.FAULT and h.status in (HealthStatus.OK, HealthStatus.WARNING):
            return type(h)(h.adc_id, HealthStatus.NOT_CURRENT, h.details, h.reported_at)
        return h
