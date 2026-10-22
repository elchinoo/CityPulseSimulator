"""Credentials for the apps the simulator drives (PayBroker merchant key, gateway client key, ingest tokens).

A workload, city or company names a variable (`PAYBROKER_SECRET_KEY`). The value comes from the environment when it is set there (Docker,
headless, a platform that injects it); otherwise from the credential saved in the app, which is encrypted with the vault and never
shown again. The decrypted values live only in this process's memory.
"""
import threading

from apgkit.config import read_secret

from .model import SpecError

_stored: dict[str, str] = {}
_lock = threading.Lock()


def get(name: str) -> str:
    """The credential called `name`: the environment first, then the saved one; empty when there is none."""
    return read_secret(name) or _stored.get(name, "")


def source(name: str) -> str:
    return "environment" if read_secret(name) else ("saved" if name in _stored else "none")


def load(conn, vault) -> bool:
    """Unlock the vault with the platform secret (creating it on first use) and decrypt the saved credentials. False when it stays locked."""
    if not vault.is_unlocked():
        if vault.is_initialized(conn):
            vault.try_unlock_with_platform_secret(conn)
        else:
            vault.initialize_with_platform_secret(conn)
    if not vault.is_unlocked():
        return False
    rows = conn.execute("SELECT name, ciphertext FROM cpsim_credential").fetchall()
    fresh = {}
    for r in rows:
        try:
            fresh[r["name"]] = vault.decrypt(r["ciphertext"])
        except Exception:  # noqa: BLE001 - a value from another data key; leave it out rather than fail the others
            continue
    with _lock:
        _stored.clear()
        _stored.update(fresh)
    return True


def saved_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM cpsim_credential").fetchall()}


def put(conn, vault, name: str, value: str) -> None:
    from .wl_model import (
        check_secret_env,  # not at import time: wl_model reaches the kinds, which read credentials
    )

    name = check_secret_env(name, "name")
    if not vault.is_unlocked():
        raise SpecError("the credential store is locked: set CPSIM_ENCRYPTION_KEY on the app, or sign in with the admin password")
    value = value.strip()
    if not value or len(value) > 4096 or any(c in value for c in "\r\n\0"):
        raise SpecError("a credential is one line of at most 4096 characters")
    conn.execute("INSERT INTO cpsim_credential (name, ciphertext) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE "
                 "SET ciphertext = EXCLUDED.ciphertext, updated_at = now()", (name, vault.encrypt(value)))
    conn.commit()
    with _lock:
        _stored[name] = value


def delete(conn, name: str) -> bool:
    cur = conn.execute("DELETE FROM cpsim_credential WHERE name = %s", (name,))
    conn.commit()
    with _lock:
        _stored.pop(name, None)
    return cur.rowcount > 0
