"""Owns the running cities. Everything runs in the app's event loop."""
import asyncio
import math
import os
from dataclasses import replace

from . import fleet, kinds
from .engine import CityRunner
from .hr_engine import HrRun, TrustDeskClient
from .hr_model import Company, parse_company
from .model import City, SpecError, parse_city
from .schedule import company_schedule, max_rate
from .sinks import HttpSink, NullSink
from .tokens import CITYPULSE_INGEST, TRUSTDESK_CLIENT, outbound_token
from .wl_model import Workload, allow_http_targets, parse_workload
from .wl_runner import WorkloadRunner

RESUME_SETTING = "resume_on_start"        # the runtime switch (Settings page); CPSIM_RESUME_ON_START is its default until someone sets it
DEFAULT_STAGGER = 1.0


def resume_default() -> bool:
    return os.environ.get("CPSIM_RESUME_ON_START", "").strip().lower() not in ("0", "false", "no", "off")


def resume_stagger() -> float:
    """Seconds between two resumed items (CPSIM_RESUME_STAGGER_SECONDS), so a big fleet does not all start in the same instant."""
    try:
        v = float(os.environ.get("CPSIM_RESUME_STAGGER_SECONDS", ""))
    except ValueError:
        return DEFAULT_STAGGER
    return min(v, 60.0) if math.isfinite(v) and v >= 0 else DEFAULT_STAGGER


