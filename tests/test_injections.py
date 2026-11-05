"""End-to-end tests of every simulated-ADC injection, through the two pages (Flask test client).

Run: python -m pytest -q tests/test_injections.py
"""
import re
import shutil
from pathlib import Path

import pytest

from hms.hw.adc_link import TcpAdcLink
from hms.hw.adc_simulator import ALARM_TYPES
from hms.hw.clock import SimClock
from hms.main import HmsApp

ROOT = Path(__file__).resolve().parents[1]
CRITICAL = {"DOOR_FORCED", "TEMPERATURE_OUT_OF_RANGE", "DISPENSE_FAILURE", "POWER_FAILURE", "HARDWARE_FAULT", "UNAUTHORIZED_ACCESS"}


class Hms:
    def __init__(self, tmp_path, monkeypatch, clock=None):
        if not (tmp_path / "config").exists():
            shutil.copytree(ROOT / "config", tmp_path / "config")
        monkeypatch.chdir(tmp_path)
        self.clock = clock or SimClock()
        self.app = HmsApp(simulate=True, clock=self.clock)
        self.app.start()
        self.app.step()
        self.web = self.app.flask_app().test_client()
        self.users = {}

    def inject(self, adc, fault, alarm_type="DOOR_FORCED"):
        r = self.web.post("/sim/inject", data={"adc": adc, "fault": fault, "alarm_type": alarm_type})
        assert r.status_code == 302
        self.app.step()
        return "sent" in r.headers["Location"]

    def advance(self, seconds):
        for _ in range(int(seconds)):
            self.clock.advance(1)
            self.app.step()

    def page(self, user):
        if user not in self.users:
            c = self.app.flask_app().test_client()
            assert c.post("/login", data={"username": user, "password": user}).headers["Location"] == "/dashboard"
            self.users[user] = c
        d = self.clock.now().date().isoformat()
        return self.users[user].get(f"/dashboard?from={d}&to={d}").get_data(as_text=True)

    def sim_page(self):
        return self.web.get("/sim").get_data(as_text=True)

    def events(self):
        return Path("data/events.jsonl").read_text()


@pytest.fixture
def hms(tmp_path, monkeypatch):
    return Hms(tmp_path, monkeypatch)


def section(html, title):
    m = re.search(rf"<h2>{title}</h2>(.*?)(<h2>|</body>)", html, re.S)
    return m.group(1) if m else ""


def row(html, title, adc):
    return " ".join(r for r in re.findall(r"<tr>(.*?)</tr>", section(html, title), re.S) if f">{adc}<" in r)


# ---------- single injections ----------

@pytest.mark.parametrize("alarm_type", ALARM_TYPES)
def test_alarm_each_type_shown_with_adc_and_logged(hms, alarm_type):
    assert hms.inject("ADC-01", "ALARM", alarm_type)
    for user in ("nurse1", "tech1"):
        alarms = section(hms.page(user), "Active alarms")
        assert "ADC-01" in alarms and alarm_type in alarms
        assert ("CRITICAL" if alarm_type in CRITICAL else "WARNING") in alarms
    assert "Active alarms" not in hms.page("pharm1")
    assert '"ALARM_RECEIVED", "source": "ADC-01"' in hms.events()


def test_critical_alarm_listed_before_warning(hms):
    hms.inject("ADC-01", "ALARM", "DOOR_AJAR")
    hms.inject("ADC-02", "ALARM", "POWER_FAILURE")
    alarms = section(hms.page("nurse1"), "Active alarms")
    assert alarms.index("POWER_FAILURE") < alarms.index("DOOR_AJAR")


def test_clear_alarm_removes_it_and_logs(hms):
    hms.inject("ADC-02", "ALARM", "DOOR_FORCED")
    assert hms.inject("ADC-02", "CLEAR_ALARM", "DOOR_FORCED")
    assert "No active alarms." in hms.page("nurse1")
    assert "ALARM_CLEARED" in hms.events()


def test_clear_with_no_active_alarm_is_rejected_by_sim(hms):
    assert not hms.inject("ADC-02", "CLEAR_ALARM")


