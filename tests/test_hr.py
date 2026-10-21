"""Company simulations for TrustDesk: validation, reproducible generation, the sender and the web API."""
import asyncio
import sys
import time
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from cpsim import api, auth
from cpsim.hr_catalog import TYPES
from cpsim.hr_engine import HrRun
from cpsim.hr_gen import build_plan
from cpsim.hr_model import parse_company
from cpsim.model import SpecError
from cpsim.sinks import Fatal

ALL = {k: 12 for k in TYPES}


def company(**kw):
    spec = {"name": "acme", "seed": 5, "employees": {"new": 8, "tenured": 24}, "requests": ALL}
    spec.update(kw)
    return parse_company(spec)


# ---- validation --------------------------------------------------------------------------------------
@pytest.mark.parametrize("change, message", [
    ({"name": "Bad Name"}, "name must be"),
    ({"surprise": 1}, "unknown company field"),
    ({"requests": {"teleport": 3}}, "unknown request type"),
    ({"requests": {"vacation": -1}}, "whole number"),
    ({"requests": {"vacation": 5001}}, "whole number"),
    ({"requests": {"vacation": True}}, "whole number"),
    ({"requests": {k: 3000 for k in list(TYPES)[:8]}}, "at most 20000"),
    ({"employees": {"new": 3000}}, "whole number"),
    ({"employees": {"new": 1500, "tenured": 1000}}, "at most 2000"),
    ({"employees": {"new": 0, "tenured": 0}}, "need employees"),
    ({"employees": {"bogus": 1}}, "employees must be"),
    ({"token_env": "DATABASE_URL"}, "contains 'TOKEN'"),
    ({"token_env": "my token"}, "NAME of an environment variable"),
    ({"token_env": "td_token"}, "NAME of an environment variable"),
    ({"target_url": "http://example.org"}, "https"),
    ({"target_url": "https://user:pw@example.org"}, "credentials"),
    ({"target_url": "https://169.254.169.254"}, "link-local"),
    ({"problem_pct": 101}, "problem_pct"),
    ({"rate": 0}, "rate"),
    ({"rate": float("nan")}, "rate"),
    ({"concurrency": 99}, "concurrency"),
    ({"employee_prefix": "lower"}, "employee_prefix"),
    ({"seed": -1}, "seed"),
])
def test_bad_companies_are_refused(change, message):
    with pytest.raises(SpecError, match=message):
        company(**change)


def test_a_good_company_round_trips():
    c = company(target_url="https://td.example.org/", problem_pct=40)
    again = parse_company(c.to_dict())
    assert again == c and c.target_url == "https://td.example.org" and c.total_requests() == 12 * len(TYPES)


# ---- generation --------------------------------------------------------------------------------------
TODAY = date(2026, 10, 21)


def test_exact_counts_per_type_and_reproducibility():
    c = company(requests={"vacation": 30, "sick_leave": 20, "expense_customer": 15})
    p1, p2 = build_plan(c, TODAY), build_plan(c, TODAY)
    counts = {}
    for it in p1.items:
        counts[it.type] = counts.get(it.type, 0) + 1
    assert counts == {"vacation": 30, "sick_leave": 20, "expense_customer": 15}
    assert [i.payload for i in p1.items] == [i.payload for i in p2.items] and p1.seed == 5
    other = build_plan(company(seed=6, requests=c.requests), TODAY)
    assert [i.payload for i in other.items] != [i.payload for i in p1.items]


def test_no_seed_still_gives_a_plan_and_reports_its_seed():
    p = build_plan(company(seed=None, requests={"vacation": 5}), TODAY)
    assert 0 <= p.seed < 2**32 and len(p.items) == 5


