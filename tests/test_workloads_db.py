"""Workloads over the web API with Postgres: permissions per role and scope, validation, start/stop/run-once on a mock target,
metrics, the global ceiling, secrets never stored or echoed, boot-time resume and recurring company runs."""
import json
import time

import httpx
import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from fastapi.testclient import TestClient
from test_federation_db import AUD, ISS, Provider, access
from test_workload_kinds import PayBrokerMock

from cpsim import api, core, kinds
from cpsim.core import db

CSRF = {"X-CPSim-CSRF": "1"}
SPEC = {"kind": "paybroker", "target_url": "https://pb.example", "schedule": {"interval": 0.2, "batch": 2}}
KEY = "sk_test_SENTINEL"


@pytest.fixture
def client(monkeypatch, db):
    for v in ("CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_METRICS_TOKEN", "CPSIM_MAX_RATE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", KEY)
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    mock = PayBrokerMock(KEY)
    box = {"handler": mock.handler}           # a test may swap the target's behaviour
    real = kinds.build_sender
    monkeypatch.setattr(kinds, "build_sender", lambda wl, **kw: real(wl, transport=httpx.MockTransport(lambda req: box["handler"](req)), **kw))
    with TestClient(api.app) as c:
        c.mock, c.box = mock, box
        yield c


def who(*roles, **kw):
    return access(sub=kw.pop("sub", "usr_1"), roles=[f"cpsim:{r}" for r in roles], **kw)


def test_roles_decide_who_may_see_run_and_edit_workloads(client):
    admin, operator, viewer, stranger = who("admin"), who("operator"), who("viewer", sub="v"), who("other", sub="s")
    assert client.put("/api/workloads/shop", headers=operator, json=SPEC).status_code == 200
    assert client.put("/api/workloads/shop2", headers=viewer, json=SPEC).status_code == 403
    assert client.put("/api/workloads/shop2", headers=stranger, json=SPEC).status_code == 403
    for ok in (admin, operator, viewer):
        assert client.get("/api/workloads", headers=ok).status_code == 200
        assert client.get("/api/workloads/shop", headers=ok).status_code == 200
        assert client.get("/api/workloads/catalog", headers=ok).status_code == 200
    assert client.get("/api/workloads", headers=stranger).status_code == 403
    for path in ("start", "stop", "run-once"):
        assert client.post(f"/api/workloads/shop/{path}", headers=viewer).status_code == 403, path
        assert client.post(f"/api/workloads/shop/{path}", headers=stranger).status_code == 403, path
    assert client.post("/api/workloads/shop/scenario", headers=viewer, json={"scenario": "healthy"}).status_code == 403
    assert client.delete("/api/workloads/shop", headers=viewer).status_code == 403
    assert client.post("/api/workloads/shop/run-once", headers=operator).status_code == 200                # sim:run
    assert client.delete("/api/workloads/shop", headers=operator).status_code == 200                       # sim:edit


def test_service_and_local_token_scopes_keep_to_their_boundaries(client, monkeypatch):
    client.put("/api/workloads/shop", headers=who("admin"), json=SPEC)
    reader = access(sub="dash", sub_type="service", scope="cpsim.read", client_id="dash")
    scraper = access(sub="prom", sub_type="service", scope="cpsim.metrics", client_id="prom")
    assert client.get("/api/workloads", headers=reader).status_code == 200
    assert client.put("/api/workloads/x", headers=reader, json=SPEC).status_code == 403
    assert client.post("/api/workloads/shop/start", headers=reader).status_code == 403
    assert client.get("/api/workloads", headers=scraper).status_code == 403
    monkeypatch.setenv("CPSIM_API_KEY", "k" * 40)        # pragma: allowlist secret
    admin = {"Authorization": "Bearer " + "k" * 40}
    tokens = {s: {"Authorization": "Bearer " + client.post("/api/tokens", headers=admin, json={"name": s, "scope": s}).json()["token"]}
              for s in ("read", "metrics", "full")}
    assert client.get("/api/workloads", headers=tokens["read"]).status_code == 200
    assert client.post("/api/workloads/shop/run-once", headers=tokens["read"]).status_code == 403
    assert client.get("/api/workloads", headers=tokens["metrics"]).status_code == 403
    assert client.post("/api/workloads/shop/run-once", headers=tokens["full"]).status_code == 200
    assert client.put("/api/workloads/y", headers=admin, json=SPEC).status_code == 200


