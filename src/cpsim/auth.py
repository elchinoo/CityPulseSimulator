"""Credentials. First run: a one-time setup code is printed to the server log; presenting it in the browser sets the admin
password (stored only as a salted scrypt hash). Browser sessions are random cookies held in memory (only their SHA-256).
Automation can use CPSIM_API_KEY (full access); Prometheus or pagerlens-agent can use CPSIM_METRICS_TOKEN (only /metrics).
"""
import hashlib
import hmac
import secrets
import time

from . import passwords
from .config import get_settings

SESSION_SECONDS = 12 * 3600
_K_ADMIN = "admin_password_hash"
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters  # pragma: allowlist secret
_setup_code: str | None = None
_sessions: dict[str, float] = {}


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def setup_code() -> str:
    global _setup_code
    if _setup_code is None:
        raw = "".join(secrets.choice(_ALPHABET) for _ in range(12))
        _setup_code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
    return _setup_code


def admin_configured(store) -> bool:
    return store.get(_K_ADMIN) is not None


def needs_setup(store) -> bool:
    return not admin_configured(store) and not get_settings().api_key


def complete_setup(store, code: str, password: str) -> None:
    global _setup_code
    if admin_configured(store):
        raise PermissionError("setup is already complete")
    if not hmac.compare_digest(code.strip().upper().encode(), setup_code().encode()):
        raise PermissionError("invalid setup code")
    set_password(store, password)
    _setup_code = None


def set_password(store, password: str) -> None:
    passwords.check_strength(password)
    store.put(_K_ADMIN, passwords.hash_password(password))
    _sessions.clear()


def login(store, password: str) -> bool:
    stored = store.get(_K_ADMIN)
    return stored is not None and passwords.verify_password(password, stored)


def change_password(store, current: str, new: str) -> None:
    if not login(store, current):
        raise PermissionError("current password is incorrect")
    set_password(store, new)


def reset_admin(store) -> None:
    store.delete(_K_ADMIN)
    _sessions.clear()


def create_session() -> str:
    raw = secrets.token_urlsafe(32)
    now = time.time()
    for k in [k for k, exp in _sessions.items() if exp < now]:
        del _sessions[k]
    _sessions[sha256(raw)] = now + SESSION_SECONDS
    return raw


def session_valid(raw: str | None) -> bool:
    return bool(raw) and _sessions.get(sha256(raw), 0) > time.time()


def delete_session(raw: str | None) -> None:
    if raw:
        _sessions.pop(sha256(raw), None)


def static_key_matches(raw: str) -> bool:
    key = get_settings().api_key
    return bool(key) and hmac.compare_digest(raw.encode(), key.encode())


def metrics_token_matches(raw: str) -> bool:
    token = get_settings().metrics_token
    return bool(token) and hmac.compare_digest(raw.encode(), token.encode())
