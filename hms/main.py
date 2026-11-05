"""Composition root: constructs every module, injects dependencies, runs the event loop and the UI."""
import argparse
import threading
import time
from datetime import timedelta

from hms.behaviour import timing_policy as tp
from hms.behaviour.access_policy import AccessPolicy
from hms.behaviour.adc_registry import AdcRegistry
from hms.behaviour.alarm_handling import AlarmHandler
from hms.behaviour.health_monitor import HealthMonitor
from hms.behaviour.inventory_monitor import InventoryMonitor
from hms.behaviour.link_monitor import LinkMonitor
from hms.behaviour.message_processing import MessageProcessor
from hms.behaviour.mode_controller import Mode, ModeController, ModeEvent
from hms.behaviour.operator_service import OperatorService
from hms.hw.adc_link import LinkStartError, TcpAdcLink
from hms.hw.adc_protocol import AdcProtocol
from hms.hw.adc_simulator import AdcSimulator
from hms.hw.clock import SystemClock
from hms.hw.operator_ui import OperatorUI
from hms.sw.authentication import Authentication
from hms.sw.event_store import EventStore
from hms.sw.report_generator import ReportGenerator


class HmsApp:
    def __init__(self, simulate: bool = False, adc_port: int = 5050, clock=None):
        self._clock = clock or SystemClock()
        self._store = EventStore(self._clock)
        self._registry = AdcRegistry()
        self._links = LinkMonitor(self._registry, self._store, self._clock)
        self._alarms = AlarmHandler(self._registry, self._store)
        self._inventory = InventoryMonitor(self._links, self._store)
        self._health = HealthMonitor(self._links, self._store)
        self._modes = ModeController(self._store, self._clock)
        self._sim = AdcSimulator(self._clock) if simulate else None
        self._link = self._sim or TcpAdcLink(self._clock, adc_port)
        self._processor = MessageProcessor(self._link, AdcProtocol(), self._registry, self._links, self._alarms,
                                           self._inventory, self._health, self._store, self._clock)
        auth = Authentication(self._store, self._clock)
        self._service = OperatorService(auth, AccessPolicy(), ReportGenerator(self._store, self._clock), self._modes,
                                        self._alarms, self._inventory, self._health, self._links, self._registry,
                                        self._store, self._clock, simulate)
        self._ui = OperatorUI(self._service, self._sim)

    def run(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.start()
        threading.Thread(target=self._event_loop, daemon=True).start()
        self._ui.serve(host, port)

    def start(self) -> None:
        now = self._clock.now()
        self._last_purge = now
        self._store.purge(now)
        self._alarms.restore(self._store.query((now - tp.RETENTION).date(), now.date()))
        self._self_check()

    def step(self) -> None:
        """One event-loop iteration: drain the ADC link, then run the 1 s tick."""
        self._processor.poll()
        now = self._clock.now()
        self._processor.tick(now)
        self._store.flush()
        self._modes.tick(now)
        if self._modes.current_mode() is Mode.INITIALIZING:
            self._self_check()
        if now - self._last_purge > timedelta(days=1):
            self._store.purge(now)
            self._last_purge = now

    def flask_app(self):
        return self._ui.wsgi_app()

    def _self_check(self):
        now = self._clock.now()
        ok = now is not None and bool(self._registry.registered())
        if ok:
            try:
                self._link.start()
            except LinkStartError:
                ok = False
        self._modes.handle(ModeEvent.SELF_CHECK_OK if ok else ModeEvent.SELF_CHECK_FAIL, now)

    def _event_loop(self):
        next_tick = 0.0
        while True:
            if time.monotonic() >= next_tick:
                next_tick = time.monotonic() + tp.TICK_PERIOD
                self.step()
            else:
                self._processor.poll()
            time.sleep(tp.POLL_PERIOD)


def main():
    ap = argparse.ArgumentParser(description="Hospital Monitoring System")
    ap.add_argument("--simulate", action="store_true", help="use the ADC Simulator instead of TCP port 5050")
    ap.add_argument("--port", type=int, default=8080, help="web UI port")
    ap.add_argument("--adc-port", type=int, default=5050, help="TCP port for real ADCs")
    args = ap.parse_args()
    HmsApp(args.simulate, args.adc_port).run(port=args.port)


if __name__ == "__main__":
    main()
