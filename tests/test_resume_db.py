"""Resume after a restart over the web API with Postgres: a start is remembered, a person's stop is too, the app comes back up running what was
running (once, after the start-on-boot items), the Settings switch decides, and a broken item never stops the app from coming up."""
import time

import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from fastapi.testclient import TestClient
from test_federation_db import AUD, ISS, Provider, access

from cpsim import api, core, hr_engine

CITY = {"groups": [{"type": "temperature", "count": 2, "protocol": "direct", "frequency": 5}]}
WL = {"kind": "paybroker", "schedule": {"interval": 0.2, "batch": 1}}
CO = {"employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}, "schedule": {"interval": 30}}


@pytest.fixture
def client(monkeypatch, db):
    for v in ("CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_METRICS_TOKEN", "CPSIM_RESUME_ON_START"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CPSIM_RESUME_STAGGER_SECONDS", "0")
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_x")
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    api.runtime.runners.clear()
    with TestClient(api.app) as c:
        yield c


def who(*roles, **kw):
    return access(sub=kw.pop("sub", "usr_1"), roles=[f"cpsim:{r}" for r in roles], **kw)


def restart():
    """What `ecoctl recreate` does to the container: everything in memory is gone; the database stays."""
    api.runtime.runners.clear()
    api.runtime.hr_runs.clear()
    api.runtime.wl_runners.clear()
    api.runtime.company_schedules.clear()
    return TestClient(api.app)


def until(cond, seconds=8):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_start_is_remembered_a_stop_by_a_person_is_too(client, monkeypatch):
    admin = who("admin")
    client.put("/api/cities/metro", headers=admin, json=CITY)
    assert client.get("/api/cities/metro", headers=admin).json()["will_resume"] is False
    client.post("/api/cities/metro/start", headers=admin)
    assert client.get("/api/cities/metro", headers=admin).json()["will_resume"] is True
    assert client.get("/api/cities", headers=admin).json()[0]["will_resume"] is True
    assert api.store.desired("city") == {"metro"}
    client.post("/api/cities/metro/stop", headers=admin)
    assert api.store.desired("city") == set() and client.get("/api/cities/metro", headers=admin).json()["will_resume"] is False

    client.put("/api/workloads/shop", headers=admin, json=WL)
    client.post("/api/workloads/shop/start", headers=admin)
    assert client.get("/api/workloads/shop", headers=admin).json()["will_resume"] is True
    client.post("/api/workloads/shop/stop", headers=admin)
    assert api.store.desired("workload") == set()

    client.put("/api/companies/acme", headers=admin, json=CO)
    client.post("/api/companies/acme/schedule/start", headers=admin)
    got = client.get("/api/companies/acme", headers=admin).json()
    assert got["schedule_will_resume"] is True and got["will_resume"] is False
    client.post("/api/companies/acme/schedule/stop", headers=admin)
    assert api.store.desired("schedule") == set()
    async def stays_running(self):                  # a real run with no target ends at once and clears its own flag: keep it going until stopped
        await self._stop.wait()
        self.state = "stopped"
    monkeypatch.setattr(hr_engine.HrRun, "_main", stays_running)
    started = client.post("/api/companies/acme/run", headers=admin)
    assert started.status_code == 200, started.text
    assert api.store.desired("company") == {"acme"}
    client.post("/api/companies/acme/stop", headers=admin)
    assert api.store.desired("company") == set()


def test_a_restart_brings_back_what_was_running_and_only_that(client):
    admin = who("admin")
    for n in ("on", "off"):
        client.put(f"/api/cities/{n}", headers=admin, json=CITY)
    client.put("/api/workloads/shop", headers=admin, json=WL)
    client.put("/api/companies/acme", headers=admin, json=CO)
    for path in ("cities/on/start", "workloads/shop/start", "companies/acme/schedule/start", "cities/off/start", "cities/off/stop"):
        assert client.post(f"/api/{path}", headers=admin).status_code == 200
    with restart() as c2:                           # the lifespan ends by stopping everything: that must not count as a stop by a person
        until(lambda: set(api.runtime.runners) == {"on"} and api.runtime.wl_runners.get("shop") and api.runtime.company_schedules.get("acme"))
        assert api.runtime.wl_runners["shop"].running and api.runtime.company_schedules["acme"].running
        assert c2.get("/api/cities", headers=admin).json()[0]["running"] in (True, False)
    assert api.store.desired("city") == {"on"} and api.store.desired("workload") == {"shop"} and api.store.desired("schedule") == {"acme"}


def test_a_stop_before_the_restart_stays_stopped_and_the_switch_can_turn_resume_off(client):
    admin = who("admin")
    client.put("/api/cities/metro", headers=admin, json=CITY)
    client.post("/api/cities/metro/start", headers=admin)
    assert client.get("/api/run-settings", headers=admin).json() == {"resume_on_start": True, "default": True, "stagger_seconds": 0.0}
    assert client.put("/api/run-settings", headers=who("operator", sub="o"), json={"resume_on_start": False}).status_code == 403
    assert client.get("/api/run-settings", headers=who("viewer", sub="v")).status_code == 403
    assert client.put("/api/run-settings", headers=admin, json={"resume_on_start": "maybe"}).status_code == 422
    assert client.put("/api/run-settings", headers=admin, json={"resume_on_start": False}).json()["resume_on_start"] is False
    assert client.get("/api/cities/metro", headers=admin).json()["will_resume"] is False        # the note follows the switch
    with restart() as c2:
        time.sleep(0.5)
        assert api.runtime.runners == {}
        c2.put("/api/run-settings", headers=admin, json={"resume_on_start": True})
    with restart():
        until(lambda: "metro" in api.runtime.runners)                           # the flag was kept while the switch was off


def test_a_city_set_to_start_on_boot_is_started_once_and_an_unresumable_item_does_not_stop_the_app(client, capsys):
    admin = who("admin")
    client.put("/api/cities/boot", headers=admin, json={**CITY, "autostart": True})
    client.put("/api/cities/broken", headers=admin, json={**CITY, "target_url": "${CITYPULSE_URL}"})
    client.put("/api/cities/fine", headers=admin, json=CITY)
    client.post("/api/cities/fine/start", headers=admin)
    api.store.set_desired("city", "boot", True)
    api.store.set_desired("city", "broken", True)                               # its fleet address is not set on this app
    with restart():
        until(lambda: set(api.runtime.runners) == {"boot", "fine"})
        first = api.runtime.runners["boot"]
        time.sleep(0.3)
        assert api.runtime.runners["boot"] is first                              # started by start-on-boot, not started again by the resume
    out = capsys.readouterr().out
    assert out.count("cannot resume city broken") == 1 and "CITYPULSE_URL" in out
    assert api.store.desired("city") == {"boot", "broken", "fine"}               # the failed one stays flagged


def test_the_resume_note_in_a_city_is_read_only_when_the_page_saves_it_back(client):
    admin = who("admin")
    client.put("/api/cities/metro", headers=admin, json=CITY)
    echo = {k: v for k, v in client.get("/api/cities/metro", headers=admin).json().items() if k != "running"}        # what the page does
    assert "will_resume" in echo and client.put("/api/cities/metro", headers=admin, json=echo).status_code == 200
