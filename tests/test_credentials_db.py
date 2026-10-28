"""Credentials saved in the app: encrypted at rest, never returned, environment wins, locked without a platform secret, admin only."""
import httpx
import pytest
from apgkit.auth import Federation, RelyingParty, TokenVerifier
from fastapi.testclient import TestClient
from test_federation_db import AUD, ISS, Provider, access
from test_workload_kinds import PayBrokerMock

from cpsim import api, core, credentials, kinds
from cpsim.core import db

CSRF = {"X-CPSim-CSRF": "1"}
KEY = "sk_test_SAVED_SENTINEL"
SPEC = {"kind": "paybroker", "target_url": "https://pb.example", "schedule": {"interval": 0.2, "batch": 2}}


@pytest.fixture
def client(monkeypatch, db):
    for v in ("CPSIM_API_KEY", "CPSIM_ALLOW_ANONYMOUS", "CPSIM_METRICS_TOKEN", "PAYBROKER_SECRET_KEY", "PAYBROKER_SECRET_KEY_FILE"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("CPSIM_ENCRYPTION_KEY", "platform-secret-for-tests")
    core.vault.lock()
    credentials._stored.clear()
    provider = Provider()
    verifier = TokenVerifier(ISS, AUD, http=provider.http())
    monkeypatch.setattr(core.auth, "verifier", verifier)
    monkeypatch.setattr(api, "fed", Federation(ISS, AUD, verifier, RelyingParty(ISS, AUD, None, http=provider.http()), True))
    with TestClient(api.app) as c:
        yield c
    core.vault.lock()
    credentials._stored.clear()


def who(*roles, **kw):
    return access(sub=kw.pop("sub", "usr_1"), roles=[f"cpsim:{r}" for r in roles], **kw)


def test_a_saved_credential_is_encrypted_never_returned_and_used_by_a_workload(client, monkeypatch):
    admin = who("admin")
    assert client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers={**admin, **CSRF}, json={"value": KEY}).status_code == 200
    with db.connect() as conn:
        row = conn.execute("SELECT ciphertext FROM cpsim_credential WHERE name = 'PAYBROKER_SECRET_KEY'").fetchone()
    assert KEY not in row["ciphertext"]
    listing = client.get("/api/credentials", headers=admin)
    assert KEY not in listing.text
    item = next(i for i in listing.json()["items"] if i["name"] == "PAYBROKER_SECRET_KEY")
    assert item["source"] == "saved" and item["saved"] and listing.json()["unlocked"]
    # a workload with no environment variable now starts, sending the saved key to its own target only
    mock = PayBrokerMock(KEY)
    real = kinds.build_sender
    monkeypatch.setattr(kinds, "build_sender", lambda wl, **kw: real(wl, transport=httpx.MockTransport(mock.handler), **kw))
    assert client.put("/api/workloads/shop", headers={**admin, **CSRF}, json=SPEC).status_code == 200
    r = client.post("/api/workloads/shop/run-once", headers={**admin, **CSRF})
    assert r.status_code == 200, r.text
    assert KEY not in r.text and mock.calls


def test_it_survives_a_restart_and_the_environment_wins(client, monkeypatch):
    admin = who("admin")
    client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers={**admin, **CSRF}, json={"value": KEY})
    credentials._stored.clear()
    core.vault.lock()
    with db.connect() as conn:
        assert credentials.load(conn, core.vault)               # the platform secret unlocks it again
    assert credentials.get("PAYBROKER_SECRET_KEY") == KEY
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_FROM_ENV")
    assert credentials.get("PAYBROKER_SECRET_KEY") == "sk_test_FROM_ENV" and credentials.source("PAYBROKER_SECRET_KEY") == "environment"


