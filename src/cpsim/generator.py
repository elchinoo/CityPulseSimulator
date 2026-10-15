"""One simulated sensor: a daily curve plus noise, and random burst episodes.

`burst_pct` (0..100) sets how readily a sensor enters a burst episode: each reading starts one with probability
burst_pct/100 * 0.04 (times the city's temporary boost), so 100 means about one episode per 25 readings and 0 never.
Episodes: spike, noise, stuck, dropout, flood, corrupt. With a seed, the whole sequence is reproducible.
"""
import math
import random
from dataclasses import dataclass
from datetime import datetime

from .catalog import TYPES, hash_str

BASE_START = 0.04
KINDS = {"spike": 25, "noise": 22, "stuck": 20, "dropout": 13, "flood": 10, "corrupt": 10}
_KIND_NAMES, _KIND_WEIGHTS = list(KINDS), list(KINDS.values())


@dataclass
class Tick:
    value: float | None        # None: the sensor produced nothing this time
    kind: str | None           # the active burst episode, if any


class SensorSim:
    def __init__(self, sid: str, kind: str, district: str, protocol: str, lo: float, hi: float, frequency: float,
                 burst_pct: float, seed: int | None, now: float):
        self.id, self.type, self.district, self.protocol = sid, kind, district, protocol
        self.lo, self.hi, self.frequency, self.burst_pct = lo, hi, frequency, burst_pct
        self.spec = TYPES[kind]
        self.rng = random.Random(None if seed is None else seed ^ hash_str(sid))
        self.bias = self.rng.gauss(0, (hi - lo) * 0.01)
        self.due = now + self.rng.uniform(0, frequency)          # spread first readings over one period
        self.episode: str | None = None
        self.left = 0.0              # readings left (or seconds, for a dropout)
        self.frozen: float | None = None
        self.state = self.rng.random() < 0.5
        self.last = (lo + hi) / 2

    # ---- normal behaviour ---------------------------------------------------------------------
    def _normal(self, now: float) -> float:
        t, local = self.spec, datetime.fromtimestamp(now)
        if self.type == "traffic_light":
            phase = (now + hash_str(self.id) % 90) % 90
            return 0.0 if phase < 45 else 1.0 if phase < 85 else 2.0
        if self.type == "parking":
            if self.rng.random() < 0.04:
                self.state = self.rng.random() < 0.5 + 0.35 * math.cos(2 * math.pi * (local.hour - t.peak) / 24)
            return 1.0 if self.state else 0.0
        mean, span = (self.lo + self.hi) / 2, self.hi - self.lo
        hour = local.hour + local.minute / 60
        v = mean + 0.25 * span * math.cos(2 * math.pi * (hour - t.peak) / 24) + self.bias + self.rng.gauss(0, span * 0.02)
        return _reflect(v, self.lo, self.hi)

    # ---- bursts -------------------------------------------------------------------------------
    def _maybe_start(self, boost: float) -> None:
        p = min(1.0, self.burst_pct / 100 * BASE_START * boost)
        if self.episode is None and p > 0 and self.rng.random() < p:
            self.episode = self.rng.choices(_KIND_NAMES, _KIND_WEIGHTS)[0]
            self.left = self.rng.uniform(20, 120) if self.episode == "dropout" else self.rng.randint(3, 20)
            self.frozen = None

    def _spike(self) -> float:
        t, span = self.spec, self.hi - self.lo
        if self.rng.random() < 0.6:   # beyond the sensor's physical limits: a fault
            return t.hi * (1.1 + self.rng.random() * 0.6) + 1 if self.rng.random() < 0.7 else t.lo - 1 - self.rng.random() * 5
        return self.hi + span * (1 + self.rng.random() * 2)

    def interval(self) -> float:
        """Seconds until the next reading; a flood episode reports five times as often."""
        f = self.frequency / 5 if self.episode == "flood" else self.frequency
        return f * self.rng.uniform(0.9, 1.1)

    def step(self, now: float, boost: float = 1.0) -> Tick:
        self._maybe_start(boost)
        kind, v = self.episode, self._normal(now)
        if kind == "dropout":
            self.left -= self.frequency
            if self.left <= 0:
                self.episode = None
            return Tick(None, kind)
        if kind == "spike":
            v = self._spike()
        elif kind == "noise":
            v = _reflect(v + self.rng.gauss(0, (self.hi - self.lo) * 0.15), self.lo - (self.hi - self.lo), self.hi + (self.hi - self.lo))
        elif kind == "stuck":
            if self.frozen is None:
                self.frozen = v
            v = self.frozen
        elif kind == "corrupt":
            v = float("nan")
        if kind is not None and kind != "dropout":
            self.left -= 1
            if self.left <= 0:
                self.episode, self.frozen = None, None
        self.last = v
        return Tick(v, kind)


def _reflect(v: float, lo: float, hi: float) -> float:
    if v < lo:
        v = lo + (lo - v)
    if v > hi:
        v = hi - (v - hi)
    return min(max(v, lo), hi)