def test_low_stock_then_restock(hms):
    hms.inject("ADC-01", "LOW_STOCK")
    assert "ADC-01 requires inventory attention" in hms.page("nurse1")
    assert "ATTENTION_REQUIRED" in row(hms.page("pharm1"), "Inventory", "ADC-01")
    assert "INVENTORY_ATTENTION" in hms.events()
    hms.inject("ADC-01", "RESTOCK")
    assert "requires inventory attention" not in hms.page("nurse1")


def test_bad_quantity_invalid_until_next_valid_status(hms):
    hms.inject("ADC-01", "BAD_QUANTITY")
    inv = row(hms.page("pharm1"), "Inventory", "ADC-01")
    assert "INVALID" in inv and "<td>20</td>" in inv  # old quantity kept
    assert "INVENTORY_INVALID" in hms.events()
    hms.advance(5)
    assert "INVALID" not in row(hms.page("pharm1"), "Inventory", "ADC-01")


def test_health_fault_then_recovery(hms):
    hms.inject("ADC-02", "HEALTH_FAULT")
    h = row(hms.page("tech1"), "Cabinet health", "ADC-02")
    assert "FAULT" in h and "MOTOR_STALL" in h
    assert "HEALTH_CHANGED" in hms.events()
    hms.inject("ADC-02", "HEALTH_OK")
    assert "<td>OK</td>" in row(hms.page("tech1"), "Cabinet health", "ADC-02")


def test_link_loss_after_30s_then_restore(hms):
    hms.inject("ADC-03", "LINK_LOSS")
    hms.advance(25)
    assert "has been lost" not in hms.page("nurse1")
    hms.advance(7)
    page = hms.page("nurse1")
    assert "Communication with ADC-03 has been lost" in page
    assert "NOT CURRENT" in row(page, "Cabinets", "ADC-03")
    assert "COMM_LOST" in hms.events()
    hms.inject("ADC-03", "RESTORE")
    page = hms.page("nurse1")
    assert "has been lost" not in page and "CURRENT" in row(page, "Cabinets", "ADC-03")
    assert "COMM_RESTORED" in hms.events()


def test_duplicate_ignored(hms):
    hms.inject("ADC-01", "ALARM")
    before = hms.events()
    assert hms.inject("ADC-01", "DUPLICATE")
    assert "IGNORED" in hms.sim_page()
    assert section(hms.page("nurse1"), "Active alarms").count("<td>ADC-01</td>") == 1
    assert hms.events().count("ALARM_RECEIVED") == before.count("ALARM_RECEIVED")


def test_damaged_message_rejected_and_listed(hms):
    hms.inject("ADC-01", "DAMAGED")
    assert "REJECTED" in hms.sim_page()
    assert "unreadable" in section(hms.page("tech1"), r"Rejected messages \(today\)")


def test_unregistered_adc_rejected_never_shown_as_cabinet(hms):
    hms.inject("ADC-01", "UNREGISTERED_ID")
    page = hms.page("tech1")
    assert "from ADC-99: unregistered ADC" in page
    assert "ADC-99" not in section(page, "Cabinets") and "ADC-99" not in section(page, "Active alarms")


# ---------- combinations and sequences ----------

def test_link_loss_on_one_adc_does_not_stop_others(hms):
    hms.inject("ADC-03", "LINK_LOSS")
    hms.advance(32)
    hms.inject("ADC-01", "ALARM", "DISPENSE_FAILURE")
    hms.inject("ADC-02", "LOW_STOCK")
    page = hms.page("nurse1")
    assert "DISPENSE_FAILURE" in section(page, "Active alarms")
    assert "CONNECTED" in row(page, "Cabinets", "ADC-01") and "CONNECTED" in row(page, "Cabinets", "ADC-02")
    assert "ADC-02 requires inventory attention" in page
    assert "has been lost" in row(page, "Cabinets", "ADC-03")


def test_alarm_on_lost_adc_stays_visible_and_marked(hms):
    hms.inject("ADC-03", "ALARM", "TEMPERATURE_OUT_OF_RANGE")
    hms.inject("ADC-03", "LINK_LOSS")
    hms.advance(32)
    alarms = section(hms.page("nurse1"), "Active alarms")
    assert "TEMPERATURE_OUT_OF_RANGE" in alarms and "may not be current" in alarms


