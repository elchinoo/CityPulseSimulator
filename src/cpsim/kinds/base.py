"""What every workload kind shares: results and their outcome classes, the HTTP helper, parameter helpers and the sender base.

A kind is a small module with a PURE generator (`plan`: decides what to send from a seeded RNG, no network) and a sender
(`execute`: sends one planned item and classifies the answer). Outcome classes are short strings counted per workload.
Declines, fraud rejections, rate limits and exhausted budgets are EXPECTED outcomes (not errors); only the classes in
ERROR_OUTCOMES set the "last error".
"""
import asyncio
import math
import time
from dataclasses import dataclass

import httpx

from ..model import SpecError
from ..tokens import BearerAuth

# classes that mean something is wrong (a refused credential, a bad request, a broken or unreachable target)
ERROR_OUTCOMES = frozenset({"auth_error", "client_error", "server_error", "unavailable", "network_error", "generator_error"})
# classes that make the loop back off (the target is pushing back)
MAX_RETRY_AFTER = 120.0


@dataclass(slots=True)
class Result:
    outcome: str
    latency: float | None = None      # seconds; None when no request was made or it is not a single call
    error: str = ""                   # short message for an error class (never a body, header or credential)
    retry_after: float = 0.0          # seconds the target asked us to wait
    throttled: bool = False           # 429/503: the loop backs off
    fatal: bool = False               # the target refuses us for good (bad credential): stop and say so

    @property
    def is_error(self) -> bool:
        return self.outcome in ERROR_OUTCOMES


def parse_retry_after(headers) -> float:
    try:
        v = float(headers.get("retry-after", "0") or 0)
    except (ValueError, AttributeError):
        return 0.0
    return min(max(v, 0.0), MAX_RETRY_AFTER) if math.isfinite(v) else 0.0


def classify_status(status: int, headers=None) -> Result:
    """The generic mapping from an HTTP status; a kind refines it (a 402 or 403 may be an expected business outcome)."""
    if 200 <= status < 300:
        return Result("ok")
    if status == 401:
        return Result("auth_error", error=f"HTTP {status}: the target refused the credential", fatal=True)
    if status == 429:
        return Result("rate_limited", throttled=True, retry_after=parse_retry_after(headers or {}))
    if status in (502, 503, 504):
        return Result("unavailable", error=f"HTTP {status}", throttled=status == 503, retry_after=parse_retry_after(headers or {}))
    if status >= 500:
        return Result("server_error", error=f"HTTP {status}")
    return Result("client_error", error=f"HTTP {status}")


def json_error_code(response: httpx.Response) -> str:
    """The `error.code` (PayBroker, LLM gateway) or string `detail` of a JSON error body, upper/lower as sent; '' if none."""
    try:
        body = response.json()
    except ValueError:
        return ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("code"), str):
            return err["code"]
        if isinstance(err, str):
            return err
    return ""


class Http:
    """One client per workload. The credential goes only to the workload's URL; redirects are never followed."""

    def __init__(self, base_url: str, token=None, *, transport=None, timeout: float = 20.0):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=timeout, follow_redirects=False, transport=transport,
                                        auth=BearerAuth(token) if token else None)

    async def call(self, method: str, path: str, *, json=None, headers=None, data=None, auth=None) -> tuple[httpx.Response | None, float, str]:
        """(response, seconds, error class name). On a network failure the response is None and only the class name is kept."""
        t0 = time.monotonic()
        try:
            kw = {} if auth is None else {"auth": auth}
            r = await self.client.request(method, path, json=json, headers=headers, data=data, **kw)
        except httpx.HTTPError as exc:
            return None, time.monotonic() - t0, type(exc).__name__
        return r, time.monotonic() - t0, ""

    async def close(self) -> None:
        await self.client.aclose()


def network_result(latency: float, name: str) -> Result:
    return Result("network_error", latency, error=f"the target did not answer ({name})")


# ---- parameter helpers (shared by the kinds' validators) --------------------------------------------------------------
def p_num(params: dict, key: str, default, lo: float, hi: float) -> float:
    v = params.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
        raise SpecError(f"params.{key} must be a number between {lo:g} and {hi:g}")
    return float(v)


def p_int(params: dict, key: str, default, lo: int, hi: int):
    v = params.get(key, default)
    if v is None and default is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise SpecError(f"params.{key} must be a whole number from {lo} to {hi}")
    return v


def p_list(params: dict, key: str, default: list[str], pattern, *, max_items: int = 20) -> list[str]:
    v = params.get(key, default)
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",") if x.strip()]
    if not isinstance(v, list) or not v or len(v) > max_items or not all(isinstance(x, str) and pattern.match(x) for x in v):
        raise SpecError(f"params.{key} must be a list of 1 to {max_items} valid names")
    return v


def p_bool(params: dict, key: str, default: bool) -> bool:
    v = params.get(key, default)
    if not isinstance(v, bool):
        raise SpecError(f"params.{key} must be true or false")
    return v


def p_mix(params: dict, key: str, default: dict[str, float]) -> dict[str, float]:
    """A percentage mix. Every share is 0..100; at least one is above zero. Shares are weights: they are scaled to 100 when used."""
    v = params.get(key, default)
    if not isinstance(v, dict) or set(v) - set(default):
        raise SpecError(f"params.{key} must be an object with the keys {', '.join(default)}")
    out = {k: p_num(v, k, default[k], 0, 100) for k in default}
    for k in v:
        if k not in out:
            out[k] = 0.0
    if sum(out.values()) <= 0:
        raise SpecError(f"params.{key} needs at least one share above zero")
    return out


def unknown_params(params: dict, allowed: set[str]) -> None:
    extra = set(params) - allowed
    if extra:
        raise SpecError(f"unknown param(s): {', '.join(sorted(extra))}")


def pick_weighted(rng, mix: dict[str, float]) -> str:
    total = sum(mix.values())
    roll = rng.random() * total
    acc = 0.0
    last = ""
    for k, w in mix.items():
        if w <= 0:
            continue
        last = k
        acc += w
        if roll < acc:
            return k
    return last


class BaseSender:
    """plan(n) decides what to send (pure, seeded); execute(op) sends one item; run_batch ties them together.

    With no target (`dry`), items are planned and counted as `generated` and nothing leaves the process.
    """

    def __init__(self, params: dict, http: Http | None, rng):
        self.params, self.http, self.rng = params, http, rng
        self.dry = http is None
        self.setup_calls = 0

    def plan(self, n: int) -> list:
        raise NotImplementedError

    async def execute(self, op) -> Result:
        raise NotImplementedError

    def extra(self) -> dict:
        """Kind-specific numbers for the status (e.g. customers in the pool)."""
        return {"setup_calls": self.setup_calls} if self.setup_calls else {}

    async def run_batch(self, n: int, concurrency: int) -> list[Result]:
        ops = self.plan(n)
        if self.dry:
            return [Result("generated") for _ in ops]
        sem = asyncio.Semaphore(concurrency)

        async def one(op) -> Result:
            async with sem:
                try:
                    return await self.execute(op)
                except Exception as exc:  # noqa: BLE001 - one bad item must not stop the batch; keep only the class name
                    return Result("generator_error", error=f"unexpected {type(exc).__name__}")
        return list(await asyncio.gather(*(one(op) for op in ops)))

    async def close(self) -> None:
        if self.http:
            await self.http.close()
