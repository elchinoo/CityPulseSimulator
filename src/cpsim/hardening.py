"""Read-only / AI hardening, applied after every migration (see the kit's `apgkit.masking`).

AgentPG instances have the group role `a1p_readonly` (SELECT on every new public table by default privilege). Direct read-only database
users and AgentPG's restricted MCP server run as members of it. This hook REVOKEs SELECT from that role on every table that holds
secrets, hashes or ciphertext (there is no view for them). The simulator stores no personal data (cities, companies and workloads are
synthetic specs; credentials are kept by environment-variable NAME in the specs), so there is no masked view.

Idempotent, and quiet where the role does not exist (plain Postgres). Only the table owner can REVOKE: the result is verified.
"""
from __future__ import annotations

import logging

import psycopg
from apgkit import masking

log = logging.getLogger("cpsim.hardening")
ROLE = masking.READONLY_ROLE

MASKED_VIEWS: dict[str, tuple[str, dict[str, str]]] = {}
DENIED_TABLES = (
    "cpsim_credential",       # the credentials saved for the apps it drives (Fernet ciphertext)
    "app_setting",            # kit settings and the vault store (wrapped data key, admin password hash)
    "web_session", "api_token", "oidc_login",     # session / API token hashes, PKCE verifiers
)
REASSERT_EVERY = 300      # seconds: the re-assert loop (api.lifespan) re-runs `apply` and counts drift on DENIED_TABLES, because grants made
                          # after startup (`agentpg db-users create`, default privileges) reopen base tables


def apply(conn: psycopg.Connection, role: str = ROLE) -> dict[str, object]:
    """Deny. Returns {"role_exists", "masked": {}, "denied": {table: bool}}."""
    out: dict[str, object] = {"role_exists": False, "masked": {}, "denied": {}}
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        log.info("hardening skipped: role %s does not exist", role)
        return out
    out["role_exists"] = True
    for table in DENIED_TABLES:
        if not conn.execute("SELECT to_regclass(%s) AS t", (f"public.{table}",)).fetchone()["t"]:
            continue
        conn.execute(f'REVOKE SELECT ON public."{table}" FROM "{role}"')
        conn.commit()
        denied = not conn.execute("SELECT has_table_privilege(%s, %s, 'SELECT') AS ok", (role, f"public.{table}")).fetchone()["ok"]
        out["denied"][table] = denied                                                        # type: ignore[index]
        if not denied:
            log.warning("hardening: %s can still read %s (not the owner, or PUBLIC has SELECT)", role, table)
    return out
