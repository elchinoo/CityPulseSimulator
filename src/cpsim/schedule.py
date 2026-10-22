"""Schedules: how often a workload pushes, how much, and for how long. Pure maths plus a drift-free tick loop.

A schedule has an `interval` (seconds between ticks, 0.2 to 3600, fractions allowed), a `jitter_pct` (each tick fires up to that
share of the interval early or late, around a FIXED grid, so jitter never accumulates), a `batch` (items per tick), a
`concurrency` (parallel requests inside a batch), optional `duration` (seconds), `total` (items) and `ticks` limits (None means
forever), and `start_on_boot` (resume after a restart).

The loop (`Ticker`) never replays missed ticks in a burst and never waits for the work: the work is started as a task, so a slow
target makes later ticks "skipped" instead of piling up.
"""
import asyncio
import math
import os
import random
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass

from .model import SpecError

MIN_INTERVAL, MAX_INTERVAL = 0.2, 3600.0
MAX_BATCH, MAX_CONCURRENCY = 500, 64
DEFAULT_MAX_RATE = 200.0
FIELDS = {"interval", "jitter_pct", "batch", "concurrency", "duration", "total", "ticks", "start_on_boot"}


@dataclass
class Schedule:
    interval: float = 5.0            # seconds between ticks
    jitter_pct: float = 0.0          # 0..50: each tick fires within +-this share of the interval around the fixed grid
    batch: int = 1                   # items sent per tick
    concurrency: int = 4             # parallel requests inside one batch
    duration: float | None = None    # stop after this many seconds (None: forever)
    total: int | None = None         # stop after this many items (None: forever)
    ticks: int | None = None         # stop after this many scheduled ticks (None: forever)
    start_on_boot: bool = False      # start again when the app starts

    def rate(self) -> float:
        """Items per second this schedule asks for (before any follow-up calls a kind adds)."""
        return self.batch / self.interval

    def to_dict(self) -> dict:
        return asdict(self)


def max_rate() -> float:
    """The global ceiling on items per second for one workload and for all running workloads together (CPSIM_MAX_RATE)."""
    try:
        v = float(os.environ.get("CPSIM_MAX_RATE", ""))
    except ValueError:
        return DEFAULT_MAX_RATE
    return v if math.isfinite(v) and v > 0 else DEFAULT_MAX_RATE