def test_validation_over_the_api(client):
    admin = who("admin")
    assert client.put("/api/workloads/shop", headers=admin, json=SPEC).json() == {"ok": True, "rate": 10.0}
    got = client.get("/api/workloads/shop", headers=admin).json()
    assert got["kind"] == "paybroker" and got["token_env"] == "PAYBROKER_SECRET_KEY" and got["schedule"]["interval"] == 0.2 and got["state"] == "idle"
    listing = client.get("/api/workloads", headers=admin).json()
    assert listing[0]["name"] == "shop" and listing[0]["state"] == "idle" and listing[0]["stats"] is None
    for bad in ({**SPEC, "token_env": "sk_test_abc123realvalue"}, {**SPEC, "token_env": "CPSIM_API_KEY"}, {**SPEC, "kind": "citypulse"},
                {**SPEC, "kind": "nope"}, {**SPEC, "target_url": "http://evil.example"}, {**SPEC, "schedule": {"interval": 0.1}},
                {**SPEC, "schedule": {"batch": 501}}, {**SPEC, "params": {"mix": {"regular": 500}}}, {**SPEC, "extra": 1}):
        assert client.put("/api/workloads/shop", headers=admin, json=bad).status_code == 422, bad
    assert client.put("/api/workloads/Bad_Name", headers=admin, json=SPEC).status_code == 422
    assert client.get("/api/workloads/ghost", headers=admin).status_code == 404
    assert client.get("/api/workloads/ghost/status", headers=admin).status_code == 404
    assert client.post("/api/workloads/ghost/start", headers=admin).status_code == 404
    assert client.post("/api/workloads/ghost/run-once", headers=admin).status_code == 404
    cat = client.get("/api/workloads/catalog", headers=admin).json()
    assert {k["key"] for k in cat["kinds"]} == {"paybroker", "llmgw", "pagerlens", "trustdesk", "identity", "shopfront"}
    assert "citypulse" in cat["managed_elsewhere"] and cat["limits"] == {"interval": [0.2, 3600.0], "batch": 500, "max_rate": 200.0}
    assert client.delete("/api/workloads/shop", headers=admin).status_code == 200
    assert client.get("/api/workloads", headers=admin).json() == []


