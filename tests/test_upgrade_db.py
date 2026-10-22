"""Upgrading from the first release: its Postgres tables keep working, and its file store can be imported."""
import json

import pytest
from apgkit import passwords

from cpsim import auth, migrate
from cpsim.core import db as database
from cpsim.store import PgStore, import_files

CITY = {"name": "oldtown", "groups": [{"type": "temperature", "count": 3, "protocol": "modbus_tcp", "frequency": 5}]}
OLD_SCHEMA = """
CREATE TABLE cpsim_setting (key text PRIMARY KEY, value text NOT NULL);
CREATE TABLE cpsim_city (name text PRIMARY KEY, spec jsonb NOT NULL);
CREATE TABLE cpsim_company (name text PRIMARY KEY, spec jsonb NOT NULL);
"""


def test_a_database_of_the_first_release_upgrades_in_place(db):
    with database.connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.execute(OLD_SCHEMA)
        c.execute("INSERT INTO cpsim_setting VALUES ('admin_password_hash', %s)", (passwords.hash_password("correct-horse-battery"),))
        c.execute("INSERT INTO cpsim_city VALUES ('oldtown', %s)", (json.dumps(CITY),))
        c.commit()
    assert migrate.run() == ["001_init.sql", "002_workloads.sql"]
    assert migrate.run() == []                                   # idempotent
    assert PgStore().cities()["oldtown"]["groups"][0]["count"] == 3
    with database.connect() as c:
        assert auth.admin_configured(c) and auth.login_password(c, "correct-horse-battery")     # the old admin password still works
        assert c.execute("SELECT to_regclass('web_session') AS t, to_regclass('api_token') AS a, to_regclass('oidc_login') AS o").fetchone() == {
            "t": "web_session", "a": "api_token", "o": "oidc_login"}


def test_import_files_loads_cities_companies_and_the_admin_password(db, tmp_path):
    state = {"settings": {"admin_password_hash": passwords.hash_password("correct-horse-battery")},
             "cities": {"oldtown": CITY, "broken": {"name": "broken", "groups": [{"type": "nope", "count": 1}]}},
             "companies": {"acme": {"name": "acme", "employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}}}}
    (tmp_path / "state.json").write_text(json.dumps(state))
    assert import_files(str(tmp_path)) == {"cities": 1, "companies": 1, "skipped": 1, "admin_password": 1}
    assert list(PgStore().cities()) == ["oldtown"] and list(PgStore().companies()) == ["acme"]
    with database.connect() as c:
        assert auth.login_password(c, "correct-horse-battery")
    assert import_files(str(tmp_path))["admin_password"] == 0   # never overwrites an existing admin password
    with pytest.raises(FileNotFoundError):
        import_files(str(tmp_path / "missing"))


def test_a_deployment_on_the_previous_schema_gains_workloads_and_keeps_everything(db, tmp_path):
    """Apply only the old migration (what a running install has), put data in it, then run the new code's migrations."""
    from apgkit import migrate as kit_migrate

    with database.connect() as c:
        c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        c.commit()
    (tmp_path / "001_init.sql").write_text((migrate.MIGRATIONS / "001_init.sql").read_text())
    assert kit_migrate.run(database, tmp_path) == ["001_init.sql"]
    company = {"name": "oldco", "employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}}          # a company saved before schedules existed
    with database.connect() as c:
        c.execute("INSERT INTO cpsim_city VALUES ('oldtown', %s)", (json.dumps(CITY),))
        c.execute("INSERT INTO cpsim_company VALUES ('oldco', %s)", (json.dumps(company),))
        c.commit()
    assert migrate.run() == ["002_workloads.sql"]
    assert migrate.run() == []
    store = PgStore()
    assert store.workloads() == {} and store.cities()["oldtown"]["groups"][0]["count"] == 3
    from cpsim.hr_model import parse_company
    assert parse_company(store.companies()["oldco"]).schedule is None, "an old company has no schedule and parses as before"
    store.save_workload("w", {"name": "w", "kind": "llmgw"})
    assert list(store.workloads()) == ["w"]
    store.delete_workload("w")
    assert store.workloads() == {}