class Runtime:
    def __init__(self, store):
        self.store = store
        self.runners: dict[str, CityRunner] = {}
        self.hr_runs: dict[str, HrRun] = {}     # the latest run of each company (kept after it ends, for its results)
        self.wl_runners: dict[str, WorkloadRunner] = {}         # the latest runner of each workload (kept after it ends, for its counters)
        self.company_schedules: dict[str, WorkloadRunner] = {}  # companies that repeat their run on a schedule

    # ---- the desired run state: started and not stopped by a person, so it runs again after a restart --------------------
    def want(self, kind: str, name: str, on: bool) -> None:
        """Remember (or forget) that this city/workload/company/schedule should be running. Never raises: the run itself matters more."""
        try:
            self.store.set_desired(kind, name, on)
        except Exception as exc:  # noqa: BLE001 - e.g. the database blinked; the run goes on, it just may not resume
            print(f"cpsim: cannot record the run state of {kind} {name}: {str(exc).splitlines()[0][:120]}", flush=True)

    def wanted(self, kind: str) -> set[str]:
        try:
            return self.store.desired(kind)
        except Exception:  # noqa: BLE001
            return set()

    def resume_enabled(self) -> bool:
        try:
            v = self.store.setting(RESUME_SETTING)
        except Exception:  # noqa: BLE001
            v = None
        return resume_default() if v is None else v == "true"

    def _ends(self, kind: str, name: str, runner) -> None:
        """A run that finishes by itself (a total or duration was reached) is done, not interrupted: it must not start over after a restart."""
        task = getattr(runner, "_task", None)
        if task is not None:
            task.add_done_callback(lambda _t: self.want(kind, name, False) if runner.state == "done" else None)

    def specs(self) -> dict[str, City]:
        out = {}
        for name, raw in self.store.cities().items():
            try:
                out[name] = parse_city(raw)
            except SpecError:
                continue            # a hand-edited spec that no longer validates is skipped, not trusted
        return out

    def get(self, name: str) -> City | None:
        return self.specs().get(name)

    @staticmethod
    def resolved(spec, allow_http: bool = False):
        """The spec with its fleet reference (`${CITYPULSE_URL}`) replaced by the address the environment holds NOW, checked with the
        usual URL rules; raises SpecError when it does not resolve. Used at start time only: what is stored keeps the reference."""
        return replace(spec, target_url=fleet.resolve(spec.target_url, allow_http))

    def _sink(self, city: City):
        if not city.target_url:
            return NullSink()
        token = outbound_token(city.token_env, CITYPULSE_INGEST)
        if not token:
            raise SpecError(f"no credential for {city.token_env}: save an ingest token for {city.target_url} under Settings > Credentials, or set that environment variable "
                            "(or configure the service client of the identity provider: see CPSIM_SERVICE_CLIENT_SECRET)")
        return HttpSink(city.target_url, token)

    async def start(self, name: str) -> CityRunner:
        city = self.get(name)
        if city is None:
            raise KeyError(name)
        if name in self.runners:
            return self.runners[name]
        if city.sensor_count() == 0:
            raise SpecError("add at least one sensor group first")
        city = self.resolved(city)
        runner = CityRunner(city, self._sink(city))
        await runner.start()
        self.runners[name] = runner
        self.want("city", name, True)
        return runner

    async def stop(self, name: str) -> bool:
        runner = self.runners.pop(name, None)
        if runner:
            await runner.stop()
        return runner is not None

    async def stop_all(self) -> None:
        for name in list(self.runners):
            await self.stop(name)
        for name in list(self.hr_runs):
            await self.stop_hr(name)
        for name in list(self.wl_runners):
            await self.stop_workload(name)
        for name in list(self.company_schedules):
            await self.stop_company_schedule(name)

    async def autostart(self) -> None:
        for name, city in self.specs().items():
            if city.autostart:
                try:
                    await self.start(name)
                except SpecError as exc:
                    print(f"cpsim: cannot autostart {name}: {exc}", flush=True)
        for name, wl in self.workloads().items():
            if wl.schedule.start_on_boot:
                try:
                    self.start_workload(name)
                except (SpecError, RuntimeError) as exc:
                    print(f"cpsim: cannot autostart workload {name}: {exc}", flush=True)
        for name, company in self.companies().items():
            if company.schedule and company.schedule["start_on_boot"]:
                try:
                    self.start_company_schedule(name)
                except (SpecError, RuntimeError) as exc:
                    print(f"cpsim: cannot autostart the schedule of {name}: {exc}", flush=True)

    async def resume(self, stagger: float | None = None) -> None:
        """After a restart, start again whatever was running when the app went down (the desired-run flags), one item every `stagger`
        seconds. A failure (target unreachable, credential locked) is one log line and leaves the flag set: the next restart tries again."""
        if not self.resume_enabled():
            print("cpsim: resume on start is off: nothing is resumed", flush=True)
            return
        stagger = resume_stagger() if stagger is None else stagger
        plan = [("city", self.specs()), ("workload", self.workloads()), ("schedule", self.companies()), ("company", self.companies())]
        first = True
        for kind, known in plan:
            for name in sorted(self.wanted(kind)):
                if name not in known or name not in self.wanted(kind):       # deleted, or stopped by a person since the app came up
                    continue
                if self._is_running(kind, name):
                    continue                                                 # already started (start on boot): no second start
                if not first:
                    await asyncio.sleep(stagger)
                first = False
                try:
                    await self._resume_one(kind, name)
                    print(f"cpsim: resumed {kind} {name}", flush=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - never crash the app over one item
                    print(f"cpsim: cannot resume {kind} {name}: {str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__}", flush=True)

    def _is_running(self, kind: str, name: str) -> bool:
        if kind == "city":
            return name in self.runners
        table = {"workload": self.wl_runners, "schedule": self.company_schedules, "company": self.hr_runs}[kind]
        return bool(table.get(name) and table[name].running)

    async def _resume_one(self, kind: str, name: str) -> None:
        if kind == "city":
            await self.start(name)
        elif kind == "workload":
            self.start_workload(name)
        elif kind == "schedule":
            self.start_company_schedule(name)
        else:
            self.start_hr(name)

    # ---- companies (simulated employers sending requests to TrustDesk) -----------------------------------------
    def companies(self) -> dict[str, Company]:
        out = {}
        for name, raw in self.store.companies().items():
            try:
                out[name] = parse_company(raw)
            except SpecError:
                continue
        return out

    def company(self, name: str) -> Company | None:
        return self.companies().get(name)

    def _hr_client_factory(self, company: Company):
        """A function that builds the TrustDesk client of a company (None: generate only). Raises SpecError when the token is missing."""
        company = self.resolved(company)
        if not company.target_url:
            return None
        token = outbound_token(company.token_env, TRUSTDESK_CLIENT)
        if not token:
            raise SpecError(f"no credential for {company.token_env}: save a TrustDesk "
                            f"client token for {company.target_url} under Settings > Credentials, or set that environment variable "
                            "(or configure the service client of the identity provider: see CPSIM_SERVICE_CLIENT_SECRET)")

        def make():
            return TrustDeskClient(company.target_url, token)
        return make

    def start_hr(self, name: str) -> HrRun:
        company = self.company(name)
        if company is None:
            raise KeyError(name)
        run = self._launch_hr(company)        # by a person (or the resume): the schedule's own runs are not remembered, its schedule is
        self.want("company", name, True)
        self._ends("company", name, run)
        return run

    def _launch_hr(self, company: Company) -> HrRun:
        name = company.name
        current = self.hr_runs.get(name)
        if current and current.running:
            raise RuntimeError("this company is already running")
        if company.total_requests() == 0 and (company.new_employees + company.tenured_employees) == 0:
            raise SpecError("add employees or requests first")
        company = self.resolved(company)
        run = HrRun(company, self._hr_client_factory(company))
        self.hr_runs[name] = run
        run.start()
        return run

    async def stop_hr(self, name: str) -> bool:
        run = self.hr_runs.get(name)
        if run and run.running:
            await run.stop()
            return True
        return False

    def hr_snapshots(self) -> list[dict]:
        return [r.snapshot() for r in self.hr_runs.values()]

    def snapshots(self) -> list[dict]:
        return [r.snapshot() for r in self.runners.values()]


    # ---- workloads: scheduled pushes to the other apps ---------------------------------------------------------------
    def workloads(self) -> dict[str, Workload]:
        out = {}
        for name, raw in self.store.workloads().items():
            try:
                out[name] = parse_workload(raw)
            except SpecError:
                continue            # a hand-edited spec that no longer validates is skipped, not trusted
        return out

    def workload(self, name: str) -> Workload | None:
        return self.workloads().get(name)

    def _check_rate(self, wl: Workload, exclude: str = "") -> None:
        """The global ceiling: running workloads together may not ask for more than CPSIM_MAX_RATE items per second."""
        if wl.kind == "trustdesk":
            return
        total = wl.schedule.rate() + sum(r.workload.schedule.rate() for n, r in self.wl_runners.items()
                                         if r.running and n != exclude and r.workload.kind != "trustdesk")
        if total > max_rate():
            raise SpecError(f"running workloads would ask for {total:g} items/s in all, above the ceiling CPSIM_MAX_RATE={max_rate():g}")

    def _sender_factory(self, wl: Workload):
        token = kinds.credential(wl)
        company = start_run = None
        if wl.kind == "trustdesk":
            company = self.company(wl.params["company"])
            if company is None:
                raise SpecError(f"there is no company named {wl.params['company']!r}: create it under Companies first")
            company = self.resolved(company)
            self._hr_client_factory(company)         # fail now, not at the first tick, when its token is missing
            start_run = self._launch_hr
        return lambda: kinds.build_sender(wl, token=token, company=company, start_run=start_run)

    def _runner(self, wl: Workload, name: str | None = None) -> WorkloadRunner:
        wl = self.resolved(wl, allow_http_targets())
        return WorkloadRunner(wl, self._sender_factory(wl), name=name)

    def start_workload(self, name: str) -> WorkloadRunner:
        wl = self.workload(name)
        if wl is None:
            raise KeyError(name)
        current = self.wl_runners.get(name)
        if current and current.running:
            raise RuntimeError("this workload is already running")
        self._check_rate(wl, exclude=name)
        runner = self._runner(wl)
        runner.start()
        self.wl_runners[name] = runner
        self.want("workload", name, True)
        self._ends("workload", name, runner)
        return runner

    async def stop_workload(self, name: str) -> bool:
        runner = self.wl_runners.get(name)
        if runner and runner.running:
            await runner.stop()
            return True
        return False

    async def run_workload_once(self, name: str) -> dict:
        wl = self.workload(name)
        if wl is None:
            raise KeyError(name)
        current = self.wl_runners.get(name)
        if current and current.running:
            raise RuntimeError("this workload is running: stop it first")
        self._check_rate(wl, exclude=name)
        runner = self._runner(wl)
        self.wl_runners[name] = runner
        return await runner.run_once()

    def start_company_schedule(self, name: str) -> WorkloadRunner:
        company = self.company(name)
        if company is None:
            raise KeyError(name)
        if not company.schedule:
            raise SpecError("this company has no schedule: set one first")
        current = self.company_schedules.get(name)
        if current and current.running:
            raise RuntimeError("this company's schedule is already running")
        company = self.resolved(company)
        wl = Workload(name, "trustdesk", company.target_url, "", "", {"company": name}, company_schedule(company.schedule))
        runner = self._runner(wl, name=f"company/{name}")
        runner.start()
        self.company_schedules[name] = runner
        self.want("schedule", name, True)
        self._ends("schedule", name, runner)
        return runner

    async def stop_company_schedule(self, name: str) -> bool:
        runner = self.company_schedules.get(name)
        if runner and runner.running:
            await runner.stop()
            return True
        return False

    def workload_snapshots(self) -> list[dict]:
        return [r.snapshot() for r in (*self.wl_runners.values(), *self.company_schedules.values())]
