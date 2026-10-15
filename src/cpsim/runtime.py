"""Owns the running cities. Everything runs in the app's event loop."""
import os

from .engine import CityRunner
from .model import City, SpecError, parse_city
from .sinks import HttpSink, NullSink


class Runtime:
    def __init__(self, store):
        self.store = store
        self.runners: dict[str, CityRunner] = {}

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
        token = os.environ.get(city.token_env, "") or _file_token(city.token_env)
        if not token:
            raise SpecError(f"the environment variable {city.token_env} is empty: set it to an ingest token for {city.target_url}")
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

    async def autostart(self) -> None:
        for name, city in self.specs().items():
            if city.autostart:
                try:
                    await self.start(name)
                except SpecError as exc:
                    print(f"cpsim: cannot autostart {name}: {exc}", flush=True)

    def snapshots(self) -> list[dict]:
        return [r.snapshot() for r in self.runners.values()]


def _file_token(env: str) -> str:
    path = os.environ.get(f"{env}_FILE", "")
    if not path:
        return ""
    try:
        with open(path) as fh:
            return fh.readline().strip()
    except OSError:
        return ""


