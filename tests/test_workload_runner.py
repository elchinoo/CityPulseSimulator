"""The workload loop: counters, skipped ticks, duration/total stops, backoff on 429/503, fatal refusals, stop and run-once (fake clock)."""
import asyncio
import random

import pytest

from cpsim.kinds.base import BaseSender, Result
from cpsim.schedule import Schedule
from cpsim.wl_model import Workload
from cpsim.wl_runner import BACKOFF_MAX, WorkloadRunner


class FakeTime:
    def __init__(self):
        self.now = 500.0

    def clock(self):
        return self.now

    async def sleep(self, d):
        self.now += d
        for _ in range(5):               # let started batches make progress
            await asyncio.sleep(0)


class Scripted(BaseSender):
    """A sender whose batches answer from a script: a list of callables(n) -> list[Result], the last one repeats."""

    def __init__(self, script=None):
        super().__init__({}, None, random.Random(1))
        self.dry = False
        self.script, self.batches, self.closed = script or [lambda n: [Result("ok", 0.01)] * n], [], False

    async def run_batch(self, n, concurrency):
        self.batches.append(n)
        step = self.script[min(len(self.batches) - 1, len(self.script) - 1)]
        out = step(n)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    async def close(self):
        self.closed = True


def runner(schedule: Schedule, sender: Scripted, ft: FakeTime | None = None, **wlkw) -> WorkloadRunner:
    ft = ft or FakeTime()
    wl = Workload("w", "paybroker", "https://t.example", "K_TOKEN", "", {}, schedule)
    return WorkloadRunner(wl, lambda: sender, clock=ft.clock, sleep=ft.sleep, rng=random.Random(1), **wlkw)


async def finish(r: WorkloadRunner):
    r.start()
    await asyncio.wait_for(r.wait(), 5)
    return r.snapshot()


def test_a_run_counts_ticks_items_outcomes_and_latency():
    sender = Scripted([lambda n: [Result("ok", 0.01)] * (n - 1) + [Result("declined", 0.03)]])
    snap = asyncio.run(finish(runner(Schedule(interval=2, batch=4, ticks=5), sender)))
    assert snap["state"] == "done" and snap["ticks"] == 5 and snap["skipped"] == 0 and snap["items"] == 20
    assert snap["outcomes"] == {"ok": 15, "declined": 5} and snap["errors_total"] == 0 and snap["last_error"] == ""
    assert snap["latency_p50"] == 0.01 and snap["latency_p95"] == 0.03 and sender.closed
    assert sender.batches == [4] * 5 and snap["next_tick_in"] is None


def test_duration_ends_the_run_and_waits_for_the_last_batch():
    snap = asyncio.run(finish(runner(Schedule(interval=2, batch=1, duration=9), Scripted())))
    assert snap["state"] == "done" and snap["ticks"] == 5          # ticks at 0, 2, 4, 6, 8 s


def test_total_trims_the_last_batch_and_stops():
    sender = Scripted()
    snap = asyncio.run(finish(runner(Schedule(interval=1, batch=5, total=12), sender)))
    assert snap["state"] == "done" and snap["items"] == 12 and snap["ticks"] == 3 and sender.batches == [5, 5, 2]


def test_a_tick_that_finds_the_previous_batch_running_is_skipped_never_queued():
    gate = asyncio.Event()

    async def slow(n):
        await gate.wait()
        return [Result("ok", 1.0)] * n
    sender = Scripted([slow, lambda n: [Result("ok", 0.01)] * n])

    async def go():
        ft = FakeTime()
        r = runner(Schedule(interval=1, batch=2, ticks=8), sender, ft)
        r.start()
        for _ in range(60):                       # let the loop run through its ticks while batch 1 is stuck
            await asyncio.sleep(0)
            if r._ticker.slots >= 8 or r.skipped >= 6:
                break
        mid = r.snapshot()
        gate.set()
        await asyncio.wait_for(r.wait(), 5)
        return mid, r.snapshot()
    mid, end = asyncio.run(go())
    assert mid["ticks"] == 1 and mid["skipped"] >= 5 and sender.batches[0] == 2
    assert end["ticks"] + end["skipped"] == 8 and end["state"] == "done"
    assert len(sender.batches) == end["ticks"], "skipped ticks never ran"


def test_throttling_backs_off_then_recovers():
    throttled = lambda n: [Result("rate_limited", 0.01, throttled=True, retry_after=0)] * n  # noqa: E731
    ok = lambda n: [Result("ok", 0.01)] * n  # noqa: E731
    sender = Scripted([throttled, throttled, ok])
    ft = FakeTime()
    r = runner(Schedule(interval=1, batch=1, ticks=40), sender, ft)
    snap = asyncio.run(finish(r))
    assert snap["outcomes"]["rate_limited"] == 2 and snap["skipped_backoff"] > 0
    assert snap["skipped"] >= snap["skipped_backoff"] and snap["items"] == len(sender.batches) and snap["state"] == "done"
    # after the first throttle the wait is 1 s base, after the second 2 s, never more than the cap; success resets it
    assert r.throttle_streak == 0 and snap["outcomes"]["ok"] > 5


