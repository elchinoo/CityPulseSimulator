"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of CityPulse Simulator.

Permissions are what the code checks (`sim:run`); roles (given to people by the identity provider) and scopes (given to
services, local API tokens and the static key) are mapped onto them here, in one place.
"""
import hmac

from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, Principal, from_env
from apgkit.config import env_bool, read_secret
from apgkit.db import Database

from . import __version__

APP = "cpsim"
COOKIE = "cs_session"
CSRF_HEADER = "x-cpsim-csrf"

db = Database()
metrics = metrics_mod.Metrics(APP, __version__)

ALL = frozenset({"*"})
PERMISSIONS = {
    "sim:read": "see cities, companies, the catalogs, previews, statistics and run results",
    "sim:run": "start, stop and boost cities; run, stop and refresh companies (sends readings and requests to other apps)",
    "sim:edit": "create, change and delete cities and companies",
    "settings:manage": "admin password and API tokens",
    "metrics:scrape": "scrape /metrics",
}
ROLE_PERMISSIONS = {
    "admin": ALL,
    "operator": frozenset({"sim:read", "sim:run", "sim:edit"}),
    "viewer": frozenset({"sim:read"}),
}
SCOPE_PERMISSIONS = {
    # local API tokens
    "read": frozenset({"sim:read"}), "metrics": frozenset({"metrics:scrape"}), "full": ALL,
    # scopes the identity provider can grant to services
    "cpsim.read": frozenset({"sim:read"}), "cpsim.metrics": frozenset({"metrics:scrape"}),
}
fed = from_env("CPSIM", APP)
APP_AUTH = AppAuth(APP, fed.client_id, ROLE_PERMISSIONS, SCOPE_PERMISSIONS)

local = LocalAuth(APP_AUTH, env_prefix="CPSIM", token_prefix="cs_", token_scopes={"read", "metrics", "full"})


class SimAuthenticator(Authenticator):
    """The kit's front door, plus the simulator's static CPSIM_METRICS_TOKEN: it may scrape /metrics and nothing else."""

    @staticmethod
    def metrics_token() -> str:
        return read_secret("CPSIM_METRICS_TOKEN", "/etc/cpsim/metrics_token")

    def _bearer(self, raw: str) -> Principal | None:
        token = self.metrics_token()
        if token and hmac.compare_digest(raw.encode(), token.encode()):
            return Principal("static", "metrics-token", frozenset({"metrics:scrape"}), name="metrics token")
        return super()._bearer(raw)


auth = SimAuthenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier,
                        allow_anonymous=lambda: env_bool("CPSIM_ALLOW_ANONYMOUS"))
public_url_env = "CPSIM_PUBLIC_URL"
