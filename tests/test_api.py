"""The web API: setup, sessions, CSRF, city CRUD with validation, run control and metrics."""
import pytest
from fastapi.testclient import TestClient

from cpsim import api, auth

H = {"X-CPSim-CSRF": "1"}
PASSWORD = "correct-horse-battery"  # pragma: allowlist secret
CITY = {"groups": [{"type": "temperature", "count": 8, "protocol": "modbus_tcp", "frequency": 5, "burst_pct": 10}]}


@pytest.fixture
def client(monkeypatch):
    api.store.delete("admin_password_hash")
    for name in list(api.store.cities()):
        api.store.delete_city(name)
    auth._setup_code, auth._sessions.clear(), api._failures.clear()
    with TestClient(api.app) as c:
        yield c


def login(c):
    assert c.post("/api/setup", json={"code": auth.setup_code(), "password": PASSWORD}, headers=H).status_code == 200


def test_everything_is_denied_before_setup_and_setup_needs_the_code(client):
    assert client.get("/api/cities").status_code == 401
    assert client.get("/api/session").json()["state"] == "setup_required"
    assert client.post("/api/setup", json={"code": "AAAA-BBBB-CCCC", "password": PASSWORD}, headers=H).status_code == 403
    assert client.post("/api/setup", json={"code": auth.setup_code(), "password": "short"}, headers=H).status_code == 422
    login(client)
    assert client.get("/api/cities").json() == []
    assert client.post("/api/setup", json={"code": auth.setup_code(), "password": PASSWORD}, headers=H).status_code == 403


def test_login_logout_and_csrf(client):
    login(client)
    client.post("/api/logout", headers=H)
    assert client.get("/api/cities").status_code == 401
    assert client.post("/api/login", json={"password": "wrong-wrong-wrong"}, headers=H).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    assert client.put("/api/cities/x", json=CITY).status_code == 403                    # cookie write without the header
    assert client.put("/api/cities/x", json=CITY, headers=H).status_code == 200


def test_failed_logins_are_rate_limited(client):
    login(client)
    client.post("/api/logout", headers=H)
    codes = [client.post("/api/login", json={"password": "wrong-wrong-wrong"}, headers=H).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_city_crud_and_validation(client):
    login(client)
    assert client.put("/api/cities/metropolis", json=CITY, headers=H).json() == {"ok": True, "sensors": 8}
    assert client.get("/api/cities/metropolis").json()["groups"][0]["protocol"] == "modbus_tcp"
    listing = client.get("/api/cities").json()
    assert listing[0]["name"] == "metropolis" and listing[0]["running"] is False and listing[0]["protocols"] == ["modbus_tcp"]
    for bad in ({"groups": [{"type": "x", "count": 1}]}, {"token_env": "abc123secretvalue"}, {"target_url": "http://evil.example"},
                {"groups": [{"type": "rain", "count": 1, "frequency": 0}]}):
        r = client.put("/api/cities/metropolis", json=bad, headers=H)
        assert r.status_code == 422, bad
    assert client.put("/api/cities/Bad_Name", json=CITY, headers=H).status_code == 422
    assert client.get("/api/cities/ghost").status_code == 404
    assert client.delete("/api/cities/metropolis", headers=H).status_code == 200
    assert client.get("/api/cities").json() == []


def test_run_a_city_boost_it_and_stop_it(client):
    login(client)
    client.put("/api/cities/metropolis", json={**CITY, "poll_interval": 0.5}, headers=H)
    assert client.post("/api/cities/metropolis/burst", json={"seconds": 30}, headers=H).status_code == 409   # not running yet
    started = client.post("/api/cities/metropolis/start", headers=H)
    assert started.status_code == 200 and started.json()["endpoints"]["modbus_tcp"]["port"] > 0
    assert client.put("/api/cities/metropolis", json=CITY, headers=H).status_code == 409                    # stop it first
    assert client.post("/api/cities/metropolis/burst", json={"seconds": 30}, headers=H).json()["boost"] > 0
    assert client.get("/api/cities/metropolis/stats").json()["sensors"] == 8
    assert client.get("/api/cities").json()[0]["running"] is True
    assert client.post("/api/cities/metropolis/stop", headers=H).json() == {"stopped": True}
    assert client.get("/api/cities/metropolis/stats").status_code == 404
    assert client.post("/api/cities/ghost/start", headers=H).status_code == 404
    client.put("/api/cities/empty", json={}, headers=H)
    assert client.post("/api/cities/empty/start", headers=H).status_code == 422                               # no sensors


def test_a_city_that_needs_a_token_will_not_start_without_it(client, monkeypatch):
    monkeypatch.delenv("CITYPULSE_INGEST_TOKEN", raising=False)
    login(client)
    client.put("/api/cities/metropolis", json={**CITY, "target_url": "https://citypulse.example"}, headers=H)
    r = client.post("/api/cities/metropolis/start", headers=H)
    assert r.status_code == 422 and "CITYPULSE_INGEST_TOKEN" in r.json()["detail"]


def test_metrics_need_their_own_token_and_use_the_shared_names(client, monkeypatch):
    monkeypatch.setenv("CPSIM_METRICS_TOKEN", "m" * 40)  # pragma: allowlist secret
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer " + "m" * 40})
    assert ok.status_code == 200
    for name in ("http_requests_total", "http_request_duration_seconds", 'db_up{app="cpsim"} 1.0', "app_info{", "cpsim_cities_running"):
        assert name in ok.text, name
    login(client)
    assert client.get("/metrics").status_code == 401                                          # a browser session is not a scraper
    assert client.get("/api/cities", headers={"Authorization": "Bearer " + "m" * 40}).status_code == 401   # nor the reverse


def test_catalog_and_health(client):
    assert client.get("/healthz").json()["status"] == "ok"
    login(client)
    cat = client.get("/api/catalog").json()
    assert len(cat["types"]) >= 20 and "profibus" in cat["protocols"] and "direct" in cat["protocols"]
    assert cat["limits"]["channels_per_device"] == 32