def test_backoff_honours_retry_after_and_is_capped():
    async def go():
        ft = FakeTime()
        sender = Scripted([lambda n: [Result("rate_limited", 0.0, throttled=True, retry_after=45)] * n])
        r = runner(Schedule(interval=1, batch=1, ticks=1), sender, ft)
        await finish(r)
        return r.resume_at - ft.now
    assert asyncio.run(go()) == pytest.approx(45, abs=1.5)
    r = runner(Schedule(interval=1, batch=1), Scripted())
    for _ in range(20):
        r._account([Result("unavailable", 0.0, error="HTTP 503", throttled=True)])
    assert r.resume_at - r.clock() <= BACKOFF_MAX


def test_a_refused_credential_stops_the_workload_and_says_so():
    fatal = lambda n: [Result("auth_error", 0.01, error="HTTP 401: the target refused the credential", fatal=True)] * n  # noqa: E731
    sender = Scripted([fatal])
    snap = asyncio.run(finish(runner(Schedule(interval=1, batch=2), sender)))
    assert snap["state"] == "failed" and "refused" in snap["fatal"] and snap["ticks"] == 1 and sender.closed
    assert snap["last_error"] and snap["errors_total"] == 2


def test_only_error_classes_set_the_last_error():
    sender = Scripted([lambda n: [Result("declined", 0.0), Result("fraud_rejected", 0.0), Result("budget_exceeded", 0.0, throttled=True)]])
    snap = asyncio.run(finish(runner(Schedule(interval=1, batch=3, ticks=1), sender)))
    assert snap["last_error"] == "" and snap["errors_total"] == 0 and snap["recent_errors"] == []
    sender = Scripted([lambda n: [Result("server_error", 0.0, error="HTTP 500"), Result("network_error", None, error="no answer (ConnectError)")]])
    snap = asyncio.run(finish(runner(Schedule(interval=1, batch=2, ticks=1), sender)))
    assert snap["errors_total"] == 2 and snap["last_error"] == "no answer (ConnectError)" and len(snap["recent_errors"]) == 2


def test_a_crashing_generator_becomes_an_error_outcome_and_the_loop_goes_on():
    def boom(n):
        raise ValueError("secret-looking detail")
    snap = asyncio.run(finish(runner(Schedule(interval=1, batch=1, ticks=3), Scripted([boom, lambda n: [Result("ok", 0.0)]]))))
    assert snap["outcomes"] == {"generator_error": 1, "ok": 2} and "secret-looking" not in snap["last_error"]


def test_stop_cancels_a_running_batch_and_closes_the_sender():
    async def forever(n):
        await asyncio.Event().wait()
    sender = Scripted([forever])

    async def go():
        r = runner(Schedule(interval=1, batch=1), sender, FakeTime())
        r.start()
        for _ in range(20):
            await asyncio.sleep(0)
        await asyncio.wait_for(r.stop(), 2)
        return r
    r = asyncio.run(go())
    assert r.state == "stopped" and sender.closed and r.snapshot()["next_tick_in"] is None


def test_start_twice_is_refused_and_a_restart_resets_the_counters():
    async def go():
        r = runner(Schedule(interval=1, batch=1, ticks=2), Scripted())
        await finish(r)
        first = r.snapshot()["items"]
        r.start()
        with pytest.raises(RuntimeError):
            r.start()
        await asyncio.wait_for(r.wait(), 5)
        return first, r.snapshot()["items"]
    assert asyncio.run(go()) == (2, 2)


def test_run_once_sends_one_batch_and_counts_it():
    sender = Scripted()

    async def go():
        r = runner(Schedule(interval=60, batch=7), sender)
        return await r.run_once()
    snap = asyncio.run(go())
    assert snap["items"] == 7 and snap["ticks"] == 1 and snap["state"] == "idle" and sender.closed and snap["outcomes"] == {"ok": 7}


def test_run_once_is_refused_while_running_and_surfaces_a_refusal():
    async def go():
        r = runner(Schedule(interval=1, batch=1), Scripted([lambda n: [Result("auth_error", 0, error="HTTP 401", fatal=True)]]))
        snap = await r.run_once()
        r2 = runner(Schedule(interval=1, batch=1), Scripted())
        r2.start()
        with pytest.raises(RuntimeError):
            await r2.run_once()
        await r2.stop()
        return snap
    snap = asyncio.run(go())
    assert snap["fatal"] and snap["state"] != "running"
