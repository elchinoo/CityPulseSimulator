"""Which database `cpsim aidb-bench` may touch, and where its DSN comes from.

The benchmark creates tables, roles, models, a pipeline and agents (all named `cpsim_bench*`) with an operator-level DSN, so it follows
the simulator's outbound rules and is stricter in two places:

* the host must be listed in `CPSIM_TARGET_ALLOWLIST` itself (comma or space separated; `.example.org` matches a domain and its
  subdomains). Unlike an HTTP target, the hosts of the ecosystem URLs (CITYPULSE_URL, ...) do NOT count: they are not database hosts.
  An empty allow-list refuses everything.
* the NetGuard resolves the host once and the connection is pinned to that address (`hostaddr`); loopback and private addresses need
  `CPSIM_ALLOW_PRIVATE_URLS=true` (link-local is always refused), exactly as for an HTTP target.
* the database name must end in `bench` or `test` (it is a throwaway by design) unless `--allow-any-database` is passed.

The DSN comes from `CPSIM_AIDB_DSN` (or the file named by `CPSIM_AIDB_DSN_FILE`), else a hidden prompt. It is never printed, never
written to the report, and error text is scrubbed of it.
"""
import getpass
import os
import re
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

from apgkit.config import read_secret
from apgkit.netguard import UnsafeURL
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .. import outbound

DSN_ENV = "CPSIM_AIDB_DSN"
DEFAULT_PORT = 5432
SAFE_DB_SUFFIXES = ("bench", "test")


class TargetRefused(Exception):
    """The target is not allowed (or not usable). The message never contains the DSN."""


@dataclass(frozen=True)
class Target:
    conninfo: str           # the DSN with the validated address pinned: SECRET, never log or store it
    database: str


def allowlist() -> list[str]:
    """Hostnames and `.suffix` patterns from CPSIM_TARGET_ALLOWLIST (only that variable)."""
    return [w.lower().lstrip("*") if w.startswith("*.") else w.lower() for w in outbound._words(os.environ.get("CPSIM_TARGET_ALLOWLIST", ""))]


def host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a or (a.startswith(".") and host.endswith(a)) for a in allowlist())


def get_dsn(prompt=getpass.getpass, interactive: bool | None = None) -> str:
    """The DSN: environment (or `_FILE`), else a hidden prompt. Raises TargetRefused when neither is available."""
    value = read_secret(DSN_ENV)
    if value:
        return value.strip()
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        raise TargetRefused(f"no target database: set {DSN_ENV} (or {DSN_ENV}_FILE), or run in a terminal to be asked for it")
    value = prompt("AIDB benchmark target DSN (input hidden): ").strip()
    if not value:
        raise TargetRefused("no target database given")
    return value


def resolve(dsn: str, allow_any_database: bool = False) -> Target:
    """Validate `dsn` and return the pinned target. Raises TargetRefused (never echoing the DSN)."""
    try:
        parts = conninfo_to_dict(dsn)
    except Exception as exc:
        raise TargetRefused("the DSN could not be parsed") from exc
    host = (parts.get("host") or "").strip()
    if not host:
        raise TargetRefused("the DSN names no host (a unix socket or PGHOST fallback is not accepted: name an allow-listed host)")
    if host.startswith("/") or "," in host or "," in str(parts.get("port") or ""):
        raise TargetRefused("a unix socket or a multi-host DSN is not accepted: name one allow-listed host")
    if parts.get("hostaddr"):
        raise TargetRefused("the DSN must not set hostaddr: the address is resolved and pinned by the benchmark")
    if not allowlist():
        raise TargetRefused("CPSIM_TARGET_ALLOWLIST is empty: add the database's host to run the benchmark against it")
    if not host_allowed(host):
        raise TargetRefused(f"{host!r} is not in CPSIM_TARGET_ALLOWLIST")
    try:
        port = int(parts.get("port") or DEFAULT_PORT)
    except ValueError as exc:
        raise TargetRefused("the DSN's port is not a number") from exc
    try:
        pinned = outbound.guard.resolve(f"https://{host if ':' not in host else '[' + host + ']'}:{port}")
    except UnsafeURL as exc:
        raise TargetRefused(str(exc)) from exc
    database = parts.get("dbname") or parts.get("user") or ""
    if not allow_any_database and not database.lower().endswith(SAFE_DB_SUFFIXES):
        raise TargetRefused(f"the database name must end in {' or '.join(repr(s) for s in SAFE_DB_SUFFIXES)} (the benchmark creates and drops "
                            "cpsim_bench objects); pass --allow-any-database to override")
    extra = {"hostaddr": pinned.ip} if pinned.ip else {}          # (no address only under a test's offline guard)
    return Target(make_conninfo(dsn, port=port, **extra), database)


_SECRET_ISH = re.compile(r"(postgres(?:ql)?://\S+|password=\S+)", re.I)


def scrub(text: str, dsn: str = "") -> str:
    """Remove the DSN (and anything that looks like a connection URL or a password) from error text."""
    out = text.replace(dsn, "<dsn>") if dsn else text
    try:
        pw = conninfo_to_dict(dsn).get("password") if dsn else ""
        if pw:
            out = out.replace(str(pw), "<hidden>")
    except Exception:  # noqa: S110
        pass
    return _SECRET_ISH.sub("<hidden>", out)


def callback_host_ok(host: str) -> bool:
    """The fake model server's address must be a private or loopback IP literal (AIDB allows plain http only to those)."""
    import ipaddress

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def endpoint_target(url: str):
    """A real chat endpoint (CPSIM_AIDB_CHAT_URL): allow-listed host plus the NetGuard, like any outbound target. Returns the URL."""
    host = urlsplit(url).hostname or ""
    if not host_allowed(host):
        raise TargetRefused(f"the chat endpoint host {host!r} is not in CPSIM_TARGET_ALLOWLIST")
    try:
        return outbound.guard.resolve(url).url
    except UnsafeURL as exc:
        raise TargetRefused(f"chat endpoint refused: {exc}") from exc
