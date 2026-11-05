"""1.1.4 Operator UI: present information to staff and accept their actions.

Secret: Flask routes, HTML templates, layout, wording.
Two pages: /dashboard (HMS operator dashboard) and /sim (simulated ADC panel).
"""
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from flask import Flask, Response, redirect, render_template, request

from hms.behaviour import timing_policy as tp
from hms.behaviour.mode_controller import ModeEvent
from hms.hw.adc_simulator import ALARM_TYPES, SimFault

_COOKIE = "hms_token"


@dataclass(frozen=True)
class UserAction:
    kind: str
    token: str | None
    params: dict = field(default_factory=dict)


class OperatorUI:
    def __init__(self, service, simulator=None):
        self._service = service
        self._sim = simulator
        self._app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))
        self._routes()

    def show(self, view) -> str:
        try:
            return render_template(view["template"], refresh=tp.UI_REFRESH, **view)
        except Exception as e:  # stored data is unchanged; show a notice instead of the page
            return f"<p>Display error: {e}</p>"

    def get_user_action(self, request) -> UserAction:
        p = dict(request.values)
        kind = request.path.strip("/").replace("/", "_").upper() or "DASHBOARD"
        return UserAction(kind, request.cookies.get(_COOKIE), p)

    def serve(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self._app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)

    def wsgi_app(self):
        return self._app

    def _routes(self):
        app = self._app

        @app.get("/")
        def _root():
            return redirect("/dashboard")

        @app.get("/dashboard")
        def _dashboard():
            a = self.get_user_action(request)
            view = {"template": "dashboard.html", "message": a.params.get("msg", ""), "dash": None, "history": None,
                    "today": datetime.now(timezone.utc).date().isoformat(), "params": a.params}
            if a.token:
                r = self._service.dashboard(a.token)
                if r.ok:
                    view["dash"] = r.value
                    if "from" in a.params:
                        h = self._service.history(a.token, self._date(a.params.get("from")), self._date(a.params.get("to")), a.params.get("adc"))
                        view["history"] = h.value if h.ok else None
                        view["message"] = view["message"] or (h.message if not h.ok else "")
                else:
                    view["message"] = r.message
            return self.show(view)

        @app.post("/login")
        def _login():
            a = self.get_user_action(request)
            r = self._service.login(a.params.get("username", ""), a.params.get("password", ""))
            resp = redirect("/dashboard" if r.ok else f"/dashboard?msg={r.message}")
            if r.ok:
                resp.set_cookie(_COOKIE, r.value, httponly=True, samesite="Strict")
            return resp

        @app.post("/logout")
        def _logout():
            a = self.get_user_action(request)
            self._service.logout(a.token)
            resp = redirect("/dashboard")
            resp.delete_cookie(_COOKIE)
            return resp

        @app.post("/mode")
        def _mode():
            a = self.get_user_action(request)
            try:
                ev = ModeEvent(a.params.get("event"))
            except ValueError:
                return redirect("/dashboard?msg=unknown mode event")
            r = self._service.mode_command(a.token, ev)
            return redirect("/dashboard" + ("" if r.ok else f"?msg={r.message}"))

        @app.get("/report.csv")
        def _report():
            a = self.get_user_action(request)
            r = self._service.report(a.token, self._date(a.params.get("from")), self._date(a.params.get("to")), a.params.get("adc"))
            if not r.ok:
                return redirect(f"/dashboard?msg={r.message}")
            return Response(r.value.content, mimetype=r.value.media_type,
                            headers={"Content-Disposition": f"attachment; filename={r.value.filename}"})

        @app.get("/sim")
        def _sim():
            a = self.get_user_action(request)
            return self.show({"template": "sim.html", "enabled": self._sim is not None, "message": a.params.get("msg", ""),
                              "adcs": ["ADC-01", "ADC-02", "ADC-03"], "faults": [f.value for f in SimFault],
                              "alarm_types": ALARM_TYPES,
                              "receipts": {a: self._sim.recent_receipts(a) for a in ["ADC-01", "ADC-02", "ADC-03"]} if self._sim else {},
                              "active": {a: self._sim.active_alarm_types(a) for a in ["ADC-01", "ADC-02", "ADC-03"]} if self._sim else {}})

        @app.post("/sim/inject")
        def _inject():
            a = self.get_user_action(request)
            if self._sim is None:
                return redirect("/sim?msg=simulation disabled")
            try:
                fault = SimFault(a.params.get("fault"))
            except ValueError:
                return redirect("/sim?msg=unknown fault")
            ok = self._sim.inject(a.params.get("adc", ""), fault, a.params.get("alarm_type", "DOOR_FORCED"))
            return redirect(f"/sim?msg={fault.value} on {a.params.get('adc')}: {'sent' if ok else 'rejected'}")

    def _date(self, s):
        try:
            return date.fromisoformat(s)
        except (TypeError, ValueError):
            return date.max
