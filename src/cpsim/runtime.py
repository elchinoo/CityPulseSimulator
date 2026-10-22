"""Owns the running cities. Everything runs in the app's event loop."""
from .engine import CityRunner
from .hr_engine import HrRun, TrustDeskClient
from .hr_model import Company, parse_company
from .model import City, SpecError, parse_city
from .sinks import HttpSink, NullSink
from .tokens import CITYPULSE_INGEST, TRUSTDESK_CLIENT, outbound_token


class Runtime:
    def __init__(self, store):
        self.store = store
        self.runners: dict[str, CityRunner] = {}
        self.hr_runs: dict[str, HrRun] = {}     # the latest run of each company (kept after it ends, for its results)

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

    def _sink(self, city: City):
        if not city.target_url:
            return NullSink()
        token = outbound_token(city.token_env, CITYPULSE_INGEST)
        if not token:
            raise SpecError(f"the environment variable {city.token_env} is empty: set it to an ingest token for {city.target_url} "
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
        runner = CityRunner(city, self._sink(city))
        await runner.start()
        self.runners[name] = runner
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

    async def autostart(self) -> None:
        for name, city in self.specs().items():
            if city.autostart:
                try:
                    await self.start(name)
                except SpecError as exc:
                    print(f"cpsim: cannot autostart {name}: {exc}", flush=True)

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

    def start_hr(self, name: str) -> HrRun:
        company = self.company(name)
        if company is None:
            raise KeyError(name)
        current = self.hr_runs.get(name)
        if current and current.running:
            raise RuntimeError("this company is already running")
        if company.total_requests() == 0 and (company.new_employees + company.tenured_employees) == 0:
            raise SpecError("add employees or requests first")
        make = None
        if company.target_url:
            token = outbound_token(company.token_env, TRUSTDESK_CLIENT)
            if not token:
                raise SpecError(f"the environment variable {company.token_env} is empty: set it to a TrustDesk "
                                f"client token for {company.target_url} "
                                "(or configure the service client of the identity provider: see CPSIM_SERVICE_CLIENT_SECRET)")

            def make():  # noqa: E306
                return TrustDeskClient(company.target_url, token)
        run = HrRun(company, make)
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

