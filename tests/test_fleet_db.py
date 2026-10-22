"""Fleet references over the web API (needs TEST_DATABASE_URL): stored as written, shown with their address, resolved when a run starts."""
from test_workloads_db import client, who  # noqa: F401  (the app on a mock PayBroker, and role tokens)

from cpsim.fleet import FLEET_VARS

REF = {"kind": "paybroker", "target_url": "${PAYBROKER_URL}", "schedule": {"interval": 0.2, "batch": 2}}
CITY = {"target_url": "${CITYPULSE_URL}", "groups": [{"type": "temperature", "count": 4}]}
COMPANY = {"target_url": "${TRUSTDESK_URL}", "employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}}


def test_references_are_validated_stored_as_written_and_shown_with_their_address(client, monkeypatch):  # noqa: F811
    admin = who("admin")
    monkeypatch.setenv("PAYBROKER_URL", "https://pb-1.example")
    assert client.put("/api/workloads/shop", headers=admin, json=REF).status_code == 200
    got = client.get("/api/workloads/shop", headers=admin).json()
    assert got["target_url"] == "${PAYBROKER_URL}"
    assert got["target"] == {"target_url": "${PAYBROKER_URL}", "reference": "PAYBROKER_URL", "resolved_url": "https://pb-1.example", "resolves": True, "problem": ""}
    assert client.get("/api/workloads", headers=admin).json()[0]["target"]["resolved_url"] == "https://pb-1.example"
    # what a GET returns can be PUT back (the `target` echo is ignored), and the reference is still what is stored
    assert client.put("/api/workloads/shop", headers=admin, json={k: v for k, v in got.items() if k != "state"}).status_code == 200
    assert client.get("/api/workloads/shop", headers=admin).json()["target_url"] == "${PAYBROKER_URL}"
    monkeypatch.setenv("PAYBROKER_URL", "https://pb-2.example")                       # recreated: the next read shows the new address
    assert client.get("/api/workloads/shop", headers=admin).json()["target"]["resolved_url"] == "https://pb-2.example"
    monkeypatch.delenv("PAYBROKER_URL")
    t = client.get("/api/workloads/shop", headers=admin).json()["target"]
    assert t["resolves"] is False and t["resolved_url"] == "" and "PAYBROKER_URL is not set on this app" in t["problem"]
    for bad in ("${HOME}", "https://${PAYBROKER_URL}", "${PAYBROKER_URL}/v1", "${${PAYBROKER_URL}}"):
        r = client.put("/api/workloads/shop", headers=admin, json={**REF, "target_url": bad})
        assert r.status_code == 422 and "fleet reference" in r.json()["detail"], bad
    assert client.get("/api/workloads/shop", headers=admin).json()["target_url"] == "${PAYBROKER_URL}"


def test_cities_and_companies_accept_references_too(client, monkeypatch):  # noqa: F811
    admin = who("admin")
    monkeypatch.setenv("CITYPULSE_URL", "https://cp.example")
    assert client.put("/api/cities/metro", headers=admin, json=CITY).status_code == 200
    assert client.put("/api/companies/acme", headers=admin, json=COMPANY).status_code == 200
    assert client.put("/api/cities/bad", headers=admin, json={**CITY, "target_url": "${DATABASE_URL}"}).status_code == 422
    assert client.put("/api/companies/bad", headers=admin, json={**COMPANY, "target_url": "x${TRUSTDESK_URL}"}).status_code == 422
    city = client.get("/api/cities/metro", headers=admin).json()
    assert city["target_url"] == "${CITYPULSE_URL}" and city["target"]["resolved_url"] == "https://cp.example" and city["target"]["resolves"]
    assert client.put("/api/cities/metro", headers=admin, json={k: v for k, v in city.items() if k != "running"}).status_code == 200                # round trip
    assert client.get("/api/cities", headers=admin).json()[0]["target"]["reference"] == "CITYPULSE_URL"
    co = client.get("/api/companies/acme", headers=admin).json()
    assert co["target_url"] == "${TRUSTDESK_URL}" and co["target"]["resolves"] is False       # TRUSTDESK_URL is not set here
    assert client.put("/api/companies/acme", headers=admin, json={k: v for k, v in co.items() if k not in ("state", "schedule_run")}).status_code == 200
    row = client.get("/api/companies", headers=admin).json()[0]
    assert row["target"]["reference"] == "TRUSTDESK_URL" and row["dry_run"] is False          # a reference is a target, not generate-only


def test_a_start_resolves_the_reference_now_and_fails_clearly_when_it_cannot(client, monkeypatch):  # noqa: F811
    admin = who("admin")
    client.put("/api/workloads/shop", headers=admin, json=REF)
    r = client.post("/api/workloads/shop/run-once", headers=admin)
    assert r.status_code == 422 and r.json()["detail"] == "PAYBROKER_URL is not set on this app; set the URL by hand or deploy it with ecoctl"
    assert client.post("/api/workloads/shop/start", headers=admin).status_code == 422
    monkeypatch.setenv("PAYBROKER_URL", "http://pb.example")                          # plain http to another host: refused, nothing sent
    r = client.post("/api/workloads/shop/run-once", headers=admin)
    assert r.status_code == 422 and "PAYBROKER_URL" in r.json()["detail"] and "https" in r.json()["detail"]
    monkeypatch.setenv("PAYBROKER_URL", "https://pb.example")
    r = client.post("/api/workloads/shop/run-once", headers=admin)
    assert r.status_code == 200 and r.json()["items"] >= 1
    assert client.get("/api/workloads/shop", headers=admin).json()["target_url"] == "${PAYBROKER_URL}"       # never resolved into the database
    assert client.post("/api/cities/ghost/start", headers=admin).status_code == 404
    client.put("/api/cities/metro", headers=admin, json=CITY)
    r = client.post("/api/cities/metro/start", headers=admin)
    assert r.status_code == 422 and "CITYPULSE_URL is not set on this app" in r.json()["detail"]


def test_fleet_card_catalogs_and_the_generator_default(client, monkeypatch):  # noqa: F811
    admin, viewer = who("admin"), who("viewer", sub="v")
    monkeypatch.setenv("CITYPULSE_URL", "https://cp.example")
    monkeypatch.setenv("LLMGW_URL", "https://gw.example")
    for h in (admin, viewer):
        vars_ = client.get("/api/fleet", headers=h).json()["vars"]
        assert [v["name"] for v in vars_] == list(FLEET_VARS)
        assert {v["name"]: v["value"] for v in vars_ if v["set"]} == {"CITYPULSE_URL": "https://cp.example", "LLMGW_URL": "https://gw.example"}
    assert client.get("/api/fleet", headers=who("other", sub="s")).status_code == 403
    assert client.get("/api/catalog", headers=admin).json()["defaults"] == {"city_target_url": "${CITYPULSE_URL}"}
    assert [v["name"] for v in client.get("/api/hr/catalog", headers=admin).json()["fleet"]] == list(FLEET_VARS)
    wc = client.get("/api/workloads/catalog", headers=admin).json()
    assert wc["kind_vars"] == {"paybroker": "PAYBROKER_URL", "llmgw": "LLMGW_URL", "pagerlens": "PAGERLENS_URL", "trustdesk": "TRUSTDESK_URL",
                               "identity": "IDENTITY_URL"} and len(wc["fleet"]) == 6
    made = client.post("/api/cities/generate", headers=admin, json={"count": 2, "size": "small", "seed": 4}).json()
    for c in made["cities"]:
        assert client.get(f"/api/cities/{c['name']}", headers=admin).json()["target_url"] == "${CITYPULSE_URL}"
    explicit = client.post("/api/cities/generate", headers=admin, json={"size": "small", "seed": 9, "target_url": ""}).json()
    assert client.get(f"/api/cities/{explicit['cities'][0]['name']}", headers=admin).json()["target_url"] == ""
    assert client.post("/api/cities/generate", headers=admin, json={"size": "small", "target_url": "${HOME}"}).status_code == 422
    monkeypatch.delenv("CITYPULSE_URL")
    made = client.post("/api/cities/generate", headers=admin, json={"size": "small", "seed": 11}).json()
    assert client.get(f"/api/cities/{made['cities'][0]['name']}", headers=admin).json()["target_url"] == ""
    assert client.get("/api/catalog", headers=admin).json()["defaults"] == {"city_target_url": ""}
