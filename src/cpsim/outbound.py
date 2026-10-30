"""Where the simulator may send requests, and which environment variables it may send as credentials.

Targets: every outbound target (a city's, a company's, a workload's, the PagerLens reporter's and the PayBroker address a shop publishes)
must be allow-listed and must pass the kit's NetGuard when something starts. The allow-list is the hosts of the ecosystem URLs this app
was given by environment (CITYPULSE_URL, TRUSTDESK_URL, PAYBROKER_URL, SHOPFRONT_URL, PAGERLENS_URL, LLMGW_URL, IDENTITY_URL) plus
`CPSIM_TARGET_ALLOWLIST` (comma or space separated hostnames; `.example.org` or `*.example.org` matches that domain and its subdomains).
NetGuard refuses private, loopback and link-local addresses; `CPSIM_ALLOW_PRIVATE_URLS=true` allows private and loopback ones (link-local
is always refused), for local stand-ins.

Credentials: a variable NAME that becomes a bearer token must be on an explicit list: the service-token variables the kinds use by
default plus `CPSIM_TOKEN_ENV_ALLOWLIST`. The app's own secrets (LLM_API_KEY, anything ending in _ENCRYPTION_KEY, ...) can never be named.
"""
import os
import re
from urllib.parse import urlsplit

from apgkit.netguard import NetGuard, UnsafeURL

from . import fleet
from .errors import SpecError

guard = NetGuard("CPSIM_ALLOW_PRIVATE_URLS")

# the variables the simulator's kinds use by default, and the ecosystem app each one belongs to
DEFAULT_TOKEN_ENVS = {
    "CITYPULSE_INGEST_TOKEN": "CITYPULSE_URL", "TRUSTDESK_CLIENT_TOKEN": "TRUSTDESK_URL", "PAGERLENS_TOKEN": "PAGERLENS_URL",
    "PAYBROKER_SECRET_KEY": "PAYBROKER_URL", "PAYBROKER_PUBLISHABLE_KEY": "PAYBROKER_URL", "LLMGW_CLIENT_KEY": "LLMGW_URL",
    "SHOPFRONT_TOKEN": "SHOPFRONT_URL",
}
_EXTRA_OK = re.compile(r"^(?!LLM_API_KEY$)(?!.*_ENCRYPTION_KEY)(?!CPSIM_)(?!DATABASE)(?!DB_)(?!PG)(?!PSQL)(?!POSTGRES)(?!AGENTPG)[A-Z][A-Z0-9_]{0,63}$")


def _words(value: str) -> list[str]:
    return [w for w in re.split(r"[\s,]+", value.strip()) if w]


def allowed_token_envs() -> set[str]:
    """The names a workload, city or company may send as a credential here."""
    extra = {w for w in _words(os.environ.get("CPSIM_TOKEN_ENV_ALLOWLIST", "")) if _EXTRA_OK.match(w) and not w.endswith("_FILE")}
    return set(DEFAULT_TOKEN_ENVS) | extra


def check_token_env(name: str, what: str = "token_env") -> str:
    if name not in allowed_token_envs():
        listed = ", ".join(sorted(allowed_token_envs()))
        raise SpecError(f"{what} {name!r} is not an allowed credential variable on this app; allowed: {listed}. "
                        "An operator can add a variable with CPSIM_TOKEN_ENV_ALLOWLIST (never LLM_API_KEY or an encryption key).")
    return name


def allowlist() -> list[str]:
    """Hostnames (and `.suffix` patterns) a target may have."""
    out = [w.lower().lstrip("*") if w.startswith("*.") else w.lower() for w in _words(os.environ.get("CPSIM_TARGET_ALLOWLIST", ""))]
    for var in fleet.FLEET_VARS:
        host = (urlsplit(fleet.value(var)).hostname or "").lower()
        if host:
            out.append(host)
    return out


def host_allowed(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a or (a.startswith(".") and host.endswith(a)) for a in allowlist())


def check_allowed(url: str) -> str:
    """The pure part (no DNS): the URL's host must be allow-listed."""
    host = (urlsplit(url).hostname or "")
    if not host_allowed(host):
        raise SpecError(f"{host or url!r} is not an allowed target on this app: it is not the address of an ecosystem app given to it "
                        "(CITYPULSE_URL, TRUSTDESK_URL, PAYBROKER_URL, ...) and not in CPSIM_TARGET_ALLOWLIST")
    return url


def check_url(url: str) -> str:
    """Allow-list, then the NetGuard (resolves the host: private and link-local addresses are refused unless CPSIM_ALLOW_PRIVATE_URLS)."""
    check_allowed(url)
    try:
        return guard.check(url)
    except UnsafeURL as exc:
        raise SpecError(f"target refused: {exc}") from exc


def origin_of(url: str) -> str:
    """scheme://host[:port] in canonical form ('' when it is not a URL); the unit a saved credential is bound to."""
    try:
        parts = urlsplit((url or "").strip())
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        return ""
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return ""
    if port is not None and (scheme, port) in {("https", 443), ("http", 80)}:
        port = None
    host = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host}{':' + str(port) if port else ''}"


def resolve_origin(bound: str) -> str:
    """A stored origin is a literal origin or a fleet reference (`${PAYBROKER_URL}`), which follows the environment."""
    if fleet.is_reference(bound):
        name = fleet.reference_name(bound)
        return origin_of(fleet.value(name)) if name else ""
    return origin_of(bound)
