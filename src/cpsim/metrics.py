"""Prometheus metrics, using the shared names every app in the family exposes (http_requests_total,
http_request_duration_seconds, db_up, app_info, process_*) plus the simulator's own."""
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.process_collector import ProcessCollector

from . import __version__

APP = "cpsim"
REGISTRY = CollectorRegistry()
ProcessCollector(registry=REGISTRY)
REQUESTS = Counter("http_requests_total", "HTTP requests handled", ["app", "method", "route", "status"], registry=REGISTRY)
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["app", "route"], registry=REGISTRY,
                    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
INFO = Gauge("app_info", "Build information", ["app", "version"], registry=REGISTRY)
INFO.labels(APP, __version__).set(1)
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


REGISTRY.register(SimCollector())


def bind(runtime, store) -> None:
    _state["runtime"], _state["store"] = runtime, store


def observe(method: str, route: str, status: int, started: float) -> None:
    REQUESTS.labels(APP, method, route, str(status)).inc()
    LATENCY.labels(APP, route).observe(time.perf_counter() - started)


def render() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