def test_employees_new_tenured_managers_and_privacy():
    p = build_plan(company(employees={"new": 20, "tenured": 60}), TODAY)
    emps = p.employees
    assert len(emps) == 80 and len({e.employee_no for e in emps}) == 80 and len({e.email for e in emps}) == 80
    assert all(e.email.endswith("@example.com") for e in emps)  # never a real address
    new = [e for e in emps if e.tenure_days(TODAY) < 100]
    assert len(new) == 20 and all(e.tenure_days(TODAY) <= 75 for e in new)
    assert all(e.tenure_days(TODAY) >= 400 for e in emps if e not in new)
    seen = set()
    for e in emps:  # managers always come before the people who report to them
        if e.manager_no:
            assert e.manager_no in seen
        seen.add(e.employee_no)
    assert any(e.manager_no for e in emps) and any(e.title.endswith("Manager") for e in emps)


def test_problem_share_is_respected():
    base = {"vacation": 100, "expense_customer": 100, "expense_meals": 100, "sick_leave": 100}
    none = build_plan(company(problem_pct=0, requests=base), TODAY)
    assert none.problems() == 0
    allp = build_plan(company(problem_pct=100, requests=base), TODAY)
    assert allp.problems() >= 0.9 * 400
    mid = build_plan(company(problem_pct=30, requests=base), TODAY).problems()
    assert 80 <= mid <= 160


def test_ok_vacations_of_one_employee_never_overlap():
    c = company(problem_pct=0, employees={"new": 0, "tenured": 3}, requests={"vacation": 25})
    spans = {}
    for it in build_plan(c, TODAY).items:
        p = it.payload
        spans.setdefault(p["employee_no"], []).append((p["start_date"], p["end_date"]))
    for windows in spans.values():
        windows.sort()
        for (_, e1), (s2, _) in zip(windows, windows[1:]):
            assert e1 < s2


def test_overlap_problems_really_overlap_an_earlier_request():
    c = company(problem_pct=100, employees={"new": 0, "tenured": 4}, requests={"vacation": 80})
    by = {}
    for it in sorted(build_plan(c, TODAY).items, key=lambda i: i.index):
        by.setdefault(it.payload["employee_no"], []).append(it.payload)
    overlaps = sum(1 for ps in by.values() for i, a in enumerate(ps) for b in ps[:i]
                   if a["start_date"] <= b["end_date"] and b["start_date"] <= a["end_date"])
    assert overlaps > 0


def test_withdraw_share_and_use_existing():
    from cpsim.hr_gen import Employee
    old = Employee("REAL-1", "Real Person", "real@example.com", "Sales", "AE", "L3", "US", "2015-01-01", None, True)
    c = company(employees={"new": 0, "tenured": 0, "use_existing": True}, requests={"vacation": 20}, withdraw_pct=30)
    p = build_plan(c, TODAY, [old])
    assert p.employees == [] and {i.payload["employee_no"] for i in p.items} == {"REAL-1"}
    assert 1 <= sum(i.withdraw for i in p.items) <= 14
    assert build_plan(company(employees={"new": 0, "tenured": 5}, requests={"vacation": 3}), TODAY, [old]).items[0] \
        .payload["employee_no"] != "REAL-1"          # existing employees are ignored unless asked for


@pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "trustdesk/src/trustdesk/request_types.py").exists(),
                    reason="the TrustDesk sources are not next to this repository")
def test_every_generated_payload_passes_trustdesks_own_validation():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "trustdesk/src"))
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "td_request_types", Path(__file__).resolve().parents[2] / "trustdesk/src/trustdesk/request_types.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.pop(0)
    for seed in range(1, 15):
        p = build_plan(company(seed=seed, problem_pct=50, requests=ALL), date.today())
        for it in p.items:
            try:
                mod.normalize(it.payload)
            except mod.RequestError as exc:
                pytest.fail(f"{it.type}: {exc} in {it.payload}")


