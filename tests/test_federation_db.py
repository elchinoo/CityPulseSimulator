"""CityPulse Simulator federated: people and services authenticate with the ecosystem identity provider, and roles decide what they
may do. Also the standalone credentials' boundaries (static key, metrics token, API tokens) after the move to the kit."""
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey

from cpsim import api, core
from cpsim.core import db

ISS, AUD = "https://id.example.org", "cpsim"
KEY = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
CSRF = {"X-CPSim-CSRF": "1"}
CITY = {"groups": [{"type": "temperature", "count": 4, "protocol": "modbus_tcp", "frequency": 5, "burst_pct": 10}]}


class Provider:
    def __init__(self):
        self.nonce = None
        self.id_claims = {"sub": "usr_1", "name": "Ada", "email": "ada@example.org", "roles": ["cpsim:operator"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "jwks_uri": f"{ISS}/jwks", "authorization_endpoint": f"{ISS}/oauth/authorize",
                                             "token_endpoint": f"{ISS}/oauth/token", "end_session_endpoint": f"{ISS}/oauth/logout"})
        if path == "/jwks":
            return httpx.Response(200, json={"keys": [KEY.as_dict(private=False)]})
        if path == "/oauth/token":
            claims = {"iss": ISS, "aud": AUD, "exp": int(time.time()) + 300, "nonce": self.nonce, **self.id_claims}
            return httpx.Response(200, json={"id_token": jwt.encode({"alg": "RS256", "kid": "k1"}, claims, KEY), "access_token": "x"})
        return httpx.Response(404)

    def http(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler), follow_redirects=False)


