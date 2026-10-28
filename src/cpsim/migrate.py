"""Plain-SQL migrations (the kit's runner), then the kit's shared auth tables."""
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def run() -> list[str]:
    """Apply the SQL migrations. The shared tables (settings, sessions, API tokens, OIDC state) come from the kit."""
    from apgkit import migrate as kit_migrate
    from apgkit.auth.schema import SQL as AUTH_SQL

    from . import hardening
    from .core import db

    applied = kit_migrate.run(db, MIGRATIONS)
    with db.connect() as conn:      # after the app's own migrations: it upgrades tables they created and adds the OIDC state
        conn.execute(AUTH_SQL)
        conn.commit()
    with db.connect() as conn:      # then the read-only role's grants, once every table exists (kit_migrate.run's own hooks run too early)
        hardening.apply(conn)
    return applied