# ---- the sender (against a fake TrustDesk) -------------------------------------------------------------
class FakeTrustDesk:
    def __init__(self, token_ok=True, known_types=None, refuse_type=None, existing=()):
        self.token_ok, self.refuse_type = token_ok, refuse_type
        self.known = known_types or list(TYPES)
        self.employees = {e["employee_no"]: e for e in existing}
        self.requests, self.keys, self.calls, self.withdrawn = {}, [], 0, []

    def client(self):
        outer = self

        class C:
            http = type("H", (), {"base_url": "https://fake", "headers": {"Authorization": "Bearer t"}})()

            async def call(self, method, path, *, json=None, headers=None):
                return outer.handle(method, path, json, headers or {})

            async def close(self):
                return None
        return C()

    def handle(self, method, path, body, headers):
        self.calls += 1
        if not self.token_ok:
            raise Fatal("TrustDesk refused the token (HTTP 401); it must have the Client application scope")
        r = lambda code, data: httpx.Response(code, json=data)  # noqa: E731
        if path == "/api/v1/request-types":
            return r(200, {"types": [{"code": k} for k in self.known]})
        if path.startswith("/api/v1/employees") and method == "GET":
            return r(200, {"total": len(self.employees), "items": list(self.employees.values())})
        if path == "/api/v1/employees":
            if body["employee_no"] in self.employees:
                return r(409, {"detail": "exists"})
            if body.get("manager_no") and body["manager_no"] not in self.employees:
                return r(422, {"detail": "manager_no does not match any employee"})
            self.employees[body["employee_no"]] = body
            return r(201, body)
        if path == "/api/v1/requests":
            if body["employee_no"] not in self.employees:
                return r(404, {"detail": "employee not found"})
            if body["type"] == self.refuse_type:
                return r(422, {"detail": "details.x is required"})
            key = headers["Idempotency-Key"]
            self.keys.append(key)
            ref = f"REQ-{len(self.requests) + 1:06d}"
            self.requests[ref] = {"status": "pending_review", "type": body["type"]}
            return r(201, {"ref": ref})
        if path.endswith("/withdraw"):
            ref = path.split("/")[-2]
            self.requests[ref]["status"] = "withdrawn"
            self.withdrawn.append(ref)
            return r(200, {})
        if path.startswith("/api/v1/requests/REQ-"):
            return r(200, {"status": self.requests[path.rsplit("/", 1)[1]]["status"]})
        return r(404, {"detail": "nope"})


async def finish(run):
    run.start()
    await run._task


async def test_run_sends_everything_managers_first_with_idempotency_keys():
    fake = FakeTrustDesk()
    c = company(requests={"vacation": 20, "expense_customer": 10}, withdraw_pct=20, rate=200)
    run = HrRun(c, fake.client, TODAY)
    await finish(run)
    s = run.snapshot()
    assert s["state"] == "done" and s["employees"]["created"] == 32 and s["employees"]["failed"] == 0
    assert s["totals"]["accepted"] == 30 and s["totals"]["sent"] == 30 and s["totals"]["invalid"] == 0
    assert len(set(fake.keys)) == 30 and all(k.startswith("sim-5-") for k in fake.keys)
    assert s["types"]["vacation"]["accepted"] == 20 and s["types"]["vacation"]["planned"] == 20
    assert s["totals"]["withdrawn"] == len(fake.withdrawn) >= 1
    assert s["seed"] == 5 and not s["dry_run"]


async def test_rerun_with_the_same_seed_finds_the_employees_already_there():
    fake = FakeTrustDesk()
    c = company(requests={"vacation": 5}, rate=200)
    await finish(HrRun(c, fake.client, TODAY))
    again = HrRun(c, fake.client, TODAY)
    await finish(again)
    assert again.snapshot()["employees"]["existing"] == 32 and again.snapshot()["employees"]["created"] == 0