def access(**claims) -> dict:
    base = {"iss": ISS, "aud": AUD, "sub": "usr_1", "exp": int(time.time()) + 600, "sub_type": "user"}
    base.update(claims)
    return {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1", "typ": "at+jwt"}, base, KEY)}


@pytest.fixture
def client(monkeypatch, db):
    for v in ("CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_METRICS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    with TestClient(api.app) as c:
        c.provider = provider
        yield c


def test_roles_decide_what_a_person_may_do(client):
    admin, operator = access(sub="usr_3", roles=["cpsim:admin"]), access(roles=["cpsim:operator"])
    viewer, stranger = access(sub="usr_2", roles=["cpsim:viewer"]), access(sub="usr_4", roles=["otherapp:admin"])
    assert client.put("/api/cities/metropolis", headers=operator, json=CITY).status_code == 200
    assert client.put("/api/cities/metropolis", headers=viewer, json=CITY).status_code == 403
    assert client.put("/api/cities/metropolis", headers=stranger, json=CITY).status_code == 403          # signed in, but no role in this app
    for who in (operator, viewer, admin):
        assert client.get("/api/cities", headers=who).status_code == 200
        assert client.get("/api/catalog", headers=who).status_code == 200
    assert client.get("/api/cities", headers=stranger).status_code == 403
    assert client.post("/api/cities/metropolis/stop", headers=operator).status_code == 200               # sim:run
    assert client.post("/api/cities/metropolis/stop", headers=viewer).status_code == 403
    assert client.put("/api/companies/acme", headers=operator, json={"employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}}).status_code == 200
    assert client.post("/api/companies/acme/preview", headers=viewer).status_code == 200                 # read-only view of the plan
    assert client.post("/api/companies/acme/run", headers=viewer).status_code == 403
    assert client.delete("/api/companies/acme", headers=viewer).status_code == 403
    assert client.delete("/api/companies/acme", headers=operator).status_code == 200
    for who in (operator, viewer):                                                                        # settings:manage only
        assert client.get("/api/tokens", headers=who).status_code == 403
        assert client.post("/api/admin-password", headers=who, json={"new": "another-long-password"}).status_code == 403
        assert client.get("/metrics", headers=who).status_code == 403
    assert client.get("/api/tokens", headers=admin).status_code == 200
    assert client.delete("/api/cities/metropolis", headers=admin).status_code == 200


def test_a_service_token_carries_only_its_scope(client):
    reader = access(sub="dash", sub_type="service", scope="cpsim.read", client_id="dash")
    scraper = access(sub="prom", sub_type="service", scope="cpsim.metrics", client_id="prom")
    assert client.get("/api/cities", headers=reader).status_code == 200
    assert client.put("/api/cities/metropolis", headers=reader, json=CITY).status_code == 403
    assert client.get("/metrics", headers=reader).status_code == 403
    assert client.get("/metrics", headers=scraper).status_code == 200
    assert client.get("/api/cities", headers=scraper).status_code == 403
    assert client.get("/api/cities", headers=access(sub="x", sub_type="service", scope="trustdesk.requests.write")).status_code == 403


def test_tokens_for_another_app_or_with_a_bad_signature_are_refused(client):
    assert client.get("/api/cities", headers=access(aud="some-other-app", roles=["cpsim:admin"])).status_code == 401
    assert client.get("/api/cities", headers=access(exp=int(time.time()) - 3600, roles=["cpsim:admin"])).status_code == 401
    assert client.get("/api/cities", headers=access(iss="https://evil.example.org", roles=["cpsim:admin"])).status_code == 401
    other = RSAKey.generate_key(2048, {"kid": "k1", "use": "sig", "alg": "RS256"})
    forged = {"Authorization": "Bearer " + jwt.encode({"alg": "RS256", "kid": "k1"}, {"iss": ISS, "aud": AUD, "sub": "x", "exp": int(time.time()) + 60,
                                                                                       "roles": ["cpsim:admin"]}, other)}
    assert client.get("/api/cities", headers=forged).status_code == 401


def _sign_in(client):
    r = client.get("/auth/login", params={"next": "/#companies"}, follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    client.provider.nonce = q["nonce"][0]
    return r, q, client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)


def test_single_sign_on_login_creates_a_role_bound_session(client):
    r, q, cb = _sign_in(client)
    assert r.status_code == 303 and r.headers["location"].startswith(f"{ISS}/oauth/authorize?")
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/auth/callback"] and q["client_id"] == [AUD]
    assert cb.status_code == 303 and cb.headers["location"] == "/#companies" and "cs_session" in cb.cookies
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["name"] == "Ada" and s["user"]["roles"] == ["cpsim:operator"] and s["user"]["source"] == "oidc"
    assert "sim:run" in s["user"]["permissions"] and "settings:manage" not in s["user"]["permissions"]
    assert s["sso"]["login_url"] == "/auth/login" and s["sso"]["issuer"] == ISS and s["state"] != "setup_required"
    assert client.get("/api/cities").status_code == 200
    assert client.put("/api/cities/metropolis", json=CITY, headers=CSRF).status_code == 200
    assert client.get("/api/tokens").status_code == 403                                               # an operator is not an administrator
    assert client.get("/metrics").status_code == 401                                                  # a browser session is never a scraper
    out = client.post("/api/logout", headers=CSRF).json()
    assert out["logout_url"].startswith(f"{ISS}/oauth/logout?") and "id_token_hint=" in out["logout_url"]
    assert client.get("/api/cities").status_code == 401


def test_a_viewer_session_is_read_only(client):
    client.provider.id_claims = {"sub": "usr_2", "name": "Vic", "roles": ["cpsim:viewer"]}
    _sign_in(client)
    assert client.get("/api/cities").status_code == 200
    assert client.put("/api/cities/metropolis", json=CITY, headers=CSRF).status_code == 403
    assert client.get("/api/session").json()["user"]["permissions"] == ["sim:read"]


def test_sso_refuses_people_without_a_role_here_and_replayed_or_foreign_logins(client):
    client.provider.id_claims = {"sub": "usr_9", "name": "Nobody", "roles": ["otherapp:admin"]}
    r, q, cb = _sign_in(client)
    assert cb.headers["location"].startswith("/?sso_error=") and "cs_session" not in cb.cookies
    assert client.get("/api/cities").status_code == 401
    again = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]}, follow_redirects=False)
    assert again.headers["location"].startswith("/?sso_error=")                                       # the state was single-use
    assert client.get("/auth/callback", params={"error": "access_denied"}, follow_redirects=False).headers["location"].startswith("/?sso_error=")


def test_sso_endpoints_are_404_when_federation_is_off(client, monkeypatch):
    monkeypatch.setattr(api, "fed", Federation("", AUD, None, None, True))
    assert client.get("/auth/login", follow_redirects=False).status_code == 404
    assert client.get("/auth/callback", follow_redirects=False).status_code == 404
    s = client.get("/api/session").json()
    assert s["sso"] is None and s["state"] == "setup_required"                                        # standalone: the setup flow is offered


# ---- standalone credentials keep exactly the access they had -----------------------------------------------------
def test_the_static_key_is_full_and_the_metrics_token_only_scrapes(client, monkeypatch):
    monkeypatch.setenv("CPSIM_API_KEY", "k" * 40)       # pragma: allowlist secret
    monkeypatch.setenv("CPSIM_METRICS_TOKEN", "m" * 40)  # pragma: allowlist secret
    key, mtok = {"Authorization": "Bearer " + "k" * 40}, {"Authorization": "Bearer " + "m" * 40}
    assert client.put("/api/cities/metropolis", headers=key, json=CITY).status_code == 200
    assert client.get("/api/tokens", headers=key).status_code == 200
    assert client.get("/metrics", headers=key).status_code == 200
    assert client.get("/metrics", headers=mtok).status_code == 200
    assert client.get("/api/cities", headers=mtok).status_code == 403
    assert client.put("/api/cities/metropolis", headers=mtok, json=CITY).status_code == 403
    assert client.get("/api/cities", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_local_api_tokens_keep_their_scope(client, monkeypatch):
    monkeypatch.setenv("CPSIM_API_KEY", "k" * 40)       # pragma: allowlist secret
    admin = {"Authorization": "Bearer " + "k" * 40}
    made = {s: client.post("/api/tokens", headers=admin, json={"name": s, "scope": s}).json() for s in ("read", "metrics", "full")}
    assert all(t["token"].startswith("cs_") for t in made.values())
    bearer = {s: {"Authorization": "Bearer " + t["token"]} for s, t in made.items()}
    assert client.get("/api/cities", headers=bearer["read"]).status_code == 200
    assert client.put("/api/cities/metropolis", headers=bearer["read"], json=CITY).status_code == 403
    assert client.get("/metrics", headers=bearer["read"]).status_code == 403
    assert client.get("/metrics", headers=bearer["metrics"]).status_code == 200
    assert client.get("/api/cities", headers=bearer["metrics"]).status_code == 403
    assert client.put("/api/cities/metropolis", headers=bearer["full"], json=CITY).status_code == 200
    assert client.post("/api/tokens", headers=bearer["read"], json={"name": "x", "scope": "full"}).status_code == 403
    assert client.delete(f"/api/tokens/{made['read']['id']}", headers=admin).status_code == 200
    assert client.get("/api/cities", headers=bearer["read"]).status_code == 401                       # revoked
    assert client.post("/api/tokens", headers=admin, json={"name": "x", "scope": "ingest"}).status_code == 422


def test_the_admin_password_session_is_the_whole_app(client):
    from cpsim import auth
    with db.connect() as conn:
        auth.set_admin_password(conn, "correct-horse-battery")
    assert client.post("/api/login", json={"password": "correct-horse-battery"}, headers=CSRF).status_code == 200
    s = client.get("/api/session").json()
    assert s["state"] == "logged_in" and s["user"]["permissions"] == ["*"] and s["user"]["source"] == "local"
    assert client.get("/api/tokens").status_code == 200
