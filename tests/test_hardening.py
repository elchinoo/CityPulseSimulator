"""Database insights (EDB extensions, degrading) and read-only / AI hardening proven with a real read-only login role."""
import re

import psycopg
import pytest
from conftest import TEST_DATABASE_URL
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from test_credentials_db import CSRF, client, who  # noqa: F401  (the fixture: the app with federated roles and a platform secret)

from cpsim import core, hardening, migrate

LOGIN = "cpsim_ro_login"


def test_kit_is_at_least_0_3_0():
    import apgkit
    assert tuple(int(x) for x in apgkit.__version__.split(".")[:3]) >= (0, 3, 0)


@pytest.fixture
def ro(client):  # noqa: F811
    """A login role in a1p_readonly holding what AgentPG's default privileges give it, after the real migrate path ran the hook."""
    assert client.put("/api/credentials/PAYBROKER_SECRET_KEY", headers={**who("admin"), **CSRF}, json={"value": "sk_test_RO_SENTINEL"}).status_code == 200
    with core.db.connect() as c:
        c.execute("DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='a1p_readonly') THEN CREATE ROLE a1p_readonly NOLOGIN; END IF; END $$")
        c.execute(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{LOGIN}') THEN CREATE ROLE {LOGIN} LOGIN; END IF; END $$")
        c.execute(f"GRANT a1p_readonly TO {LOGIN}")
        c.execute("GRANT USAGE ON SCHEMA public TO a1p_readonly")
        c.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO a1p_readonly")
        c.commit()
    migrate.run()                                          # the real startup path (`cpsim serve` / `cpsim migrate`)
    conn = psycopg.connect(make_conninfo(TEST_DATABASE_URL, user=LOGIN), row_factory=dict_row, autocommit=True)
    yield conn
    conn.close()


def test_read_only_login_cannot_read_secret_tables(ro):
    with core.db.connect() as c:
        assert c.execute("SELECT count(*) AS n FROM cpsim_credential").fetchone()["n"] == 1      # the owner sees it: the test is meaningful
    for t in hardening.DENIED_TABLES:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            ro.execute(f'SELECT * FROM "{t}"')
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        ro.execute("SELECT ciphertext FROM cpsim_credential")
    for t in ("cpsim_city", "cpsim_company", "cpsim_workload"):             # synthetic specs, no personal data: readable
        ro.execute(f"SELECT count(*) FROM {t}")
    with pytest.raises(psycopg.Error):
        ro.execute("INSERT INTO cpsim_city (name, spec) VALUES ('x', '{}')")


def test_every_secret_looking_column_is_covered(ro):
    """Guards new migrations: a hash/secret/ciphertext/key column must sit in a denied table."""
    pat = re.compile(r"hash|secret|cipher|_enc$|private|verifier|password|token")
    with core.db.connect() as c:
        cols = c.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='public'").fetchall()
    uncovered = [(r["table_name"], r["column_name"]) for r in cols
                 if pat.search(r["column_name"]) and r["table_name"] not in hardening.DENIED_TABLES]
    assert not uncovered, f"secret-looking columns readable by a1p_readonly: {uncovered}"


def test_hardening_is_idempotent_and_quiet_without_the_role(ro):
    with core.db.connect() as c:
        a, b = hardening.apply(c), hardening.apply(c)
        assert a == b and a["role_exists"] and all(a["denied"].values())
        assert hardening.apply(c, role="no_such_readonly_role") == {"role_exists": False, "masked": {}, "denied": {}}
    migrate.run()


def test_insights_route_is_admin_only_and_degrades_on_plain_postgres(client):  # noqa: F811
    assert client.get("/api/insights").status_code in (401, 403)
    for roles in (("viewer",), ("operator",)):
        assert client.get("/api/insights", headers=who(*roles)).status_code == 403          # statement text is admin-only
    r = client.get("/api/insights?order=mean&limit=5", headers=who("admin"))
    assert r.status_code == 200
    d = r.json()
    assert set(d) == {"sources", "top_queries", "tuner", "advisor"}
    assert d["tuner"]["available"] is False and d["advisor"]["available"] is False       # no EDB extensions on the throwaway Postgres
    assert d["sources"]["edb_stat_monitor"]["available"] is False and d["sources"]["edb_stat_monitor"]["reason"]
    assert client.get("/api/insights?order=bogus", headers=who("admin")).status_code == 200


def test_insights_metrics_are_exported(client):  # noqa: F811
    body, _ = core.metrics.render()
    assert b'db_insights_available{app="cpsim"}' in body
    assert b"db_insights_tuner_recommendations" not in body             # absent when edb_pg_tuner is unavailable