def wait_for(client, headers, name, cond, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        s = client.get(f"/api/workloads/{name}/status", headers=headers).json()
        if cond(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out: {s}")


def test_start_run_stop_and_the_metrics(client):
    admin = who("admin")
    client.put("/api/workloads/shop", headers=admin, json={**SPEC, "params": {"capture_pct": 50}})
    r = client.post("/api/workloads/shop/start", headers=admin)
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert client.post("/api/workloads/shop/start", headers=admin).status_code == 409                       # already running
    assert client.post("/api/workloads/shop/run-once", headers=admin).status_code == 409
    assert client.put("/api/workloads/shop", headers=admin, json=SPEC).status_code == 409                   # stop it first
    s = wait_for(client, admin, "shop", lambda s: s["ticks"] >= 3 and s["items"] >= 6)
    assert s["outcomes"]["ok"] > 0 and s["skipped"] >= 0 and s["latency_p95"] is not None and s["extra"]["customers"] >= 25
    assert client.get("/api/workloads", headers=admin).json()[0]["state"] == "running"
    mtok = access(sub="prom", sub_type="service", scope="cpsim.metrics", client_id="prom")
    text = client.get("/metrics", headers=mtok).text
    for name in ('cpsim_workload_ticks_total{kind="paybroker",workload="shop"}', 'cpsim_workload_skipped_ticks_total{kind="paybroker",workload="shop"}',
                 'cpsim_workload_items_total{kind="paybroker",outcome="ok",workload="shop"}', "cpsim_workload_latency_seconds{", "cpsim_workloads_running 1.0"):
        assert name in text, name
    assert client.post("/api/workloads/shop/stop", headers=admin).json() == {"stopped": True}
    assert client.get("/api/workloads/shop/status", headers=admin).json()["state"] == "stopped"
    assert client.post("/api/workloads/shop/stop", headers=admin).json() == {"stopped": False}
    assert client.put("/api/workloads/shop", headers=admin, json=SPEC).status_code == 200                   # editable again
    assert client.post("/api/workloads/shop/run-once", headers=admin).json()["items"] == 2


def test_a_total_ends_the_run_by_itself(client):
    admin = who("admin")
    client.put("/api/workloads/shop", headers=admin, json={**SPEC, "schedule": {"interval": 0.2, "batch": 3, "total": 7}})
    client.post("/api/workloads/shop/start", headers=admin)
    s = wait_for(client, admin, "shop", lambda s: s["state"] == "done")
    assert s["items"] == 7 and s["ticks"] == 3


def test_a_missing_credential_or_company_is_refused_at_start_with_the_variable_name(client, monkeypatch):
    admin = who("admin")
    monkeypatch.delenv("PAYBROKER_SECRET_KEY")
    client.put("/api/workloads/shop", headers=admin, json=SPEC)
    r = client.post("/api/workloads/shop/start", headers=admin)
    assert r.status_code == 422 and "PAYBROKER_SECRET_KEY" in r.json()["detail"]
    assert client.post("/api/workloads/shop/run-once", headers=admin).status_code == 422
    client.put("/api/workloads/td", headers=admin, json={"kind": "trustdesk", "params": {"company": "ghost"}, "schedule": {"interval": 10}})
    r = client.post("/api/workloads/td/start", headers=admin)
    assert r.status_code == 422 and "ghost" in r.json()["detail"]


def test_a_refused_key_ends_the_workload_as_failed_and_does_not_echo_it(client, monkeypatch):
    admin = who("admin")
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_NOT_THE_RIGHT_ONE")
    client.put("/api/workloads/shop", headers=admin, json=SPEC)
    client.post("/api/workloads/shop/start", headers=admin)
    s = wait_for(client, admin, "shop", lambda s: s["state"] == "failed")
    assert "refused" in s["fatal"] and s["outcomes"].get("auth_error")
    blob = json.dumps(s) + client.get("/api/workloads/shop", headers=admin).text + client.get("/api/workloads", headers=admin).text
    assert "NOT_THE_RIGHT_ONE" not in blob and KEY not in blob


def test_the_global_ceiling_covers_all_running_workloads(client, monkeypatch):
    admin = who("admin")
    monkeypatch.setenv("CPSIM_MAX_RATE", "50")
    for n in ("a", "b"):
        assert client.put(f"/api/workloads/{n}", headers=admin, json={**SPEC, "schedule": {"interval": 1, "batch": 30}}).status_code == 200
    assert client.post("/api/workloads/a/start", headers=admin).status_code == 200
    r = client.post("/api/workloads/b/start", headers=admin)
    assert r.status_code == 422 and "CPSIM_MAX_RATE" in r.json()["detail"]
    client.post("/api/workloads/a/stop", headers=admin)
    assert client.post("/api/workloads/b/start", headers=admin).status_code == 200


def test_secrets_are_never_stored_in_the_database(client):
    admin = who("admin")
    client.put("/api/workloads/shop", headers=admin, json={**SPEC, "publishable_env": "PAYBROKER_PUBLISHABLE_KEY"})
    client.post("/api/workloads/shop/run-once", headers=admin)
    with db.connect() as c:
        stored = json.dumps([dict(r) for r in c.execute("SELECT name, spec FROM cpsim_workload")])
        everything = json.dumps([dict(r) for r in c.execute("SELECT key, value FROM app_setting")], default=str)
    assert KEY not in stored and "SENTINEL" not in stored and "PAYBROKER_SECRET_KEY" in stored
    assert KEY not in everything


def test_a_pagerlens_scenario_can_be_switched_while_running(client, monkeypatch):
    admin = who("admin")
    monkeypatch.setenv("PAGERLENS_TOKEN", "ing_x")
    seen = []

    def handler(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"ok": True})
    client.box["handler"] = handler
    client.put("/api/workloads/pl", headers=admin, json={"kind": "pagerlens", "target_url": "https://pl.example", "schedule": {"interval": 0.2, "batch": 1}})
    assert client.post("/api/workloads/pl/scenario", headers=admin, json={"scenario": "bad_deploy"}).status_code == 409        # not running
    client.post("/api/workloads/pl/start", headers=admin)
    wait_for(client, admin, "pl", lambda s: s["ticks"] >= 2)
    assert client.post("/api/workloads/pl/scenario", headers=who("viewer", sub="v"), json={"scenario": "bad_deploy"}).status_code == 403
    assert client.post("/api/workloads/pl/scenario", headers=admin, json={"scenario": "meltdown"}).status_code == 422
    assert client.post("/api/workloads/pl/scenario", headers=admin, json={"scenario": "bad_deploy"}).json()["extra"]["scenario"] == "bad_deploy"
    wait_for(client, admin, "pl", lambda s: s["extra"]["deploys"] >= 2)
    client.post("/api/workloads/pl/stop", headers=admin)
    assert any(p == "/api/ingest/services" for p, _ in seen) and any(p == "/api/ingest/deploys" and b["service"] == "sim-checkout" for p, b in seen)
    client.put("/api/workloads/shop", headers=admin, json=SPEC)
    client.post("/api/workloads/shop/start", headers=admin)
    assert client.post("/api/workloads/shop/scenario", headers=admin, json={"scenario": "healthy"}).status_code == 409          # paybroker has none


def test_a_workload_set_to_start_on_boot_resumes_after_a_restart(client):
    admin = who("admin")
    client.put("/api/workloads/shop", headers=admin, json={**SPEC, "schedule": {"interval": 0.2, "batch": 1, "start_on_boot": True}})
    client.put("/api/workloads/quiet", headers=admin, json=SPEC)
    assert client.get("/api/workloads/shop", headers=admin).json()["state"] == "idle"
    api.runtime.wl_runners.clear()
    with TestClient(api.app) as c2:                       # a fresh start of the app: the lifespan runs autostart
        s = wait_for(c2, admin, "shop", lambda s: s["state"] == "running" and s["ticks"] >= 1)
        assert s["ticks"] >= 1 and c2.get("/api/workloads/quiet/status", headers=admin).status_code == 404


def test_a_company_can_repeat_its_run_on_a_schedule(client):
    admin = who("admin")
    company = {"employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2, "sick_leave": 1}, "schedule": {"interval": 5, "runs": 2}}
    assert client.put("/api/companies/acme", headers=admin, json=company).status_code == 200
    got = client.get("/api/companies/acme", headers=admin).json()
    assert got["schedule"]["interval"] == 5.0 and got["schedule_run"] is None
    assert client.get("/api/companies", headers=admin).json()[0]["every"] == 5.0
    assert client.post("/api/companies/acme/schedule/start", headers=who("viewer", sub="v")).status_code == 403
    r = client.post("/api/companies/acme/schedule/start", headers=admin)
    assert r.status_code == 200 and r.json()["name"] == "company/acme" and r.json()["kind"] == "trustdesk"
    assert client.post("/api/companies/acme/schedule/start", headers=admin).status_code == 409
    assert client.put("/api/companies/acme", headers=admin, json=company).status_code == 409                 # stop the schedule first
    end = time.time() + 8
    while time.time() < end and not api.runtime.hr_runs.get("acme"):
        time.sleep(0.1)
    run = api.runtime.hr_runs["acme"]
    assert run is not None and client.get("/api/companies", headers=admin).json()[0]["scheduled"] is True
    live = client.get("/api/companies/acme", headers=admin).json()["schedule_run"]
    assert live["state"] == "running" and live["ticks"] >= 1
    assert client.post("/api/companies/acme/schedule/stop", headers=admin).json() == {"stopped": True}
    assert client.put("/api/companies/acme", headers=admin, json=company).status_code == 200
    nosched = {k: v for k, v in company.items() if k != "schedule"}
    client.put("/api/companies/plain", headers=admin, json=nosched)
    assert client.post("/api/companies/plain/schedule/start", headers=admin).status_code == 422             # it has no schedule
    assert client.put("/api/companies/bad", headers=admin, json={**company, "schedule": {"interval": 1}}).status_code == 422