async def test_invalid_requests_are_counted_with_the_reason_and_the_run_goes_on():
    fake = FakeTrustDesk(refuse_type="other")
    run = HrRun(company(requests={"vacation": 6, "other": 4}, rate=200), fake.client, TODAY)
    await finish(run)
    s = run.snapshot()
    assert s["state"] == "done" and s["types"]["other"]["invalid"] == 4 and s["types"]["vacation"]["accepted"] == 6
    assert s["errors"] and "required" in s["errors"][0]["message"]


async def test_unknown_types_on_the_target_are_reported():
    fake = FakeTrustDesk(known_types=["vacation"])
    run = HrRun(company(requests={"vacation": 2, "other": 2}, rate=200), fake.client, TODAY)
    await finish(run)
    assert run.snapshot()["unknown_types"] == ["other"]


async def test_a_refused_token_fails_the_run_clearly():
    run = HrRun(company(requests={"vacation": 2}), FakeTrustDesk(token_ok=False).client, TODAY)
    await finish(run)
    s = run.snapshot()
    assert s["state"] == "failed" and "refused the token" in s["fatal"] and s["totals"]["sent"] == 0


async def test_rate_limit_is_honoured():
    fake = FakeTrustDesk()
    c = company(employees={"new": 0, "tenured": 3}, requests={"vacation": 20}, rate=40, concurrency=8)
    run = HrRun(c, fake.client, TODAY)
    t0 = time.monotonic()
    await finish(run)
    elapsed = time.monotonic() - t0
    assert run.snapshot()["totals"]["accepted"] == 20
    assert elapsed >= (3 + 20 - 1) / 40 * 0.9  # employees and requests share the same budget


async def test_stop_ends_the_run_early():
    fake = FakeTrustDesk()
    run = HrRun(company(employees={"new": 0, "tenured": 3}, requests={"vacation": 200}, rate=20), fake.client, TODAY)
    run.start()
    await asyncio.sleep(0.6)
    await run.stop()
    s = run.snapshot()
    assert s["state"] == "stopped" and 0 < s["totals"]["sent"] < 200


async def test_dry_run_sends_nothing_and_refresh_statuses_reads_them_back():
    dry = HrRun(company(requests={"vacation": 7}), None, TODAY)
    await finish(dry)
    s = dry.snapshot()
    assert s["state"] == "done" and s["dry_run"] and s["totals"]["accepted"] == 7 and dry.refs == []
    fake = FakeTrustDesk()
    run = HrRun(company(requests={"vacation": 9}, withdraw_pct=0, rate=200), fake.client, TODAY)
    await finish(run)
    fake.requests["REQ-000001"]["status"] = "approved"
    assert await run.refresh_statuses() == {"pending_review": 8, "approved": 1}
    assert run.snapshot()["status_sample"] == 9


async def test_use_existing_employees_are_picked_up_from_trustdesk():
    old = {"employee_no": "OLD-1", "name": "Old Hand", "email": "o@example.com", "department": "Sales", "title": "AE",
           "level": "L4", "country": "US", "hire_date": "2012-04-01"}
    fake = FakeTrustDesk(existing=[old])
    c = company(employees={"new": 0, "tenured": 0, "use_existing": True}, requests={"vacation": 6}, rate=200)
    run = HrRun(c, fake.client, TODAY)
    await finish(run)
    s = run.snapshot()
    assert s["employees"]["planned"] == 0 and s["totals"]["accepted"] == 6


# ---- the web API -----------------------------------------------------------------------------------
H = {"X-CPSim-CSRF": "1"}
PASSWORD = "correct-horse-battery"  # pragma: allowlist secret


@pytest.fixture
def client():
    api.store.delete("admin_password_hash")
    for name in list(api.store.companies()):
        api.store.delete_company(name)
    auth._setup_code, auth._sessions.clear(), api._failures.clear()
    api.runtime.hr_runs.clear()
    with TestClient(api.app) as c:
        assert c.post("/api/setup", json={"code": auth.setup_code(), "password": PASSWORD}, headers=H).status_code == 200
        yield c


