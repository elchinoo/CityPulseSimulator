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
    assert migrate.run() == ["001_init.sql"]
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