def test_alarm_during_link_loss_replayed_on_restore(hms):
    hms.inject("ADC-03", "LINK_LOSS")
    hms.inject("ADC-03", "ALARM", "UNAUTHORIZED_ACCESS")
    assert "UNAUTHORIZED_ACCESS" not in hms.page("nurse1")
    hms.inject("ADC-03", "RESTORE")
    assert "UNAUTHORIZED_ACCESS" in section(hms.page("nurse1"), "Active alarms")


def test_health_fault_then_alarm_on_same_adc(hms):
    hms.inject("ADC-02", "HEALTH_FAULT")
    hms.inject("ADC-02", "ALARM", "HARDWARE_FAULT")
    page = hms.page("tech1")
    assert "FAULT" in row(page, "Cabinet health", "ADC-02")
    assert "HARDWARE_FAULT" in section(page, "Active alarms")


def test_repeated_alarms_each_listed(hms):
    for _ in range(3):
        hms.inject("ADC-01", "ALARM", "DOOR_FORCED")
    assert section(hms.page("nurse1"), "Active alarms").count("DOOR_FORCED") == 3


def test_clear_then_reraise(hms):
    hms.inject("ADC-01", "ALARM", "POWER_FAILURE")
    hms.inject("ADC-01", "CLEAR_ALARM", "POWER_FAILURE")
    assert "POWER_FAILURE" not in section(hms.page("nurse1"), "Active alarms")
    hms.inject("ADC-01", "ALARM", "POWER_FAILURE")
    assert "POWER_FAILURE" in section(hms.page("nurse1"), "Active alarms")


def test_invalid_messages_do_not_affect_valid_traffic(hms):
    hms.inject("ADC-01", "DAMAGED")
    hms.inject("ADC-01", "UNREGISTERED_ID")
    hms.inject("ADC-01", "DUPLICATE")
    hms.inject("ADC-01", "ALARM", "DOOR_AJAR")
    assert "DOOR_AJAR" in section(hms.page("nurse1"), "Active alarms")


def test_full_sequence_on_every_adc_keeps_all_consistent(hms):
    for adc in ("ADC-01", "ADC-02", "ADC-03"):
        for f in ("ALARM", "LOW_STOCK", "HEALTH_FAULT", "BAD_QUANTITY", "DUPLICATE", "DAMAGED", "HEALTH_OK", "RESTOCK", "CLEAR_ALARM"):
            hms.inject(adc, f)
    hms.advance(10)
    page = hms.page("tech1")
    assert "No active alarms." in page
    for adc in ("ADC-01", "ADC-02", "ADC-03"):
        assert "CONNECTED" in row(page, "Cabinets", adc) and "<td>OK</td>" in row(page, "Cabinet health", adc)


# ---------- regression tests for bugs found in the live walkthrough ----------

def snapshot(hms):
    """Everything an operator can see about the cabinets, without timestamps or data ages."""
    tech, pharm = hms.page("tech1"), hms.page("pharm1")
    alarms = sorted(re.findall(r"<td>(CRITICAL|WARNING)</td><td>(ADC-\d+)</td><td>[^<]*</td><td>([A-Z_]+)</td>", tech))
    links = re.findall(r"<tr><td>(ADC-\d+)</td>\s*<td>([^<]+)</td>", section(tech, "Cabinets"))
    health = re.findall(r"<tr><td>(ADC-\d+)</td><td>([A-Z_]+)</td><td>([^<]*)</td>", section(tech, "Cabinet health"))
    inv = re.findall(r"<tr><td>(ADC-\d+)</td><td>([^<]+)</td><td>([^<]+)</td><td>[^<]*</td><td>([A-Z_]+)</td>", section(pharm, "Inventory"))
    return alarms, links, health, inv, "requires inventory attention" in tech


