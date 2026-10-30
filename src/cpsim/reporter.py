"""Optional: report the simulator's health to PagerLens (PAGERLENS_URL + PAGERLENS_TOKEN, an ingest-scope token, or the identity provider's `pagerlens.ingest`).

Service `citypulse-simulator`, which depends on `citypulse-ingest`: it pushes `up`, `request_rate` (readings delivered per
second) and `error_rate` (share of delivery attempts that failed), and raises `SimulatorCannotReachCityPulse` when delivery
has been failing for a while.
"""
import asyncio
import sys
import time

import httpx

from . import __version__, outbound
from .config import get_settings
from .model import SpecError, check_target
from .tokens import PAGERLENS_INGEST, BearerAuth, outbound_token
from .wl_model import allow_http_targets

SERVICE, DEPENDS = "citypulse-simulator", "citypulse-ingest"
INTERVAL, FAIL_AFTER = 15, 45


class Reporter:
    def __init__(self, url: str, token, runtime):
        self.client = httpx.AsyncClient(base_url=url, timeout=15, follow_redirects=False, auth=BearerAuth(token))
        self.runtime, self.registered = runtime, False
        self.prev: dict[str, tuple[int, int]] = {}
        self.failing_since: float | None = None

    async def post(self, path: str, body: dict) -> None:
        r = await self.client.post(path, json=body)
        if r.status_code >= 300:
            raise RuntimeError(f"PagerLens answered HTTP {r.status_code} on {path}")

    async def report(self) -> None:
        if not self.registered:
            await self.post("/api/ingest/services", {"name": SERVICE, "tier": "standard", "owner": "city-platform",
                                                     "description": "CityPulse Simulator: field devices and edge gateway",
                                                     "depends_on": [DEPENDS]})
            await self.post("/api/ingest/deploys", {"service": SERVICE, "version": __version__, "summary": "CityPulse Simulator started"})
            self.registered = True
        sent = fails = 0
        problem = ""
        for s in self.runtime.snapshots():
            ps, pf = self.prev.get(s["name"], (s["sent"], s["send_failures"]))
            sent, fails = sent + s["sent"] - ps, fails + s["send_failures"] - pf
            self.prev[s["name"]] = (s["sent"], s["send_failures"])
            problem = problem or (s["problem"] if s["backoff"] else "")
        attempts = fails + sent / 100
        error = 100 * fails / attempts if attempts else 0.0
        await self.post("/api/ingest/metrics", {"service": SERVICE, "points": [
            {"name": "up", "value": 1.0}, {"name": "request_rate", "value": round(sent / INTERVAL, 3)},
            {"name": "error_rate", "value": round(error, 2)}]})
        if error >= 50 and fails:
            self.failing_since = self.failing_since or time.monotonic()
            if time.monotonic() - self.failing_since >= FAIL_AFTER:
                await self.post("/api/ingest/alerts", {"service": SERVICE, "name": "SimulatorCannotReachCityPulse", "severity": "sev3",
                                                       "source": "cpsim", "summary": f"The simulator cannot deliver readings: {problem or 'delivery keeps failing'}"[:300]})
                self.failing_since = time.monotonic()
        else:
            self.failing_since = None

    async def run(self) -> None:
        while True:
            try:
                await self.report()
            except Exception as exc:  # noqa: BLE001 - PagerLens being down must never hurt the simulator
                print(f"reporter: {str(exc).splitlines()[0][:160]}", file=sys.stderr, flush=True)
            await asyncio.sleep(INTERVAL)


def maybe_start(runtime) -> Reporter | None:
    cfg = get_settings()
    try:       # the rules of a workload's target: https, or http for this machine (or anywhere with CPSIM_ALLOW_HTTP_TARGETS), then the NetGuard
        if cfg.pagerlens_url:
            check_target(cfg.pagerlens_url, allow_http=allow_http_targets())
            outbound.check_url(cfg.pagerlens_url)
        token = cfg.pagerlens_token or outbound_token("PAGERLENS_TOKEN", PAGERLENS_INGEST, cfg.pagerlens_url)
    except SpecError as exc:
        print(f"reporter: refusing PAGERLENS_URL, {exc}", file=sys.stderr)
        return None
    if not (cfg.pagerlens_url and token):
        return None
    return Reporter(cfg.pagerlens_url, token, runtime)
