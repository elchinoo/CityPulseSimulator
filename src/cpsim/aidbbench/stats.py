"""Statistics for the harness (pure, no database): nearest-rank-with-interpolation percentiles and a thread-safe recorder."""
import math
import threading


def percentile(sorted_values: list[float], p: float) -> float:
    """The p-th percentile (0..100) of an ascending list, linear interpolation between closest ranks (numpy's default method)."""
    if not sorted_values:
        raise ValueError("no samples")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * p / 100.0
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def summarize(seconds: list[float]) -> dict:
    """Latency summary in milliseconds of samples in seconds: n, min, mean, p50, p95, p99, max (None when there are no samples).
    With fewer than 100 samples p99 is not a meaningful estimate; `n` is always reported so a reader can tell."""
    if not seconds:
        return {"n": 0, "min_ms": None, "mean_ms": None, "p50_ms": None, "p95_ms": None, "p99_ms": None, "max_ms": None}
    s = sorted(seconds)
    ms = lambda v: round(v * 1000.0, 3)  # noqa: E731
    return {"n": len(s), "min_ms": ms(s[0]), "mean_ms": ms(sum(s) / len(s)), "p50_ms": ms(percentile(s, 50)),
            "p95_ms": ms(percentile(s, 95)), "p99_ms": ms(percentile(s, 99)), "max_ms": ms(s[-1])}


def slope(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (y per x) of (x, y) points; None for fewer than two points or no spread in x."""
    n = len(points)
    if n < 2:
        return None
    mx, my = sum(x for x, _ in points) / n, sum(y for _, y in points) / n
    den = sum((x - mx) ** 2 for x, _ in points)
    return None if den == 0 else sum((x - mx) * (y - my) for x, y in points) / den


def describe(values: list[float]) -> dict:
    """n, min, mean, p95 and max of a plain series (queue depth samples, for example)."""
    if not values:
        return {"n": 0, "min": None, "mean": None, "p95": None, "max": None}
    s = sorted(values)
    return {"n": len(s), "min": s[0], "mean": round(sum(s) / len(s), 3), "p95": round(percentile(s, 95), 3), "max": s[-1]}


class Recorder:
    """Latency samples (seconds) plus an outcome counter, safe to share between threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.samples: list[float] = []
        self.errors: dict[str, int] = {}

    def ok(self, seconds: float) -> None:
        with self._lock:
            self.samples.append(seconds)

    def fail(self, reason: str) -> None:
        with self._lock:
            self.errors[reason[:120]] = self.errors.get(reason[:120], 0) + 1

    def summary(self) -> dict:
        with self._lock:
            out = summarize(self.samples)
            out["errors"] = sum(self.errors.values())
            out["error_kinds"] = dict(sorted(self.errors.items(), key=lambda kv: -kv[1])[:5])
            return out
