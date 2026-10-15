"""Persistence for cities and settings: Postgres when a database is linked (the AgentPG way), else a JSON file.

Neither holds a secret other than the admin password HASH: ingest tokens live in environment variables that a city
only names.
"""
import json
import os
import tempfile
import threading
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from .config import get_settings


class FileStore:
    kind = "file"

    def __init__(self, directory: str):
        self.path = Path(directory) / "state.json"
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"settings": {}, "cities": {}}

    def _save(self, data: dict) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def get(self, key: str) -> str | None:
        with self.lock:
            return self._load()["settings"].get(key)

    def put(self, key: str, value: str) -> None:
        with self.lock:
            d = self._load()
            d["settings"][key] = value
            self._save(d)

    def delete(self, key: str) -> None:
        with self.lock:
            d = self._load()
            d["settings"].pop(key, None)
            self._save(d)

    def cities(self) -> dict[str, dict]:
        with self.lock:
            return self._load()["cities"]

    def save_city(self, name: str, spec: dict) -> None:
        with self.lock:
            d = self._load()
            d["cities"][name] = spec
            self._save(d)

    def delete_city(self, name: str) -> None:
        with self.lock:
            d = self._load()
            d["cities"].pop(name, None)
            self._save(d)

    def healthy(self) -> bool:
        return True


class PgStore:
    kind = "postgres"

    def __init__(self, url: str):
        self.url = url
        self._ready = False

    def _conn(self):
        conn = psycopg.connect(self.url, row_factory=dict_row, connect_timeout=10)
        if not self._ready:
            conn.execute("CREATE TABLE IF NOT EXISTS cpsim_setting (key text PRIMARY KEY, value text NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS cpsim_city (name text PRIMARY KEY, spec jsonb NOT NULL)")
            conn.commit()
            self._ready = True
        return conn

    def get(self, key: str) -> str | None:
        with self._conn() as c:
            row = c.execute("SELECT value FROM cpsim_setting WHERE key=%s", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key: str, value: str) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO cpsim_setting VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (key, value))

    def delete(self, key: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cpsim_setting WHERE key=%s", (key,))

    def cities(self) -> dict[str, dict]:
        with self._conn() as c:
            return {r["name"]: r["spec"] for r in c.execute("SELECT name, spec FROM cpsim_city ORDER BY name")}

    def save_city(self, name: str, spec: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO cpsim_city VALUES (%s,%s) ON CONFLICT (name) DO UPDATE SET spec=EXCLUDED.spec", (name, json.dumps(spec)))

    def delete_city(self, name: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM cpsim_city WHERE name=%s", (name,))

    def healthy(self) -> bool:
        try:
            with self._conn() as c:
                c.execute("SELECT 1")
            return True
        except Exception:  # noqa: BLE001
            return False


def open_store():
    cfg = get_settings()
    return PgStore(cfg.database_url) if cfg.database_url else FileStore(cfg.data_dir)
