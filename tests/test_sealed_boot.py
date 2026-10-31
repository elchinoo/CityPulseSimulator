"""Sealed boot: with APGKIT_SEALED set the app serves nothing but /_seal/*, /healthz until the operator unseals it."""
import importlib

import pytest
from apgkit import sealed as sealing
from conftest import TEST_DATABASE_URL
from fastapi.testclient import TestClient

PASS = "correct-horse-battery-staple-9"


@pytest.fixture
def sealed_app(monkeypatch):
    from cpsim import api
    monkeypatch.setenv("APGKIT_SEALED", sealing.seal({"CPSIM_METRICS_TOKEN": "tok-from-the-blob"}, PASS))
    api = importlib.reload(api)
    yield api
    monkeypatch.delenv("APGKIT_SEALED")
    sealing.set_active(None)
    importlib.reload(api)


def _unseal(client, passphrase):
    key = client.get("/_seal/key").json()["public_key"]
    return client.post("/_seal/unseal", json=sealing.make_envelope(key, passphrase))


def test_sealed_gate_then_unseal(sealed_app, monkeypatch):
    if not TEST_DATABASE_URL:       # no database: only the migration callback is replaced and the vault unlock dropped (DB runs do both for real)
        from cpsim import migrate
        monkeypatch.setattr(migrate, "run", lambda: [])
        del sealed_app.sealed._callbacks[1:]
    c = TestClient(sealed_app.app, raise_server_exceptions=False)
    assert c.get("/api/cities").status_code == 503
    assert c.get("/healthz").status_code == 200
    assert c.get("/_seal/status").json()["state"] == "sealed"
    assert _unseal(c, PASS).json() == {"state": "unsealed"}
    assert c.get("/api/cities").status_code != 503