@pytest.mark.parametrize("on, off, alarm_type", [
    ("LOW_STOCK", "RESTOCK", "DOOR_FORCED"),
    ("HEALTH_FAULT", "HEALTH_OK", "DOOR_FORCED"),
    ("ALARM", "CLEAR_ALARM", "HARDWARE_FAULT"),
    ("LINK_LOSS", "RESTORE", "DOOR_FORCED"),
])
def test_inverse_pairs_restore_previous_state(hms, on, off, alarm_type):
    hms.inject("ADC-01", "ALARM", "DOOR_AJAR")  # unrelated existing state must survive
    before = snapshot(hms)
    hms.inject("ADC-02", on, alarm_type)
    if on == "LINK_LOSS":
        hms.advance(32)
    assert snapshot(hms) != before
    hms.inject("ADC-02", off, alarm_type)
    assert snapshot(hms) == before


def test_low_stock_raises_warning_alarm_and_restock_clears_it(hms):
    hms.inject("ADC-01", "LOW_STOCK")
    hms.inject("ADC-01", "LOW_STOCK")  # repeated: still one alarm
    alarms = section(hms.page("nurse1"), "Active alarms")
    assert alarms.count("LOW_STOCK") == 1 and "WARNING" in alarms
    hms.inject("ADC-01", "RESTOCK")
    assert "No active alarms." in hms.page("nurse1")


def test_clear_alarm_only_clears_the_selected_type(hms):
    hms.inject("ADC-02", "ALARM", "POWER_FAILURE")
    assert not hms.inject("ADC-02", "CLEAR_ALARM", "DOOR_AJAR")
    assert "POWER_FAILURE" in section(hms.page("nurse1"), "Active alarms")


def test_sim_page_two_step_dropdowns(hms):
    page = hms.sim_page()
    assert '"LOW_STOCK"' not in page.split("var ALL")[1].split(";")[0]  # LOW_STOCK alarm comes from LOW_STOCK/RESTOCK
    hms.inject("ADC-03", "ALARM", "DOOR_AJAR")
    assert '"ADC-03": ["DOOR_AJAR"]' in hms.sim_page()  # CLEAR_ALARM offers only active types


def test_sim_reply_list_keeps_injection_result_after_periodic_status(hms):
    hms.inject("ADC-01", "DUPLICATE")
    hms.advance(6)
    replies = hms.sim_page().split("Recent replies")[1].split("</table>")[0]
    assert "seq" in replies and "IGNORED" in replies


def test_alarms_clearable_after_hms_restart(tmp_path, monkeypatch):
    first = Hms(tmp_path, monkeypatch)
    first.inject("ADC-01", "ALARM", "POWER_FAILURE")
    second = Hms(tmp_path, monkeypatch, clock=first.clock)  # restored from the event log and sim state
    assert "POWER_FAILURE" in section(second.page("nurse1"), "Active alarms")
    assert second.inject("ADC-01", "CLEAR_ALARM", "POWER_FAILURE")
    assert "No active alarms." in second.page("nurse1")


def test_mode_control_for_it_specialist_and_restart_returns_to_normal(hms):
    assert "Mode control" in hms.page("it1") and "Mode control" not in hms.page("nurse1")
    c = hms.users["it1"]
    c.post("/mode", data={"event": "CRITICAL_FAULT"})
    assert "Mode: <b>FAULT" in hms.page("it1")
    c.post("/mode", data={"event": "RESTART"})
    hms.app.step()
    assert "Mode: <b>NORMAL" in hms.page("it1")


def test_history_and_report_buttons_follow_permissions(hms):
    pharm, admin, it = hms.page("pharm1"), hms.page("admin1"), hms.page("it1")
    assert "View history" in pharm and "Download CSV" not in pharm
    assert "View history" in admin and "Download CSV" in admin
    assert "View history" not in it and "Download CSV" in it
    d = hms.clock.now().date().isoformat()
    r = hms.users["admin1"].get(f"/report.csv?from={d}&to={d}&adc=")
    assert r.status_code == 200 and r.data.startswith(b"time_utc,adc_id")


def test_tcp_link_start_is_idempotent_for_restart():
    import socket
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    link = TcpAdcLink(SimClock(), port=port, host="127.0.0.1")
    link.start()
    link.start()  # RESTART re-runs the self-check; must not fail to re-bind
