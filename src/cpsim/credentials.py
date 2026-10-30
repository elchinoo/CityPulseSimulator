"""Credentials for the apps the simulator drives (PayBroker merchant key, gateway client key, ingest tokens).

A workload, city or company names a variable (`PAYBROKER_SECRET_KEY`). The value comes from the environment when it is set there (Docker,
headless, a platform that injects it); otherwise from the credential saved in the app, which is encrypted with the vault and never
shown again. The decrypted values live only in this process's memory.
"""
import threading

from apgkit.config import read_secret

from . import outbound
from .model import SpecError

_stored: dict[str, str] = {}
_origins: dict[str, str] = {}          # name -> where the saved value may be sent: an origin or a fleet reference (`${PAYBROKER_URL}`)
_lock = threading.Lock()

STORE = "cpsim_credential.ciphertext"


def context(name: str) -> str:
    """What binds a saved ciphertext to its row: a value copied to another credential name does not decrypt there."""
    return f"{STORE}:{name}"


def register_store(vault) -> None:
    """Declare the encrypted column so `rotate_data_key` re-encrypts it, each value under its own context."""
    vault.register_store(STORE, "SELECT name, ciphertext AS value FROM cpsim_credential",
                         "UPDATE cpsim_credential SET ciphertext = %(value)s WHERE name = %(name)s", context=STORE + ":{name}")


def bound_origin(name: str) -> str:
    """Where the SAVED value of `name` may go: the origin it was saved with, else (a value saved before origins existed) the ecosystem
    app the name belongs to by default. Empty means it may not be sent anywhere until it is saved again with an origin."""
    return _origins.get(name) or ("${" + outbound.DEFAULT_TOKEN_ENVS[name] + "}" if name in outbound.DEFAULT_TOKEN_ENVS else "")


def get(name: str, target: str) -> str:
    """The credential called `name` for a request to `target`: the environment first (the operator chose that variable and the target
    is allow-listed), else the saved one, which is released only when the target has the origin it was saved for. Raises SpecError for a
    name that may not be sent at all and for a saved value bound elsewhere; empty when there is none. A value is never put in a message."""
    outbound.check_token_env(name, "credential")
    value = read_secret(name)
    if value:
        return value
    if name not in _stored:
        return ""
    bound = outbound.resolve_origin(bound_origin(name))
    if not bound or outbound.origin_of(target) != bound:
        raise SpecError(f"the saved credential {name} is bound to {bound or 'no origin'} and is not sent to {outbound.origin_of(target) or 'this target'}; "
                        "save it again for the address it should go to")
    return _stored[name]


def source(name: str) -> str:
    if name not in outbound.allowed_token_envs():
        return "refused"
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
    rows = conn.execute("SELECT name, ciphertext, origin FROM cpsim_credential").fetchall()
    fresh, origins, upgraded = {}, {}, []
    for r in rows:
        try:
            fresh[r["name"]] = vault.decrypt(r["ciphertext"], context(r["name"]))
            if vault.is_legacy(r["ciphertext"]):          # Fernet carries no context: rewrite it as a v2 token bound to this row
                upgraded.append((r["name"], vault.encrypt(fresh[r["name"]], context(r["name"]))))
        except Exception:  # noqa: BLE001 - not bound to this row: a value saved before contexts existed (legacy Fernet, or no context)
            try:
                plain = vault.decrypt(r["ciphertext"])
            except Exception:  # noqa: BLE001 - a value from another data key; leave it out rather than fail the others
                continue
            fresh[r["name"]] = plain
            upgraded.append((r["name"], vault.encrypt(plain, context(r["name"]))))
        if r["origin"]:
            origins[r["name"]] = r["origin"]
    for name, token in upgraded:        # read-through upgrade: bind the old values to their row from now on
        conn.execute("UPDATE cpsim_credential SET ciphertext = %s WHERE name = %s", (token, name))
    if upgraded:
        conn.commit()
    with _lock:
        _stored.clear()
        _stored.update(fresh)
        _origins.clear()
        _origins.update(origins)
    return True


def saved_names(conn) -> set[str]:
    return {r["name"] for r in conn.execute("SELECT name FROM cpsim_credential").fetchall()}


def clean_origin(name: str, origin: str | None) -> str:
    """What to store as the origin of a credential being saved: the given one (an http(s) origin or a fleet reference), else the default
    ecosystem app of a well-known name. It must be an allow-listed host. Raises SpecError."""
    from . import fleet
    origin = (origin or "").strip()
    if not origin:
        if name in outbound.DEFAULT_TOKEN_ENVS:
            return "${" + outbound.DEFAULT_TOKEN_ENVS[name] + "}"
        raise SpecError(f"say where {name} may be sent: give an origin such as https://app.example.org")
    if fleet.is_reference(origin):
        return fleet.check_reference(origin)
    norm = outbound.origin_of(origin)
    if not norm:
        raise SpecError("origin must be an http(s) address such as https://app.example.org")
    outbound.check_allowed(norm)
    return norm


def put(conn, vault, name: str, value: str, origin: str | None = None) -> None:
    from .wl_model import (
        check_secret_env,  # not at import time: wl_model reaches the kinds, which read credentials
    )

    name = check_secret_env(name, "name")
    if not vault.is_unlocked():
        raise SpecError("the credential store is locked: set CPSIM_ENCRYPTION_KEY on the app, or sign in with the admin password")
    value = value.strip()
    if not value or len(value) > 4096 or any(c in value for c in "\r\n\0"):
        raise SpecError("a credential is one line of at most 4096 characters")
    bound = clean_origin(name, origin)
    conn.execute("INSERT INTO cpsim_credential (name, ciphertext, origin) VALUES (%s, %s, %s) ON CONFLICT (name) DO UPDATE "
                 "SET ciphertext = EXCLUDED.ciphertext, origin = EXCLUDED.origin, updated_at = now()", (name, vault.encrypt(value, context(name)), bound))
    conn.commit()
    with _lock:
        _stored[name] = value
        _origins[name] = bound


def delete(conn, name: str) -> bool:
    cur = conn.execute("DELETE FROM cpsim_credential WHERE name = %s", (name,))
    conn.commit()
    with _lock:
        _stored.pop(name, None)
        _origins.pop(name, None)
    return cur.rowcount > 0
