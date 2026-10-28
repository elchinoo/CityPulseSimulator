"""This app's instances of the shared blocks (agentpg-kit), and the authorization model of CityPulse Simulator.

Permissions are what the code checks (`sim:run`); roles (given to people by the identity provider) and scopes (given to
services, local API tokens and the static key) are mapped onto them here, in one place.
"""

from apgkit import metrics as metrics_mod
from apgkit.auth import AppAuth, Authenticator, LocalAuth, Principal, from_env
from apgkit.config import env_bool, read_secret
from apgkit.db import Database
from apgkit.vault import Vault

from . import __version__

APP = "cpsim"
COOKIE = "cs_session"
CSRF_HEADER = "x-cpsim-csrf"

db = Database()
vault = Vault("CPSIM")
metrics = metrics_mod.Metrics(APP, __version__)
metrics.add_insights_collector(lambda: db.connect(retries=1))     # db_insights_* gauges (EDB query statistics, cached; 0 on plain Postgres)

ALL = frozenset({"*"})
PERMISSIONS = {
    "sim:read": "see cities, companies, workloads, the catalogs, previews, statistics and run results",
    "sim:run": "start, stop and boost cities; run, stop and refresh companies; start, stop and run workloads once (sends readings, "
               "requests and traffic to other apps)",
    "sim:edit": "create, change and delete cities, companies and workloads",
    "settings:manage": "admin password, API tokens and the credentials saved for the apps it drives",
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

local = LocalAuth(APP_AUTH, env_prefix="CPSIM", token_prefix="cs_", token_scopes={"read", "metrics", "full"}, vault=vault)


def metrics_token() -> str:
    return read_secret("CPSIM_METRICS_TOKEN", "/etc/cpsim/metrics_token")


# the static CPSIM_METRICS_TOKEN may scrape /metrics and nothing else
auth = Authenticator(APP_AUTH, local, db, cookie_name=COOKIE, verifier=fed.verifier, allow_anonymous=lambda: env_bool("CPSIM_ALLOW_ANONYMOUS"),
                     static_tokens=[(metrics_token, Principal("static", "metrics-token", frozenset({"metrics:scrape"}), name="metrics token"))])
public_url_env = "CPSIM_PUBLIC_URL"