SPEC = {"employees": {"new": 3, "tenured": 6}, "requests": {"vacation": 8, "sick_leave": 5}, "seed": 3}


def test_company_api_requires_a_session():
    with TestClient(api.app) as c:
        for method, path in [("get", "/api/companies"), ("get", "/api/hr/catalog"), ("post", "/api/companies/x/run")]:
            assert getattr(c, method)(path, headers=H).status_code == 401


def test_catalog_and_presets_are_valid(client):
    cat = client.get("/api/hr/catalog").json()
    assert len(cat["types"]) == 19 and {p["id"] for p in cat["presets"]} == {"quick", "hundreds", "leave", "expenses"}
    for p in cat["presets"]:
        c = parse_company({"name": "p", "employees": p["employees"], "requests": p["requests"]})
        assert c.total_requests() > 0
    hundreds = next(p for p in cat["presets"] if p["id"] == "hundreds")
    assert sum(hundreds["requests"].values()) == 500 and set(hundreds["requests"]) == set(TYPES)


def test_company_crud_validation_preview_and_dry_run(client):
    assert client.put("/api/companies/acme", json={**SPEC, "requests": {"nope": 1}}, headers=H).status_code == 422
    assert client.put("/api/companies/acme", json={**SPEC, "token_env": "DATABASE_URL"}, headers=H).status_code == 422
    ok = client.put("/api/companies/acme", json=SPEC, headers=H)
    assert ok.status_code == 200 and ok.json() == {"ok": True, "employees": 9, "requests": 13}
    listed = client.get("/api/companies").json()
    assert listed[0]["name"] == "acme" and listed[0]["requests"] == 13 and listed[0]["dry_run"] is True
    got = client.get("/api/companies/acme").json()
    assert got["requests"] == {"vacation": 8, "sick_leave": 5} and got["employees"]["tenured"] == 6
    pv = client.post("/api/companies/acme/preview", headers=H).json()
    assert pv["requests"] == 13 and pv["employees"] == 9 and pv["types"]["vacation"]["planned"] == 8
    assert pv["samples"]["vacation"] and "employee_no" in pv["samples"]["vacation"][0]
    assert client.get("/api/companies/acme/status").status_code == 404
    started = client.post("/api/companies/acme/run", headers=H)
    assert started.status_code == 200
    for _ in range(50):
        st = client.get("/api/companies/acme/status").json()
        if st["state"] != "running":
            break
        time.sleep(0.1)
    assert st["state"] == "done" and st["dry_run"] and st["totals"]["accepted"] == 13
    assert client.delete("/api/companies/acme", headers=H).status_code == 200
    assert client.get("/api/companies/acme").status_code == 404


def test_a_company_with_a_target_needs_its_token_variable(client, monkeypatch):
    monkeypatch.delenv("TRUSTDESK_CLIENT_TOKEN", raising=False)
    client.put("/api/companies/live", json={**SPEC, "target_url": "https://td.example.org"}, headers=H)
    r = client.post("/api/companies/live/run", headers=H)
    assert r.status_code == 422 and "TRUSTDESK_CLIENT_TOKEN" in r.json()["detail"]
    assert client.post("/api/companies/ghost/run", headers=H).status_code == 404
    assert client.post("/api/companies/ghost/preview", headers=H).status_code == 404


def test_metrics_include_company_runs(client):
    client.put("/api/companies/acme", json=SPEC, headers=H)
    client.post("/api/companies/acme/run", headers=H)
    for _ in range(50):
        if client.get("/api/companies/acme/status").json()["state"] != "running":
            break
        time.sleep(0.1)
    from cpsim import metrics
    body = metrics.render()[0].decode()
    assert 'cpsim_hr_requests_total{company="acme",outcome="accepted",type="vacation"} 8.0' in body
    assert 'cpsim_hr_employees_total{company="acme",outcome="created"} 9.0' in body
