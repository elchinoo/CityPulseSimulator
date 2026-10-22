"""Recurring requests for TrustDesk: the existing company engine (`HrRun`), run again on a schedule. Nothing is duplicated here:
each tick starts one run of the named company and waits for it; a tick that finds the previous run still going is skipped.

`params.company` names a company (in the app: one saved under Companies; in a headless file: one under `companies:`). The company
keeps its own URL, token variable and mix of requests. A fixed company seed is advanced by one per run, because TrustDesk
deduplicates requests by idempotency key (same seed, same keys, nothing new); without a seed every run is random. Items are the
requests of the run, by outcome: accepted (`ok`), refused as invalid (`invalid`, expected: some are built to break a policy) and
errors. Schedule: `interval` is the time between run starts (5 s or more, one run per tick); `batch` is not used.
"""
import dataclasses
import re

from ..model import SpecError
from .base import BaseSender, Result, unknown_params

PARAMS = {"company"}
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    name = params.get("company")
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError("params.company must name a company (lowercase letters, digits and dashes)")
    return {"company": name}


def next_company(company, run_index: int):
    """The company for run number `run_index`: a fixed seed moves on by one per run so idempotency keys never repeat."""
    if company.seed is None:
        return company
    return dataclasses.replace(company, seed=(company.seed + run_index) % 2**32)


class TrustdeskSender(BaseSender):
    """`start_run(company) -> HrRun` is supplied by the Runtime (it also keeps the run visible under Companies)."""

    def __init__(self, params: dict, company, start_run):
        super().__init__(params, None, None)
        self.company, self.start_run = company, start_run
        self.runs = 0
        self.run = None
        self.dry = not company.target_url

    def extra(self) -> dict:
        return {"company": self.company.name, "runs": self.runs}

    async def run_batch(self, n: int, concurrency: int) -> list[Result]:
        try:
            run = self.run = self.start_run(next_company(self.company, self.runs))
        except RuntimeError:        # the same company is running by hand: this tick finds it busy, nothing is sent
            return [Result("busy")]
        self.runs += 1
        await run.wait()
        snap = run.snapshot()
        t = snap["totals"]
        if snap["state"] == "failed":
            fatal = "refused the token" in snap["fatal"]
            return [Result("auth_error" if fatal else "network_error", error=snap["fatal"][:160], fatal=fatal)]
        ok = "generated" if snap["dry_run"] else "ok"
        return ([Result(ok)] * t["accepted"] + [Result("invalid")] * t["invalid"]
                + [Result("server_error", error="TrustDesk answered with an error")] * t["errors"])

    async def close(self) -> None:
        if self.run is not None and self.run.running:
            await self.run.stop()
