"""Schedule maths: validation, the drift-free tick loop (on a fake clock), jitter bounds, skipped ticks, duration/total stops."""
import asyncio
import random

import pytest

from cpsim.model import SpecError
from cpsim.schedule import (
    Schedule,
    Ticker,
    company_schedule,
    jitter_offset,
    max_rate,
    parse_company_schedule,
    parse_schedule,
    percentile,
)


class FakeTime:
    """A clock the loop's sleep advances. `lag` makes every wake-up late, like a busy event loop."""

    def __init__(self, lag: float = 0.0):
        self.now, self.lag = 1000.0, lag

    def clock(self) -> float:
        return self.now

    async def sleep(self, d: float) -> None:
        self.now += d + self.lag
        await asyncio.sleep(0)


def run_ticker(schedule: Schedule, ft: FakeTime, *, seed=1, stop_after: int | None = None):
    fired, missed = [], []

    def on_tick(n: int) -> None:
        fired.append((n, ft.now))
        if stop_after is not None and len(fired) >= stop_after:
            t.stop()

    t = Ticker(schedule, on_tick, clock=ft.clock, sleep=ft.sleep, rng=random.Random(seed), on_missed=missed.append)
    asyncio.run(t.run())
    return t, fired, missed


# ---- validation ---------------------------------------------------------------------------------------------------
def test_defaults_and_limits():
    s = parse_schedule({})
    assert (s.interval, s.batch, s.jitter_pct, s.duration, s.total, s.ticks, s.start_on_boot) == (5.0, 1, 0.0, None, None, None, False)
    assert parse_schedule({"interval": 0.2, "batch": 500}).rate() == 2500.0
    assert parse_schedule({"interval": 2, "batch": 10, "duration": 60, "total": 100, "ticks": 5, "start_on_boot": True}).to_dict()["total"] == 100
    for bad in ({"interval": 0.19}, {"interval": 3601}, {"interval": float("nan")}, {"interval": True}, {"interval": "2"},
                {"batch": 0}, {"batch": 501}, {"batch": 1.5}, {"jitter_pct": 51}, {"jitter_pct": -1}, {"concurrency": 0}, {"concurrency": 65},
                {"duration": 0}, {"total": 0}, {"ticks": 0}, {"nope": 1}):
        with pytest.raises(SpecError):
            parse_schedule(bad)
    with pytest.raises(SpecError):
        parse_schedule([])


def test_a_kind_can_be_stricter_than_the_hard_limits():
    with pytest.raises(SpecError):
        parse_schedule({"interval": 0.5}, min_interval=1.0)
    with pytest.raises(SpecError):
        parse_schedule({"batch": 21}, max_batch=20)
    assert parse_schedule({"interval": 1.0, "batch": 20}, min_interval=1.0, max_batch=20).batch == 20
    with pytest.raises(SpecError):
        parse_schedule({"interval": 0.1}, min_interval=0.01)      # the hard floor of 0.2 s still applies


def test_the_global_ceiling_reads_the_environment(monkeypatch):
    monkeypatch.delenv("CPSIM_MAX_RATE", raising=False)
    assert max_rate() == 200.0
    monkeypatch.setenv("CPSIM_MAX_RATE", "50")
    assert max_rate() == 50.0
    for junk in ("0", "-5", "abc", "inf", ""):
        monkeypatch.setenv("CPSIM_MAX_RATE", junk)
        assert max_rate() == 200.0


def test_company_schedule():
    assert parse_company_schedule(None) is None
    d = parse_company_schedule({"interval": 30, "runs": 3, "start_on_boot": True})
    assert d == {"interval": 30.0, "jitter_pct": 0.0, "duration": None, "runs": 3, "start_on_boot": True}
    s = company_schedule(d)
    assert (s.interval, s.batch, s.ticks, s.start_on_boot) == (30.0, 1, 3, True)
    for bad in ({"interval": 4}, {"interval": 86401}, {"runs": 0}, {"batch": 2}, []):
        with pytest.raises(SpecError):
            parse_company_schedule(bad)


