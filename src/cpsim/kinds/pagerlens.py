"""Telemetry for PagerLens (`/api/ingest/{services,deploys,logs,metrics,alerts}`): a small fictional shop that reports healthy
baseline signals every tick, plus a failure you can switch on so an incident opens.

The system (names get `prefix`, default `sim-`): web -> checkout, catalog; checkout -> payments, db; catalog -> db; payments -> db.
Scenarios (a live switch, also settable per run with `params.scenario`), all aimed at `target_service`:
  healthy      baseline only
  bad_deploy   a deploy is announced, then errors and latency jump on the target (its dependents see a few errors too)
  error_spike  errors without a deploy
  latency      the p95 latency multiplies, errors stay normal
  silence      the target stops reporting altogether (PagerLens must notice the absence)
Items are HTTP calls: per tick one metrics call and one logs call (`batch` log events) for each reporting service.
Everything is fictional: log lines carry no addresses or personal data.
"""
import re
from dataclasses import dataclass

from ..model import SpecError
from .base import BaseSender, Result, classify_status, network_result, p_bool, p_int, unknown_params

SCENARIOS = ("healthy", "bad_deploy", "error_spike", "latency", "silence")
# service: (dependencies, baseline p95 latency ms, baseline requests per second)
SYSTEM = {"web": (["checkout", "catalog"], 180, 70), "checkout": (["payments", "db"], 240, 35), "catalog": (["db"], 120, 55),
          "payments": (["db"], 300, 30), "db": ([], 25, 140)}
PARAMS = {"prefix", "scenario", "target_service", "alert", "alert_after", "seed"}
PREFIX = re.compile(r"^(?:[a-z0-9][a-z0-9._-]{0,19})?$")
OK_LOGS = ["request handled", "cache hit for product page", "order saved", "health check passed", "session refreshed",
           "queue drained", "connection returned to the pool"]
ERROR_LOGS = {"bad_deploy": ["ERROR order submission failed: unexpected null in discount handler",
                             "ERROR unhandled exception in checkout handler after release"],
              "error_spike": ["ERROR upstream call timed out after 3000 ms", "ERROR request failed: connection reset by peer"],
              "latency": ["WARN slow response: handler took 4800 ms", "WARN thread pool queue is growing"]}
ALERTS = {"bad_deploy": ("HighErrorRate", "sev2", "5xx responses above 5% right after a deploy"),
          "error_spike": ("HighErrorRate", "sev2", "5xx responses above 5%"),
          "latency": ("HighLatency", "sev3", "p95 latency above 1000 ms")}


@dataclass
class Call:
    path: str
    body: dict


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    prefix = params.get("prefix", "sim-")
    if not isinstance(prefix, str) or not PREFIX.match(prefix):
        raise SpecError("params.prefix must be lowercase letters, digits, '.', '_' or '-' (at most 20 characters), or empty")
    scenario = params.get("scenario", "healthy")
    if scenario not in SCENARIOS:
        raise SpecError(f"params.scenario must be one of {', '.join(SCENARIOS)}")
    target = params.get("target_service", "checkout")
    if target not in SYSTEM:
        raise SpecError(f"params.target_service must be one of {', '.join(SYSTEM)}")
    return {"prefix": prefix, "scenario": scenario, "target_service": target, "alert": p_bool(params, "alert", True),
            "alert_after": p_int(params, "alert_after", 3, 0, 1000), "seed": p_int(params, "seed", None, 0, 2**32 - 1)}


def service_name(prefix: str, short: str) -> str:
    return f"{prefix}{short}"


def registration_calls(prefix: str) -> list[Call]:
    """One `services` call per service, with its dependencies (without them PagerLens cannot tell a victim from a cause)."""
    return [Call("/api/ingest/services", {"name": service_name(prefix, s), "owner": "simulated-shop", "tier": "standard",
                                          "description": f"Simulated shop service ({s})",
                                          "depends_on": [service_name(prefix, d) for d in deps]})
            for s, (deps, _, _) in SYSTEM.items()]


