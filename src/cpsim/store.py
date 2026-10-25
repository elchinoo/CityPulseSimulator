"""Persistence for cities, companies and workloads in Postgres (the AgentPG way): Postgres is required (DATABASE_URL) for the server;
the headless commands use `MemoryStore` and need none.

None holds a secret: ingest, client and API keys live in environment variables that a city or company only names. The admin
password hash, sessions and API tokens belong to the kit's auth tables (see `migrate.py`).
"""
import json

from .core import db

# What can be started and stopped, and the column that remembers it was (see Runtime.resume): a company has a run and a schedule.
DESIRED = {"city": ("cpsim_city", "desired_running"), "workload": ("cpsim_workload", "desired_running"),
           "company": ("cpsim_company", "desired_running"), "schedule": ("cpsim_company", "desired_schedule")}


class PgStore:
    def _conn(self):
        return db.connect(retries=2)

    def cities(self) -> dict[str, dict]:
        with self._conn() as c:
            return {r["name"]: r["spec"] for r in c.execute("SELECT name, spec FROM cpsim_city ORDER BY name")}

    def save_city(self, name: str, spec: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO cpsim_city (name, spec) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec", (name, json.dumps(spec)))

    def delete_city(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cpsim_city WHERE name=%s", (name,))

    def companies(self) -> dict[str, dict]:
        with self._conn() as c:
            return {r["name"]: r["spec"] for r in c.execute("SELECT name, spec FROM cpsim_company ORDER BY name")}

    def save_company(self, name: str, spec: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO cpsim_company (name, spec) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec",
                      (name, json.dumps(spec)))

    def delete_company(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cpsim_company WHERE name=%s", (name,))

    def workloads(self) -> dict[str, dict]:
        with self._conn() as c:
            return {r["name"]: r["spec"] for r in c.execute("SELECT name, spec FROM cpsim_workload ORDER BY name")}

    def save_workload(self, name: str, spec: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO cpsim_workload (name, spec) VALUES (%s,%s) ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec",
                      (name, json.dumps(spec)))

    def delete_workload(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cpsim_workload WHERE name=%s", (name,))

    def desired(self, kind: str) -> set[str]:
        """The names of this kind that were started and not stopped by a person: they run again after a restart."""
        table, col = DESIRED[kind]
        with self._conn() as c:
            return {r["name"] for r in c.execute(f"SELECT name FROM {table} WHERE {col}")}

    def set_desired(self, kind: str, name: str, on: bool) -> None:
        table, col = DESIRED[kind]
        with self._conn() as c:
            c.execute(f"UPDATE {table} SET {col}=%s WHERE name=%s", (on, name))

    def setting(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM app_setting WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def save_setting(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO app_setting (key, value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))

    def healthy(self) -> bool:
        try:
            with db.connect(retries=1) as c:
                c.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False


class MemoryStore:
    """The same interface in memory, for the headless commands (`cpsim run`, `hr-run`, `workloads run`): no Postgres needed."""

    def __init__(self):
        self._cities: dict[str, dict] = {}
        self._companies: dict[str, dict] = {}
        self._workloads: dict[str, dict] = {}
        self._desired: dict[str, set[str]] = {k: set() for k in DESIRED}
        self._settings: dict[str, str] = {}

    def cities(self) -> dict[str, dict]:
        return dict(self._cities)

    def save_city(self, name: str, spec: dict) -> None:
        self._cities[name] = spec

    def delete_city(self, name: str) -> None:
        self._cities.pop(name, None)

    def companies(self) -> dict[str, dict]:
        return dict(self._companies)

    def save_company(self, name: str, spec: dict) -> None:
        self._companies[name] = spec

    def delete_company(self, name: str) -> None:
        self._companies.pop(name, None)

    def workloads(self) -> dict[str, dict]:
        return dict(self._workloads)

    def save_workload(self, name: str, spec: dict) -> None:
        self._workloads[name] = spec

    def delete_workload(self, name: str) -> None:
        self._workloads.pop(name, None)

    def desired(self, kind: str) -> set[str]:
        return set(self._desired[kind])

    def set_desired(self, kind: str, name: str, on: bool) -> None:
        (self._desired[kind].add if on else self._desired[kind].discard)(name)

    def setting(self, key: str) -> str | None:
        return self._settings.get(key)

    def save_setting(self, key: str, value: str) -> None:
        self._settings[key] = value

    def healthy(self) -> bool:
        return True


def open_store() -> PgStore:
    return PgStore()


def import_files(directory: str, store: PgStore | None = None) -> dict[str, int]:
    """One-shot upgrade from the file store of the first release (`<dir>/state.json`): cities, companies and, if none is set yet,
    the admin password hash (the hash format is unchanged). Specs that no longer validate are skipped and counted."""
    from pathlib import Path

    from . import auth
    from .hr_model import parse_company
    from .model import SpecError, parse_city

    data = json.loads((Path(directory) / "state.json").read_text())
    store = store or open_store()
    done = {"cities": 0, "companies": 0, "skipped": 0, "admin_password": 0}
    for key, parse, save in (("cities", parse_city, store.save_city), ("companies", parse_company, store.save_company)):
        for name, raw in (data.get(key) or {}).items():
            try:
                parse(raw)
            except SpecError:
                done["skipped"] += 1
                continue
            save(name, raw)
            done[key] += 1
    stored = (data.get("settings") or {}).get("admin_password_hash")
    if stored:
        with db.connect() as conn:
            if not auth.admin_configured(conn):
                conn.execute("INSERT INTO app_setting (key, value) VALUES ('admin_password_hash', %s)", (stored,))
                conn.commit()
                done["admin_password"] = 1
    return done
