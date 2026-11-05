# Hospital Monitoring System (Group 36, Part B)

Implements the approved Deliverable 2 design (`partbfinal.docx`). One module is one file, laid out as in the Module Guide (`hms/hw`, `hms/sw`, `hms/behaviour`).

## Run

```bash
pip install -r requirements.txt
python -m hms --simulate        # demo: simulated ADC-01..ADC-03
python -m hms                   # real ADCs connect over TCP port 5050 (JSON lines)
```

Pages:
- HMS operator dashboard: http://127.0.0.1:8080/dashboard
- Simulated ADC page (inject alarms, status, inventory, link loss, ...): http://127.0.0.1:8080/sim

Demo accounts (password = username): `nurse1` (NURSE), `pharm1` (PHARMACIST), `tech1` (TECHNICIAN), `admin1` (HOSPITAL_ADMIN), `it1` (IT_SPECIALIST).

Tests (every injection, plus combinations): `python -m pytest -q tests/test_injections.py`

Where injections show: alarms for nurse1/tech1, inventory for pharm1, cabinet health for tech1, rejected messages for tech1/pharm1/admin1 (the sim page lists this too).

To reset the demo (clear all alarms, events and simulated cabinet state), stop the server and delete the `data/` folder.

State is held in memory. Events are persisted to `data/events.jsonl` and the simulated cabinets' state to `data/sim_state.json`; ADCs, thresholds and accounts are in `config/*.json`.

Username	Password	Role
nurse1
pharm1
tech1
admin1
it1
