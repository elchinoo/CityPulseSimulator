"""A workload: something the simulator pushes to another app of the ecosystem on a schedule.

Strict validation: a workload is user input. Secrets are never stored: a workload holds only the NAME of the environment variable
that carries its credential (and optionally a publishable key variable for PayBroker).
"""
import os
import re
from dataclasses import dataclass, field

from .kinds import KINDS, MANAGED_ELSEWHERE, kind_of
from .model import ENV_NAME, NAME, SpecError, check_target
from .schedule import Schedule, max_rate, parse_schedule

FIELDS = {"name", "kind", "target_url", "token_env", "publishable_env", "params", "schedule"}
SECRET_WORD = re.compile(r"TOKEN|KEY|SECRET")
# variables that belong to the simulator or its database: a workload may never send them anywhere
FORBIDDEN_ENV = re.compile(r"^(CPSIM_|DATABASE|DB_|PSQL|PG|POSTGRES|AGENTPG|HOME$|PATH$)")


@dataclass
class Workload:
    name: str
    kind: str
    target_url: str = ""                 # empty means "generate only, do not send"
    token_env: str = ""                  # NAME of the variable holding the credential (the kind's default when omitted)
    publishable_env: str = ""            # PayBroker only: NAME of a variable holding a publishable key
    params: dict = field(default_factory=dict)
    schedule: Schedule = field(default_factory=Schedule)

    def to_dict(self) -> dict:
        d = {"name": self.name, "kind": self.kind, "target_url": self.target_url, "token_env": self.token_env,
             "params": self.params, "schedule": self.schedule.to_dict()}
        if self.publishable_env:
            d["publishable_env"] = self.publishable_env
        return d


def check_secret_env(value, what: str = "token_env") -> str:
    """An environment variable NAME that plausibly holds a credential, and is not one of the simulator's own."""
    if (not isinstance(value, str) or not ENV_NAME.match(value) or not SECRET_WORD.search(value) or FORBIDDEN_ENV.match(value)
            or value.endswith("_FILE")):
        raise SpecError(f"{what} is the NAME of an environment variable that holds a credential (it must contain TOKEN, KEY or SECRET, "
                        "e.g. PAYBROKER_SECRET_KEY), never the credential itself; the simulator's own variables are not allowed")
    return value


def allow_http_targets() -> bool:
    return os.environ.get("CPSIM_ALLOW_HTTP_TARGETS", "").strip().lower() in ("1", "true", "yes")


def check_workload_target(url: str) -> str:
    """Like a city's URL (https, or http for this machine); CPSIM_ALLOW_HTTP_TARGETS=true also allows plain http to other hosts, for
    local stand-ins reached by name (such as `http://paybroker:8000` in a Docker network)."""
    return check_target(url, allow_http=allow_http_targets())


def parse_workload(d: dict) -> Workload:
    if not isinstance(d, dict):
        raise SpecError("a workload must be an object")
    unknown = set(d) - FIELDS
    if unknown:
        raise SpecError(f"unknown workload field(s): {', '.join(sorted(unknown))}")
    name = d.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError("name must be lowercase letters, digits and dashes (1 to 40 characters)")
    kind_name = d.get("kind")
    if kind_name in MANAGED_ELSEWHERE:
        raise SpecError(MANAGED_ELSEWHERE[kind_name])
    if kind_name not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)}")
    kind = kind_of(kind_name)
    token_env = d.get("token_env") or kind.token_env
    if kind.token_env:
        token_env = check_secret_env(token_env)
    elif d.get("token_env"):
        raise SpecError(f"a {kind.name} workload takes no token_env")
    pub = d.get("publishable_env") or ""
    if pub:
        if kind.name != "paybroker":
            raise SpecError("publishable_env only applies to paybroker workloads")
        check_secret_env(pub, "publishable_env")
    params = {} if d.get("params") is None else d["params"]
    if not isinstance(params, dict):
        raise SpecError("params must be an object")
    clean = kind.validate(params)
    sched = parse_schedule(d.get("schedule"), min_interval=kind.min_interval, max_batch=kind.max_batch)
    limit = max_rate()
    if kind.name != "trustdesk" and sched.rate() > limit:
        raise SpecError(f"{sched.batch} items every {sched.interval:g} s is {sched.rate():g} items/s, above the ceiling "
                        f"CPSIM_MAX_RATE={limit:g}; lower the batch or raise the interval")
    return Workload(name, kind.name, check_workload_target(d.get("target_url") or ""), token_env, pub, clean, sched)
