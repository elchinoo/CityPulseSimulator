"""Adoption of agentpg-kit 0.4.0: contexts for saved credentials (legacy read-through, rotation), the rotation route, the login limiter,
OIDC browser binding, the anonymous-mode bind host and connections pinned to the validated address."""
import base64

import httpx
import pytest
from apgkit.netguard import Resolved
from apgkit.vault import VaultError
from cryptography.fernet import Fernet
from test_credentials_db import CSRF, KEY, client, who  # noqa: F401  (the fixture: the app with federated roles and a platform secret)

from cpsim import core, credentials, outbound
from cpsim.core import db
from cpsim.hr_engine import TrustDeskClient
from cpsim.model import SpecError
from cpsim.sinks import HttpSink

NAMES = ("PAYBROKER_SECRET_KEY", "X_TOKEN")


def _legacy(text: str) -> str:
    return Fernet(base64.urlsafe_b64encode(core.vault._data_key)).encrypt(text.encode()).decode()


def _cipher(name: str) -> str:
    with db.connect() as conn:
        return conn.execute("SELECT ciphertext FROM cpsim_credential WHERE name = %s", (name,)).fetchone()["ciphertext"]


def _save(client, name="PAYBROKER_SECRET_KEY", value=KEY):  # noqa: F811
    origin = None if name == "PAYBROKER_SECRET_KEY" else "https://pb.example"
    assert client.put(f"/api/credentials/{name}", headers={**who("admin"), **CSRF}, json={"value": value, "origin": origin}).status_code == 200


# ---- contexts ----------------------------------------------------------------------------------------------------------
def test_a_saved_credential_is_bound_to_its_name(client):  # noqa: F811
    _save(client)
    tok = _cipher("PAYBROKER_SECRET_KEY")
    assert tok.startswith("v2$")
    assert core.vault.decrypt(tok, credentials.context("PAYBROKER_SECRET_KEY")) == KEY
    for wrong in ("", credentials.context("X_TOKEN")):
        with pytest.raises(VaultError):
            core.vault.decrypt(tok, wrong)
    # a ciphertext copied to another credential name (database write access) is not accepted there
    _save(client, "X_TOKEN", "other-value")
    with db.connect() as conn:
        conn.execute("UPDATE cpsim_credential SET ciphertext = %s WHERE name = 'X_TOKEN'", (tok,))
        conn.commit()
        credentials.load(conn, core.vault)
    assert "X_TOKEN" not in credentials._stored and credentials._stored["PAYBROKER_SECRET_KEY"] == KEY


def test_values_saved_before_contexts_are_read_and_upgraded_in_place(client):  # noqa: F811
    _save(client)
    _save(client, "X_TOKEN", "x-value")
    with db.connect() as conn:                       # one row as the old release wrote it (Fernet), one as 0.4.0 without a context
        conn.execute("UPDATE cpsim_credential SET ciphertext = %s WHERE name = 'PAYBROKER_SECRET_KEY'", (_legacy(KEY),))
        conn.execute("UPDATE cpsim_credential SET ciphertext = %s WHERE name = 'X_TOKEN'", (core.vault.encrypt("x-value"),))
        conn.commit()
        assert credentials.load(conn, core.vault)
    assert credentials._stored == {"PAYBROKER_SECRET_KEY": KEY, "X_TOKEN": "x-value"}
    for name, value in (("PAYBROKER_SECRET_KEY", KEY), ("X_TOKEN", "x-value")):
        tok = _cipher(name)
        assert tok.startswith("v2$") and core.vault.decrypt(tok, credentials.context(name)) == value


def test_the_store_is_registered_with_its_context():
    stores = core.vault.stores()
    assert stores[credentials.STORE]["context"] == "cpsim_credential.ciphertext:{name}"


# ---- rotation ----------------------------------------------------------------------------------------------------------
def test_rotation_reencrypts_every_credential_and_upgrades_legacy_tokens(client):  # noqa: F811
    from cpsim import auth
    with db.connect() as conn:
        auth.set_admin_password(conn, "correct-horse-battery")
    _save(client)
    _save(client, "X_TOKEN", "x-value")
    with db.connect() as conn:
        conn.execute("UPDATE cpsim_credential SET ciphertext = %s WHERE name = 'X_TOKEN'", (_legacy("x-value"),))
        conn.commit()
    before = {n: _cipher(n) for n in NAMES}
    with db.connect() as conn:
        counts = core.vault.rotate_data_key(conn, password="correct-horse-battery")
    assert counts == {credentials.STORE: 2}
    for name, value in (("PAYBROKER_SECRET_KEY", KEY), ("X_TOKEN", "x-value")):
        tok = _cipher(name)
        assert tok != before[name] and tok.startswith("v2$") and core.vault.decrypt(tok, credentials.context(name)) == value