def test_percentile():
    assert percentile([], 0.5) is None
    assert percentile([5], 0.95) == 5
    data = list(range(1, 101))
    assert percentile(data, 0.5) == 50 and percentile(data, 0.95) == 95 and percentile(reversed(data), 1.0) == 100


# ---- the loop -----------------------------------------------------------------------------------------------------
def test_ticks_fall_on_the_grid_and_never_drift_even_when_every_wakeup_is_late():
    ft = FakeTime(lag=0.013)              # each sleep overshoots by 13 ms
    t, fired, missed = run_ticker(Schedule(interval=2.0, ticks=50), ft)
    assert [n for n, _ in fired] == list(range(50)) and missed == []
    for n, at in fired:
        assert 0 <= at - (1000.0 + n * 2.0) <= 0.02, (n, at)      # the lateness never accumulates
    assert fired[-1][1] < 1000.0 + 49 * 2.0 + 0.02


def test_jitter_stays_within_its_bounds_around_the_fixed_grid():
    rng = random.Random(3)
    offsets = [jitter_offset(rng, 5.0, 20) for _ in range(2000)]
    assert max(offsets) <= 1.0 and min(offsets) >= -1.0 and max(offsets) > 0.9 and min(offsets) < -0.9
    assert jitter_offset(rng, 5.0, 0) == 0
    ft = FakeTime()
    _, fired, _ = run_ticker(Schedule(interval=5.0, jitter_pct=20, ticks=200), ft)
    offs = [at - (1000.0 + n * 5.0) for n, at in fired]
    assert all(-1.0 - 1e-9 <= o <= 1.0 + 1e-9 for o in offs[1:]) and offs[0] >= 0           # the first tick never fires before the start
    assert len({round(o, 3) for o in offs}) > 100                                        # it really varies
    assert [n for n, _ in fired] == list(range(200)), "ticks never reorder or repeat"


def test_max_jitter_never_reorders_ticks():
    _, fired, _ = run_ticker(Schedule(interval=1.0, jitter_pct=50, ticks=300), FakeTime(), seed=9)
    times = [at for _, at in fired]
    assert times == sorted(times)


def test_missed_slots_are_reported_not_replayed_in_a_burst():
    ft = FakeTime()
    fired, missed = [], []

    def on_tick(n: int) -> None:
        fired.append(n)
        if n == 1:
            ft.now += 7.5              # the loop was starved for 7.5 s inside this tick

    t = Ticker(Schedule(interval=2.0, ticks=10), on_tick, clock=ft.clock, sleep=ft.sleep, on_missed=missed.append)
    asyncio.run(t.run())
    assert fired == [0, 1, 5, 6, 7, 8, 9]            # slots 2, 3 and 4 passed while busy: skipped, never replayed
    assert missed == [3]


def test_duration_stops_before_a_tick_past_the_end():
    _, fired, _ = run_ticker(Schedule(interval=2.0, duration=10.0), FakeTime())
    assert [n for n, _ in fired] == [0, 1, 2, 3, 4]          # slots at 0, 2, 4, 6, 8 s; the one at 10 s is past the end
    _, fired, _ = run_ticker(Schedule(interval=0.5, duration=1.0), FakeTime())
    assert [n for n, _ in fired] == [0, 1]


def test_tick_limit_and_stop():
    _, fired, _ = run_ticker(Schedule(interval=1.0, ticks=3), FakeTime())
    assert len(fired) == 3
    t, fired, _ = run_ticker(Schedule(interval=1.0), FakeTime(), stop_after=4)
    assert len(fired) == 4 and t.stopped and t.next_fire is None


def test_the_real_loop_ticks_on_time_and_stops_promptly():
    async def go():
        times = []
        t = Ticker(Schedule(interval=0.2), lambda n: times.append(asyncio.get_running_loop().time()))
        task = asyncio.create_task(t.run())
        await asyncio.sleep(0.75)
        t.stop()
        await asyncio.wait_for(task, 0.5)
        return times
    times = asyncio.run(go())
    assert 3 <= len(times) <= 5
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert all(0.15 < g < 0.3 for g in gaps)