def test_only_an_admin_may_read_or_change_them_and_names_are_restricted(client):
    for roles in (("operator",), ("viewer",)):
        h = {**who(*roles, sub="x"), **CSRF}
        assert client.get("/api/credentials", headers=h).status_code == 403
        assert client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers=h, json={"value": "x"}).status_code == 403
        assert client.delete("/api/credentials/PAYBROKER_SECRET_KEY", headers=h).status_code == 403
    admin = {**who("admin"), **CSRF}
    for bad in ("CPSIM_ADMIN_TOKEN", "DATABASE_SECRET", "lowercase_token", "HOME"):
        assert client.put(f"/api/credentials/{bad}", headers=admin, json={"value": "v"}).status_code == 422
    assert client.put("/api/credentials/X_TOKEN", headers=admin, json={"value": "two\nlines"}).status_code == 422
    assert client.delete("/api/credentials/NEVER_SAVED_TOKEN", headers=admin).status_code == 404


def test_without_a_platform_secret_the_store_is_locked_and_saves_are_refused(client, monkeypatch):
    monkeypatch.delenv("CPSIM_ENCRYPTION_KEY")
    core.vault.lock()
    admin = {**who("admin"), **CSRF}
    assert client.get("/api/credentials", headers=admin).json()["unlocked"] is False
    r = client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers=admin, json={"value": KEY})
    assert r.status_code == 409 and "locked" in r.json()["detail"]


def test_delete_removes_it_and_a_workload_then_fails_with_a_clear_message(client):
    admin = {**who("admin"), **CSRF}
    client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers=admin, json={"value": KEY})
    assert client.delete("/api/credentials/PAYBROKER_SECRET_KEY", headers=admin).status_code == 200
    assert credentials.get("PAYBROKER_SECRET_KEY") == ""
    client.put("/api/workloads/shop", headers=admin, json=SPEC)
    r = client.post("/api/workloads/shop/run-once", headers=admin)
    assert r.status_code >= 400 and "Settings > Credentials" in r.text


def test_reset_admin_names_and_deletes_the_credentials_the_vault_reset_makes_unreadable(client, monkeypatch, capsys):
    from cpsim import cli

    admin = {**who("admin"), **CSRF}
    client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers=admin, json={"value": KEY})
    client.put("/api/credentials/LLMGW_CLIENT_KEY", headers=admin, json={"value": "lg_x"})

    def rows(conn):
        return sorted(r["name"] for r in conn.execute("SELECT name FROM cpsim_credential").fetchall())
    with db.connect() as conn:
        assert cli.reset_admin(conn, ask=lambda _: "no") is False                # not the phrase: nothing changes
        assert rows(conn) == ["LLMGW_CLIENT_KEY", "PAYBROKER_SECRET_KEY"] and core.vault.is_initialized(conn)
        out = capsys.readouterr().out
        assert "LLMGW_CLIENT_KEY, PAYBROKER_SECRET_KEY" in out and "DELETED" in out and "Cancelled" in out
        assert KEY not in out

        def eof(_):
            raise EOFError
        assert cli.reset_admin(conn, ask=eof) is False                           # no terminal: refuse rather than guess
        assert cli.reset_admin(conn, ask=lambda _: cli.CONFIRM_DELETE) is True
        assert rows(conn) == [] and not core.vault.is_initialized(conn)
        assert "2 saved credential(s) deleted" in capsys.readouterr().out


def test_reset_admin_with_no_saved_credentials_asks_nothing_and_yes_skips_the_question(client, capsys):
    from cpsim import cli

    def never(_):
        raise AssertionError("must not ask")
    with db.connect() as conn:
        assert cli.reset_admin(conn, ask=never) is True
        assert "deleted" not in capsys.readouterr().out
    admin = {**who("admin"), **CSRF}
    client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers=admin, json={"value": KEY})
    with db.connect() as conn:
        assert cli.reset_admin(conn, assume_yes=True, ask=never) is True
        assert conn.execute("SELECT count(*) AS n FROM cpsim_credential").fetchone()["n"] == 0