def metric_points(rng, short: str, scenario: str, target: str) -> list[dict]:
    """The metric points of one service for one tick. Pure given the RNG."""
    deps, lat, rps = SYSTEM[short]
    err = rng.uniform(0.1, 0.9)
    latency = lat * rng.uniform(0.85, 1.2)
    cpu, mem = rng.uniform(25, 45), rng.uniform(50, 65)
    if scenario in ("bad_deploy", "error_spike"):
        if short == target:
            err, latency, cpu = rng.uniform(18, 40), latency * rng.uniform(2.0, 3.0), cpu + 20
        elif target in deps:
            err += rng.uniform(2.5, 6)          # a dependent sees a few errors
    elif scenario == "latency" and short == target:
        latency *= rng.uniform(5, 8)
        cpu += 10
    pts = [{"name": "error_rate", "value": round(err, 2)}, {"name": "latency_p95_ms", "value": round(latency, 1)},
           {"name": "request_rate", "value": round(rps * rng.uniform(0.85, 1.15), 1)},
           {"name": "cpu_pct", "value": round(min(cpu, 99), 1)}, {"name": "mem_pct", "value": round(mem, 1)}]
    if short == "db":
        pts += [{"name": "db_connections", "value": float(rng.randint(18, 40))}, {"name": "db_connections_max", "value": 100.0}]
    return pts


def log_events(rng, short: str, scenario: str, target: str, count: int) -> list[dict]:
    out = []
    for _ in range(count):
        if short == target and scenario in ERROR_LOGS and rng.random() < 0.7:
            msg = rng.choice(ERROR_LOGS[scenario])
            level = "error" if msg.startswith("ERROR") else "warn"
        else:
            msg, level = rng.choice(OK_LOGS), "info"
        out.append({"level": level, "message": msg, "fields": {"route": rng.choice(["/cart", "/checkout", "/products", "/health"])}})
    return out


class PagerlensSender(BaseSender):
    def __init__(self, params: dict, http, rng):
        super().__init__(params, http, rng)
        self.scenario = params["scenario"]
        self.scenario_ticks = 0
        self.alerted = False
        self.registered = False
        self.deploys = 0
        self.alerts = 0
        self.version = 0
        self._deploy_due = self.scenario == "bad_deploy"

    def set_scenario(self, scenario: str) -> None:
        """Switch live: the next tick already reports the new situation."""
        if scenario not in SCENARIOS:
            raise SpecError(f"scenario must be one of {', '.join(SCENARIOS)}")
        self.scenario, self.scenario_ticks, self.alerted = scenario, 0, False
        self._deploy_due = scenario == "bad_deploy"

    def extra(self) -> dict:
        return {"scenario": self.scenario, "scenario_ticks": self.scenario_ticks, "deploys": self.deploys, "alerts": self.alerts}

    def plan(self, n: int) -> list[Call]:
        p, prefix, target = self.params, self.params["prefix"], self.params["target_service"]
        calls: list[Call] = []
        if not self.registered:
            calls += registration_calls(prefix)
            calls.append(Call("/api/ingest/deploys", {"service": service_name(prefix, "web"), "version": "v1.0.0", "author": "sim-ci",
                                                      "summary": "Simulated baseline release"}))
            self.registered = True
        if self._deploy_due:
            self.version += 1
            calls.append(Call("/api/ingest/deploys", {"service": service_name(prefix, target), "version": f"v1.{self.version}.0",
                                                      "author": "sim-ci", "summary": "Simulated release (the failure scenario)"}))
            self._deploy_due = False
        for short in SYSTEM:
            if self.scenario == "silence" and short == target:
                continue
            name = service_name(prefix, short)
            calls.append(Call("/api/ingest/metrics", {"service": name, "points": metric_points(self.rng, short, self.scenario, target)}))
            calls.append(Call("/api/ingest/logs", {"service": name, "events": log_events(self.rng, short, self.scenario, target, n)}))
        if (self.scenario in ALERTS and p["alert"] and not self.alerted and self.scenario_ticks >= p["alert_after"]):
            title, sev, summary = ALERTS[self.scenario]
            calls.append(Call("/api/ingest/alerts", {"service": service_name(prefix, target), "name": title, "severity": sev,
                                                     "summary": summary, "source": "cpsim"}))
            self.alerted = True
        self.scenario_ticks += 1
        return calls

    async def execute(self, op: Call) -> Result:
        r, latency, err = await self.http.call("POST", op.path, json=op.body)
        if r is None:
            return network_result(latency, err)
        if r.status_code in (401, 403):
            res = Result("auth_error", error=f"HTTP {r.status_code}: PagerLens refused the token (needs the ingest scope)", fatal=True)
        else:
            res = classify_status(r.status_code, r.headers)
        res.latency = latency
        if res.outcome == "ok":
            self.deploys += op.path.endswith("/deploys")
            self.alerts += op.path.endswith("/alerts")
        return res
