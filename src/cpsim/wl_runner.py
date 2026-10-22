"""Runs one workload: the drift-free tick loop, the non-overlapping batches, the counters and the backoff.

Each tick starts one batch as a task. A tick that finds the previous batch still running is counted as skipped (never queued), and
so is a tick that falls inside a backoff window after the target answered 429 or 503. Counters live in memory; the definition
of the workload lives in the database.
"""
import asyncio
import random
import time
from collections import Counter, deque

from .kinds.base import ERROR_OUTCOMES, Result
from .schedule import Ticker, percentile
from .wl_model import Workload

BACKOFF_MAX = 60.0
LATENCY_WINDOW, ERROR_WINDOW = 2000, 10


class WorkloadRunner:
    def __init__(self, workload: Workload, make_sender, *, clock=time.monotonic, sleep=None, wall=time.time,
                 rng: random.Random | None = None, name: str | None = None):
        """`make_sender()` builds the kind's sender (called at start, so each run begins fresh and a failure surfaces at start)."""
        self.workload, self.make_sender = workload, make_sender
        self.name = name or workload.name
        self.clock, self.wall, self._sleep, self.rng = clock, wall, sleep, rng or random.Random()
        self.sender = None
        self.state = "idle"
        self.fatal = ""
        self._ticker: Ticker | None = None
        self._task: asyncio.Task | None = None
        self._batch: asyncio.Task | None = None
        self._stopping = False
        self._reset()

    def _reset(self) -> None:
        self.ticks = self.skipped = self.skipped_backoff = self.missed = 0
        self.items = 0
        self.outcomes: Counter = Counter()
        self.latencies: deque = deque(maxlen=LATENCY_WINDOW)
        self.errors: deque = deque(maxlen=ERROR_WINDOW)
        self.last_error = ""
        self.last_error_at = 0.0
        self.throttle_streak = 0
        self.resume_at = 0.0
        self.started = self.finished = 0.0
        self._t_start = 0.0

    # ---- control ------------------------------------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.state == "running"

    def start(self) -> None:
        if self.running:
            raise RuntimeError("this workload is already running")
        self._reset()
        self.fatal = ""
        self.sender = self.make_sender()
        self._stopping = False
        s = self.workload.schedule
        self.state, self.started, self._t_start = "running", self.wall(), self.clock()
        self._ticker = Ticker(s, self._on_tick, clock=self.clock, sleep=self._sleep, rng=self.rng,
                              on_missed=self._on_missed)
        self._task = asyncio.create_task(self._main())

    async def stop(self) -> None:
        if not self.running:
            return
        self._stopping = True
        self._ticker.stop()
        if self._batch and not self._batch.done():
            self._batch.cancel()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def wait(self) -> None:
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    async def run_once(self) -> dict:
        """One batch right now, outside any schedule, counted like a tick. Not while the workload is running."""
        if self.running:
            raise RuntimeError("stop the workload first, or let its schedule run")
        self._reset()
        self.fatal = ""
        self.sender = self.make_sender()
        self.started = self.wall()
        try:
            self.ticks = 1
            await self._run_batch(self.workload.schedule.batch)
        finally:
            await self.sender.close()
            self.finished = self.wall()
        self.state = "failed" if self.fatal else "idle"
        return self.snapshot()

    # ---- the loop -----------------------------------------------------------------------------------------------
    def _on_missed(self, k: int) -> None:
        self.missed += k
        self.skipped += k

    def _on_tick(self, n: int) -> None:
        if self.clock() < self.resume_at:
            self.skipped += 1
            self.skipped_backoff += 1
            return
        if self._batch and not self._batch.done():
            self.skipped += 1
            return
        s = self.workload.schedule
        count = s.batch if s.total is None else min(s.batch, s.total - self.items)
        if count <= 0:
            self._ticker.stop()
            return
        self.ticks += 1
        self._batch = asyncio.create_task(self._run_batch(count))

    async def _main(self) -> None:
        try:
            await self._ticker.run()
            if self._batch:
                await asyncio.gather(self._batch, return_exceptions=True)    # let the last batch finish
        except asyncio.CancelledError:
            pass
        finally:
            await self.sender.close()
            self.finished = self.wall()
            self.state = "failed" if self.fatal else "stopped" if self._stopping else "done"

    async def _run_batch(self, count: int) -> None:
        s = self.workload.schedule
        try:
            results = await self.sender.run_batch(count, s.concurrency)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bug in a generator must not kill the loop silently
            results = [Result("generator_error", error=f"unexpected {type(exc).__name__}")]
        self._account(results)
        if self.fatal or (s.total is not None and self.items >= s.total):
            if self._ticker:
                self._ticker.stop()

    def _account(self, results: list[Result]) -> None:
        throttled, retry_after = False, 0.0
        for r in results:
            self.items += 1
            self.outcomes[r.outcome] += 1
            if r.latency is not None:
                self.latencies.append(r.latency)
            if r.is_error:
                self.last_error, self.last_error_at = r.error or r.outcome, self.wall()
                self.errors.append({"at": round(self.last_error_at, 1), "outcome": r.outcome, "message": r.error})
            if r.fatal and not self.fatal:
                self.fatal = r.error or r.outcome
            if r.throttled:
                throttled, retry_after = True, max(retry_after, r.retry_after)
        if throttled:
            self.throttle_streak += 1
            base = max(1.0, self.workload.schedule.interval)
            delay = max(retry_after, min(base * 2 ** (self.throttle_streak - 1), BACKOFF_MAX))
            self.resume_at = self.clock() + delay
        else:
            self.throttle_streak = 0

    # ---- what the UI and metrics read -----------------------------------------------------------------------------
    def snapshot(self) -> dict:
        s = self.workload.schedule
        elapsed = (self.finished or self.wall()) - self.started if self.started else 0.0
        now = self.clock()
        nxt = self._ticker.next_fire if (self._ticker and self.running) else None
        errors = sum(n for k, n in self.outcomes.items() if k in ERROR_OUTCOMES)
        return {"name": self.name, "kind": self.workload.kind, "state": self.state, "fatal": self.fatal,
                "dry_run": not self.workload.target_url and self.workload.kind != "trustdesk",
                "interval": s.interval, "batch": s.batch, "jitter_pct": s.jitter_pct,
                "ticks": self.ticks, "skipped": self.skipped, "skipped_backoff": self.skipped_backoff, "missed": self.missed,
                "items": self.items, "outcomes": dict(self.outcomes), "errors_total": errors,
                "latency_p50": percentile(self.latencies, 0.5), "latency_p95": percentile(self.latencies, 0.95),
                "last_error": self.last_error, "recent_errors": list(self.errors),
                "next_tick_in": None if nxt is None else round(max(0.0, nxt - now), 2),
                "backing_off": self.running and now < self.resume_at,
                "elapsed": round(elapsed, 1), "rate": round(self.items / elapsed, 2) if elapsed > 0 else 0.0,
                "extra": self.sender.extra() if self.sender else {}}