def test_the_rotation_route_needs_admin_confirm_and_the_password(client, capfd):  # noqa: F811
    from cpsim import auth
    with db.connect() as conn:
        auth.set_admin_password(conn, "correct-horse-battery")
    _save(client)
    old = _cipher("PAYBROKER_SECRET_KEY")
    admin = {**who("admin"), **CSRF}
    assert client.post("/api/vault/rotate-key", headers={**who("operator", sub="o"), **CSRF}, json={"password": "x", "confirm": True}).status_code == 403
    assert client.post("/api/vault/rotate-key", headers=admin, json={"password": "correct-horse-battery"}).status_code == 422      # no confirm
    assert client.post("/api/vault/rotate-key", headers=admin, json={"password": "wrong-password-here", "confirm": True}).status_code == 403
    assert _cipher("PAYBROKER_SECRET_KEY") == old
    r = client.post("/api/vault/rotate-key", headers=admin, json={"password": "correct-horse-battery", "confirm": True})
    assert r.status_code == 200 and r.json()["reencrypted"] == {credentials.STORE: 1} and r.json()["restart_other_replicas"]
    assert _cipher("PAYBROKER_SECRET_KEY") != old and credentials.get("PAYBROKER_SECRET_KEY", "https://pb.example") == KEY
    err = capfd.readouterr().err
    assert "vault data key rotated" in err and KEY not in err and "correct-horse-battery" not in err


# ---- login -------------------------------------------------------------------------------------------------------------
def test_password_login_is_counted_before_hashing(client):  # noqa: F811
    from cpsim import auth
    with db.connect() as conn:
        auth.set_admin_password(conn, "correct-horse-battery")
    for _ in range(core.auth.limiter.limit + 1):
        r = client.post("/api/login", json={"password": "wrong-password"}, headers=CSRF)
        if r.status_code == 429:
            break
        assert r.status_code == 401
    assert r.status_code == 429
    assert client.post("/api/login", json={"password": "correct-horse-battery"}, headers=CSRF).status_code == 429     # the limit holds for a good password too


# ---- anonymous mode ----------------------------------------------------------------------------------------------------
def test_anonymous_mode_is_honoured_only_on_loopback(monkeypatch):
    monkeypatch.setenv("CPSIM_ALLOW_ANONYMOUS", "true")
    monkeypatch.delenv("CPSIM_ALLOW_ANONYMOUS_PUBLIC", raising=False)
    for var in ("CPSIM_HOST", "HOST"):
        monkeypatch.delenv(var, raising=False)
    assert core.bind_host() == "127.0.0.1" and core.auth.bind_host == "127.0.0.1" and core.auth.anonymous_allowed()
    monkeypatch.setattr(core.auth, "bind_host", "0.0.0.0")
    assert not core.auth.anonymous_allowed()
    monkeypatch.setenv("CPSIM_HOST", "0.0.0.0")
    assert core.bind_host() == "0.0.0.0"


# ---- pinned outbound connections ---------------------------------------------------------------------------------------
class _PinGuard:
    """Resolves every name to one fixed address, like the NetGuard would after validating it."""
    def __init__(self, ip="203.0.113.7"):
        self.ip = ip

    def resolve(self, url):
        from urllib.parse import urlsplit
        p = urlsplit(url)
        return Resolved(url.rstrip("/"), p.hostname, p.port or (443 if p.scheme == "https" else 80), p.scheme, self.ip)

    def check(self, url):
        return self.resolve(url).url


def _record(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(200, json={})
    return httpx.MockTransport(handler)


async def test_every_client_goes_to_the_validated_address_with_the_original_host(monkeypatch):
    monkeypatch.setattr(outbound, "guard", _PinGuard())
    seen = []
    from cpsim.kinds.base import Http
    from cpsim.reporter import Reporter
    sink, td, http, rep = HttpSink("https://pb.example:8443", "t"), TrustDeskClient("https://pb.example", "t"), Http("https://pb.example", "t"), \
        Reporter("https://pagerlens.example", "t", None)
    for c in (sink.client, td.http, http.client, rep.client):
        c._transport = _record(seen)
    await sink.client.post("/api/readings", json={})
    await td.http.get("/p")
    await http.client.get("/q")
    await rep.client.get("/r")
    assert seen == [("https://203.0.113.7:8443/api/readings", "pb.example:8443", "pb.example"),
                    ("https://203.0.113.7/p", "pb.example", "pb.example"),
                    ("https://203.0.113.7/q", "pb.example", "pb.example"),
                    ("https://203.0.113.7/r", "pagerlens.example", "pagerlens.example")]
    for c in (sink.client, td.http, http.client, rep.client):
        assert c.follow_redirects is False


def test_a_target_that_fails_the_netguard_or_the_allow_list_gets_no_client(monkeypatch):
    class Refusing(_PinGuard):
        def resolve(self, url):
            from apgkit.netguard import UnsafeURL
            raise UnsafeURL("that address is private or local")
    monkeypatch.setattr(outbound, "guard", Refusing())
    with pytest.raises(SpecError, match="private"):
        outbound.client("https://pb.example")
    monkeypatch.setattr(outbound, "guard", _PinGuard())
    with pytest.raises(SpecError, match="not an allowed target"):
        outbound.client("https://not-listed.test")
