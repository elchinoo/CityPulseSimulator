"""Prometheus metrics, using the shared names every app in the family exposes (http_requests_total,
http_request_duration_seconds, db_up, app_info, process_*) plus the simulator's own."""
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from .core import metrics as _kit

APP = "cpsim"
REGISTRY = _kit.registry          # the kit's registry already carries http_requests_total, http_request_duration_seconds, app_info, process_*
observe = _kit.observe
render = _kit.render
_state = {"runtime": None, "store": None}


class SimCollector:
    def collect(self):
        runtime, store = _state["runtime"], _state["store"]
        up = GaugeMetricFamily("db_up", "1 if the state store answers", labels=["app"])
        up.add_metric([APP], 1 if (store is None or store.healthy()) else 0)
        yield up
        snaps = runtime.snapshots() if runtime else []
        for name, doc, key in (("cpsim_sensors", "Simulated sensors", "sensors"), ("cpsim_buffer_readings", "Readings waiting for retry", "buffer"),
                               ("cpsim_devices_unreachable", "Field devices failing their polls", "devices_unreachable")):
            g = GaugeMetricFamily(name, doc, labels=["city"])
            for s in snaps:
                g.add_metric([s["name"]], s[key])
            yield g
        r = GaugeMetricFamily("cpsim_cities_running", "Cities running")
        r.add_metric([], len(snaps))
        yield r
        for name, doc, key in (("cpsim_readings_sent_total", "Readings delivered to CityPulse", "sent"),
                               ("cpsim_readings_dropped_total", "Readings dropped (buffer full)", "dropped"),
                               ("cpsim_send_failures_total", "Failed delivery attempts", "send_failures")):
            c = CounterMetricFamily(name, doc, labels=["city"])
            for s in snaps:
                c.add_metric([s["name"]], s[key])
            yield c
        polls = CounterMetricFamily("cpsim_polls_total", "Gateway polls of field devices", labels=["city", "protocol", "result"])
        for s in snaps:
            for proto, n in s["polls_ok"].items():
                polls.add_metric([s["name"], proto, "ok"], n)
            for proto, n in s["polls_failed"].items():
                polls.add_metric([s["name"], proto, "failed"], n)
        yield polls


        hr = runtime.hr_snapshots() if runtime else []
        run = GaugeMetricFamily("cpsim_hr_runs_running", "Company runs in progress")
        run.add_metric([], sum(1 for s in hr if s["state"] == "running"))
        yield run
        reqs = CounterMetricFamily("cpsim_hr_requests", "Requests sent to TrustDesk by the latest run", labels=["company", "type", "outcome"])
        emps = CounterMetricFamily("cpsim_hr_employees", "Employees handled by the latest run", labels=["company", "outcome"])
        for s in hr:
            for t, c in s["types"].items():
                for outcome in ("accepted", "invalid", "errors", "withdrawn"):
                    reqs.add_metric([s["company"], t, outcome], c[outcome])
            for outcome in ("created", "existing", "failed"):
                emps.add_metric([s["company"], outcome], s["employees"][outcome])
        yield reqs
        yield emps


REGISTRY.register(SimCollector())


def bind(runtime, store) -> None:
    _state["runtime"], _state["store"] = runtime, store
