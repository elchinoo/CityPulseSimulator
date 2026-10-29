"""POST /api/cities/generate: what each role may do and what it creates (needs TEST_DATABASE_URL)."""
from test_federation_db import access, client  # noqa: F401  (the provider, role tokens and the app client)

from cpsim.catalog import DISTRICT_ID
from cpsim.districts import pretty

CITY = {"groups": [{"type": "temperature", "count": 8}]}


def test_roles_decide_who_may_generate(client):  # noqa: F811
    operator, viewer = access(roles=["cpsim:operator"]), access(sub="usr_2", roles=["cpsim:viewer"])
    admin, stranger = access(sub="usr_3", roles=["cpsim:admin"]), access(sub="usr_4", roles=["otherapp:admin"])
    assert client.post("/api/cities/generate", headers=viewer, json={"count": 2}).status_code == 403
    assert client.post("/api/cities/generate", headers=stranger, json={"count": 2}).status_code == 403
    assert client.post("/api/cities/generate", json={"count": 2}).status_code in (401, 403)
    assert client.get("/api/cities", headers=viewer).json() == []
    assert client.post("/api/cities/generate", headers=operator, json={"count": 2, "size": "small", "seed": 1}).status_code == 200
    assert client.post("/api/cities/generate", headers=admin, json={"count": 1, "size": "small", "seed": 90}).status_code == 200
    assert len(client.get("/api/cities", headers=viewer).json()) == 3                # a viewer sees them but cannot make more


def test_generate_creates_varied_cities_and_reports_them(client):  # noqa: F811
    op = access(roles=["cpsim:operator"])
    r = client.post("/api/cities/generate", headers=op, json={"count": 10, "size": "medium", "profile": "mixed", "seed": 5})
    assert r.status_code == 200
    out = r.json()
    assert out["created"] == 10 and len(out["cities"]) == 10 and out["sensors"] == sum(c["sensors"] for c in out["cities"])
    for c in out["cities"]:
        assert {"name", "seed", "size", "profile", "sensors", "groups", "types", "protocols", "districts"} <= set(c)
        assert all({"id", "name", "kind", "sensors", "types"} <= set(d) for d in c["districts"])
        assert sum(d["sensors"] for d in c["districts"]) == c["sensors"]
    assert len({c["name"] for c in out["cities"]}) == 10
    assert len({tuple(d["id"] for d in c["districts"]) for c in out["cities"]}) == 10
    names = [d["id"] for c in out["cities"] for d in c["districts"]]
    assert len(set(names)) >= 0.9 * len(names)                                         # each city has its own district names
    kinds = cat_kinds(client, op)
    for c in out["cities"]:
        for d in c["districts"]:
            assert DISTRICT_ID.match(d["id"]) and d["kind"] in kinds and d["name"] == (pretty(d["id"]) if c["name"] not in ("brussels", "vienna") else d["name"])
    saved = {c["name"]: c for c in client.get("/api/cities", headers=op).json()}
    assert set(saved) == {c["name"] for c in out["cities"]} and all(saved[c["name"]]["sensors"] == c["sensors"] for c in out["cities"])
    one = client.get(f"/api/cities/{out['cities'][0]['name']}", headers=op).json()
    assert one["seed"] == 5 and one["autostart"] is False and one["target_url"] == "" and one["token_env"] == "CITYPULSE_INGEST_TOKEN"
    again = client.post("/api/cities/generate", headers=op, json={"count": 10, "size": "medium", "profile": "mixed", "seed": 5}).json()
    assert not {c["name"] for c in again["cities"]} & set(saved)                      # never replaces an existing city
    assert [c["name"] for c in out["cities"]][:2] == ["brussels", "vienna"], "real names first"
    assert [c["districts"] for c in again["cities"]][2:] == [c["districts"] for c in out["cities"]][2:]   # same seeds, same cities (the real names are taken the second time)


def cat_kinds(client, op):  # noqa: F811
    return client.get("/api/catalog", headers=op).json()["generator"]["district_kinds"]


def test_generate_options_and_refusals(client):  # noqa: F811
    op = access(roles=["cpsim:operator"])
    ok = client.post("/api/cities/generate", headers=op, json={"name": "demo", "count": 2, "size": 100, "profile": "tourist",
                                                              "target_url": "https://citypulse.example", "autostart": True}).json()
    assert [c["name"] for c in ok["cities"]] == ["demo-1", "demo-2"] and all(c["profile"] == "tourist" for c in ok["cities"])
    got = client.get("/api/cities/demo-1", headers=op).json()
    assert got["target_url"] == "https://citypulse.example" and got["autostart"] is True
    assert client.post("/api/cities/generate", headers=op, json={"name": "solo", "size": "small"}).json()["cities"][0]["name"] == "solo"
    assert client.post("/api/cities/generate", headers=op, json={"name": "solo"}).status_code == 409
    for bad in ({"count": 0}, {"count": 51}, {"size": "huge"}, {"profile": "lunar"}, {"name": "Bad Name"}, {"seed": -1},
                {"target_url": "http://evil.example"}, {"token_env": "lowercase"}, {"size": 5}):
        assert client.post("/api/cities/generate", headers=op, json=bad).status_code == 422, bad
    assert len(client.get("/api/cities", headers=op).json()) == 3                     # refusals created nothing
    cat = client.get("/api/catalog", headers=op).json()
    assert "mixed" in cat["generator"]["profiles"] and cat["generator"]["max_count"] == 50 and cat["districts"][0]["kind"] == "harbour"
    assert len(cat["generator"]["district_kinds"]) == 12 and cat["generator"]["district_names"] >= 3000


def test_generated_cities_can_start_and_are_not_changed_by_the_fixed_presets(client):  # noqa: F811
    op = access(roles=["cpsim:operator"])
    name = client.post("/api/cities/generate", headers=op, json={"size": "small", "seed": 3, "count": 1}).json()["cities"][0]["name"]
    assert client.put("/api/cities/fixed", headers=op, json=CITY).status_code == 200
    started = client.post(f"/api/cities/{name}/start", headers=op)
    assert started.status_code == 200
    assert client.post(f"/api/cities/{name}/stop", headers=op).json() == {"stopped": True}
