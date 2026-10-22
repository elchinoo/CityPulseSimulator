"""Sends a company's plan to TrustDesk through its client API: employees first (managers before their reports),
then requests at a bounded rate, then the withdrawals. One run per company at a time."""
import asyncio
import time
from collections import Counter, deque
from datetime import date

import httpx

from .hr_gen import Employee, Plan, build_plan
from .hr_model import Company
from .sinks import Fatal
from .tokens import BearerAuth

RETRIES = 5
MAX_REFS = 5000


class TrustDeskClient:
    """Bearer token goes only to the configured URL; redirects are never followed."""

    def __init__(self, url: str, token):
        self.http = httpx.AsyncClient(base_url=url, timeout=30, follow_redirects=False, auth=BearerAuth(token))

    async def call(self, method: str, path: str, *, json=None, headers=None) -> httpx.Response:
        last = "no answer"
        for attempt in range(RETRIES):
            try:
                r = await self.http.request(method, path, json=json, headers=headers)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                await asyncio.sleep(min(2 ** attempt, 15))
                continue
            if r.status_code in (401, 403):
                raise Fatal(f"TrustDesk refused the token (HTTP {r.status_code}); it must have the Client application scope")
            if r.status_code in (429, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                try:
                    wait = float(r.headers.get("retry-after", "0") or 0)
                except ValueError:
                    wait = 0.0
                await asyncio.sleep(min(max(wait, 2 ** attempt), 30))
                continue
            return r
        raise ConnectionError(f"TrustDesk did not answer after {RETRIES} tries ({last})")

    async def close(self) -> None:
        await self.http.aclose()


def _detail(r: httpx.Response) -> str:
    try:
        d = r.json().get("detail")
        return d if isinstance(d, str) else str(d)[:200]
    except Exception:  # noqa: BLE001
        return f"HTTP {r.status_code}"


class HrRun:
    def __init__(self, company: Company, make_client=None, today: date | None = None):
        """`make_client` builds a TrustDeskClient (None means generate only, send nothing)."""
        self.company, self.make_client, self.today = company, make_client, today
        self.client: TrustDeskClient | None = None
        self.failed_employees: set[str] = set()
        self.state = "idle"
        self.fatal = ""
        self.plan: Plan | None = None
        self.emp = Counter(planned=0, created=0, existing=0, failed=0, skipped_requests=0)
        self.types: dict[str, Counter] = {}
        self.errors: deque = deque(maxlen=20)
        self.refs: list[tuple[str, str]] = []
        self.statuses: Counter = Counter()
        self.status_sample = 0
        self.started = self.finished = 0.0
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._next = 0.0
        self._lock = asyncio.Lock()
        self.unknown_types: list[str] = []

    # ---- control --------------------------------------------------------------------------------------
    def start(self) -> None:
        self.state, self.started = "running", time.time()
        self._task = asyncio.create_task(self._main())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)

    @property
    def running(self) -> bool:
        return self.state == "running"

    async def _pace(self) -> None:
        """At most `rate` requests per second across all workers."""
        async with self._lock:
            now = time.monotonic()
            self._next = max(self._next, now)
            wait, self._next = self._next - now, self._next + 1.0 / self.company.rate
        if wait > 0:
            await asyncio.sleep(wait)

    # ---- the run ----------------------------------------------------------------------------------------
    async def _main(self) -> None:
        try:
            self.client = self.make_client() if self.make_client else None
            existing = await self._discover()
            self.plan = build_plan(self.company, self.today, existing)
            for t, n in self.plan.counts.items():
                self.types[t] = Counter(planned=n, sent=0, accepted=0, invalid=0, errors=0, withdrawn=0)
            self.emp["planned"] = len(self.plan.employees)
            await self._create_employees(self.plan.employees)
            await self._send_requests(self.plan)
            self.state = "stopped" if self._stop.is_set() else "done"
        except Fatal as exc:
            self.state, self.fatal = "failed", str(exc)
        except ConnectionError as exc:
            self.state, self.fatal = "failed", str(exc)
        except Exception as exc:  # noqa: BLE001 - never let a run die silently
            self.state, self.fatal = "failed", f"unexpected error: {type(exc).__name__}"
        finally:
            self.finished = time.time()
            if self.client:
                await self.client.close()

    async def _discover(self) -> list[Employee]:
        if not self.client:
            return []
        r = await self.client.call("GET", "/api/v1/request-types")
        if r.status_code != 200:
            raise Fatal(f"TrustDesk answered HTTP {r.status_code} for /api/v1/request-types; is this a TrustDesk with the client API?")
        known = {t["code"] for t in r.json().get("types", [])}
        self.unknown_types = sorted(set(self.company.requests) - known)
        out: list[Employee] = []
        if self.company.use_existing:
            for offset in range(0, 1000, 200):
                r = await self.client.call("GET", f"/api/v1/employees?status=active&limit=200&offset={offset}")
                if r.status_code != 200:
                    break
                for e in r.json()["items"]:
                    if e.get("hire_date"):
                        out.append(Employee(e["employee_no"], e["name"], e["email"], e["department"], e["title"],
                                            e["level"], e["country"] or "US", str(e["hire_date"])[:10], None, True))
                if len(r.json()["items"]) < 200:
                    break
        return out

    async def _create_employees(self, employees: list[Employee]) -> None:
        sem = asyncio.Semaphore(self.company.concurrency)

        async def one(e: Employee) -> None:
            async with sem:
                if self._stop.is_set():
                    return
                if not self.client:
                    self.emp["created"] += 1
                    return
                await self._pace()
                r = await self.client.call("POST", "/api/v1/employees", json=e.payload())
                if r.status_code == 201:
                    self.emp["created"] += 1
                elif r.status_code == 409:
                    self.emp["existing"] += 1
                else:
                    self.emp["failed"] += 1
                    self.errors.append({"what": f"employee {e.employee_no}", "message": _detail(r)})
                    self.failed_employees.add(e.employee_no)

        managers = [e for e in employees if not e.manager_no]
        await asyncio.gather(*(one(e) for e in managers))
        await asyncio.gather(*(one(e) for e in employees if e.manager_no))

    async def _send_requests(self, plan: Plan) -> None:
        broken = self.failed_employees
        sem = asyncio.Semaphore(self.company.concurrency)
        withdraw: list[tuple[str, str]] = []

        async def one(item) -> None:
            async with sem:
                if self._stop.is_set():
                    return
                t = self.types[item.type]
                if item.payload["employee_no"] in broken:
                    self.emp["skipped_requests"] += 1
                    return
                t["sent"] += 1
                if not self.client:
                    t["accepted"] += 1
                    return
                await self._pace()
                r = await self.client.call("POST", "/api/v1/requests", json=item.payload,
                                           headers={"Idempotency-Key": f"sim-{plan.seed}-{item.index}"})
                if r.status_code in (200, 201):
                    t["accepted"] += 1
                    ref = r.json().get("ref")
                    if ref and len(self.refs) < MAX_REFS:
                        self.refs.append((ref, item.type))
                    if item.withdraw and ref:
                        withdraw.append((ref, item.type))
                elif r.status_code in (404, 422):
                    t["invalid"] += 1
                    if len(self.errors) < 20:
                        self.errors.append({"what": f"{item.type} #{item.index}", "message": _detail(r)})
                else:
                    t["errors"] += 1
                    self.errors.append({"what": f"{item.type} #{item.index}", "message": f"HTTP {r.status_code}"})

        await asyncio.gather(*(one(i) for i in plan.items))
        if not self.client:
            return
        for ref, type_ in withdraw:   # the employee changes their mind after sending
            if self._stop.is_set():
                break
            await self._pace()
            r = await self.client.call("POST", f"/api/v1/requests/{ref}/withdraw", json={"note": "Plans changed (simulated)"})
            if r.status_code == 200:
                self.types[type_]["withdrawn"] += 1

    # ---- what happened to the requests ----------------------------------------------------------------------
    async def refresh_statuses(self, limit: int = 500) -> dict:
        """Ask TrustDesk for the current status of a sample of the requests this run created."""
        if not self.make_client or not self.refs:
            return {}
        client = self.client if self.running and self.client else self.make_client()
        sample, tally = self.refs[:limit], Counter()
        sem = asyncio.Semaphore(8)

        async def one(ref: str) -> None:
            async with sem:
                r = await client.call("GET", f"/api/v1/requests/{ref}")
                if r.status_code == 200:
                    tally[r.json()["status"]] += 1

        try:
            await asyncio.gather(*(one(ref) for ref, _ in sample))
        finally:
            if client is not self.client:
                await client.close()
        self.statuses, self.status_sample = tally, len(sample)
        return dict(tally)

    def snapshot(self) -> dict:
        sent = sum(t["sent"] for t in self.types.values())
        total = sum(t["planned"] for t in self.types.values())
        elapsed = (self.finished or time.time()) - self.started if self.started else 0
        return {"company": self.company.name, "state": self.state, "fatal": self.fatal, "seed": self.plan.seed if self.plan else None,
                "dry_run": self.make_client is None, "employees": dict(self.emp),
                "types": {k: dict(v) for k, v in self.types.items()},
                "totals": {"planned": total, "sent": sent, "accepted": sum(t["accepted"] for t in self.types.values()),
                           "invalid": sum(t["invalid"] for t in self.types.values()),
                           "errors": sum(t["errors"] for t in self.types.values()),
                           "withdrawn": sum(t["withdrawn"] for t in self.types.values()),
                           "problem_planned": self.plan.problems() if self.plan else 0},
                "elapsed": round(elapsed, 1), "rate": round(sent / elapsed, 1) if elapsed > 0 else 0,
                "errors": list(self.errors), "unknown_types": self.unknown_types,
                "statuses": dict(self.statuses), "status_sample": self.status_sample}