def _num(value, what: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise SpecError(f"{what} must be a number between {lo:g} and {hi:g}")
    return float(value)


def _whole(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SpecError(f"{what} must be a whole number from {lo} to {hi}")
    return value


def parse_schedule(d, *, min_interval: float = MIN_INTERVAL, max_interval: float = MAX_INTERVAL,
                   max_batch: int = MAX_BATCH) -> Schedule:
    """Strict: a schedule is user input. `min_interval` / `max_batch` let a kind be stricter than the hard limits."""
    d = {} if d is None else d
    if not isinstance(d, dict):
        raise SpecError("schedule must be an object")
    unknown = set(d) - FIELDS
    if unknown:
        raise SpecError(f"unknown schedule field(s): {', '.join(sorted(unknown))}")
    lo = max(min_interval, MIN_INTERVAL)
    hi = min(max_interval, MAX_INTERVAL) if max_interval <= MAX_INTERVAL else max_interval
    interval = _num(d.get("interval", 5.0), "interval", lo, hi)
    batch = _whole(d.get("batch", 1), "batch", 1, min(max_batch, MAX_BATCH))
    duration = d.get("duration")
    total = d.get("total")
    ticks = d.get("ticks")
    return Schedule(
        interval, _num(d.get("jitter_pct", 0), "jitter_pct", 0, 50), batch,
        _whole(d.get("concurrency", 4), "concurrency", 1, MAX_CONCURRENCY),
        None if duration is None else _num(duration, "duration", 1, 30 * 86400),
        None if total is None else _whole(total, "total", 1, 100_000_000),
        None if ticks is None else _whole(ticks, "ticks", 1, 100_000_000),
        bool(d.get("start_on_boot", False)))


def percentile(values, q: float) -> float | None:
    """Nearest-rank percentile of an iterable (None when empty)."""
    data = sorted(values)
    if not data:
        return None
    return data[min(len(data) - 1, max(0, math.ceil(q * len(data)) - 1))]


def jitter_offset(rng: random.Random, interval: float, pct: float) -> float:
    """A random offset in [-interval*pct/100, +interval*pct/100]; at most half an interval, so ticks never reorder."""
    return rng.uniform(-1.0, 1.0) * interval * pct / 100.0


class Ticker:
    """Calls `on_tick(n)` for slot n at t0 + n*interval (+ jitter), on a monotonic clock. `on_tick` must be quick (start a task).

    Drift-free: slot times come from the fixed grid, never from "now + interval", so a slow wake-up does not move later ticks.
    If the loop wakes up so late that whole slots have passed, those slots are not replayed: they are reported to `on_missed`
    and the loop continues with the next future slot.
    """

    def __init__(self, schedule: Schedule, on_tick: Callable[[int], None], *, clock=time.monotonic, sleep=None,
                 rng: random.Random | None = None, on_missed: Callable[[int], None] | None = None):
        self.s, self.on_tick, self.clock = schedule, on_tick, clock
        self.on_missed = on_missed or (lambda k: None)
        self.rng = rng or random.Random()
        self._wake = asyncio.Event()
        self._sleep = sleep or self._interruptible_sleep
        self._stop = False
        self.t0: float | None = None
        self.next_fire: float | None = None
        self.slots = 0                                   # slots visited (fired or missed)

    def stop(self) -> None:
        self._stop = True
        self._wake.set()

    @property
    def stopped(self) -> bool:
        return self._stop

    async def _interruptible_sleep(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
        except TimeoutError:
            pass

    def fire_time(self, n: int) -> float:
        """When slot n fires (never before the start, never before the previous slot's nominal time)."""
        s = self.s
        nominal = self.t0 + n * s.interval
        return max(nominal + jitter_offset(self.rng, s.interval, s.jitter_pct), self.t0)

    async def run(self) -> None:
        s = self.s
        self.t0 = self.clock()
        n = 0
        while not self._stop:
            if s.ticks is not None and n >= s.ticks:
                break
            if s.duration is not None and n * s.interval >= s.duration:
                break
            fire = self.fire_time(n)
            self.next_fire = fire
            delay = fire - self.clock()
            if delay > 0:
                await self._sleep(delay)
            if self._stop:
                break
            self.on_tick(n)
            self.slots = n + 1
            n += 1
            latest = math.floor((self.clock() - self.t0) / s.interval)    # the newest slot whose nominal time has passed
            if latest >= n:
                self.on_missed(latest - n + 1)
                n = latest + 1
                self.slots = n
        self.next_fire = None


# ---- a company's schedule: "repeat the run every N seconds" ------------------------------------------------------------
COMPANY_FIELDS = {"interval", "jitter_pct", "duration", "runs", "start_on_boot"}
COMPANY_MIN_INTERVAL, COMPANY_MAX_INTERVAL = 5.0, 86400.0


def parse_company_schedule(d) -> dict | None:
    """Optional schedule of a company: one run per tick, every `interval` seconds (5 to 86400), `runs` runs at most. None: no schedule."""
    if d is None:
        return None
    if not isinstance(d, dict):
        raise SpecError("schedule must be an object")
    unknown = set(d) - COMPANY_FIELDS
    if unknown:
        raise SpecError(f"unknown schedule field(s): {', '.join(sorted(unknown))}")
    duration, runs = d.get("duration"), d.get("runs")
    return {"interval": _num(d.get("interval", 60.0), "interval", COMPANY_MIN_INTERVAL, COMPANY_MAX_INTERVAL),
            "jitter_pct": _num(d.get("jitter_pct", 0), "jitter_pct", 0, 50),
            "duration": None if duration is None else _num(duration, "duration", 1, 30 * 86400),
            "runs": None if runs is None else _whole(runs, "runs", 1, 1_000_000),
            "start_on_boot": bool(d.get("start_on_boot", False))}


def company_schedule(d: dict) -> Schedule:
    """The loop's Schedule for a company's normalized schedule dict (one run per tick)."""
    return Schedule(d["interval"], d["jitter_pct"], 1, 1, d["duration"], None, d["runs"], d["start_on_boot"])
