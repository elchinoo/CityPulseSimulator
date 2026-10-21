"""A "company": how many simulated employees and how many requests of each type to send to TrustDesk.

Strict validation: a company is user input. The token is never stored, only the NAME of the environment variable.
"""
import math
import re
from dataclasses import asdict, dataclass, field

from .hr_catalog import TYPES
from .model import ENV_NAME, NAME, SpecError, check_target

MAX_PER_TYPE, MAX_TOTAL, MAX_EMPLOYEES = 5000, 20_000, 2000
PREFIX = re.compile(r"^[A-Z][A-Z0-9]{0,7}$")
FIELDS = {"name", "target_url", "token_env", "seed", "employees", "requests", "problem_pct", "withdraw_pct",
          "rate", "concurrency", "employee_prefix"}


@dataclass
class Company:
    name: str
    target_url: str = ""                   # TrustDesk base URL; empty means "generate only, do not send"
    token_env: str = "TRUSTDESK_CLIENT_TOKEN"   # NAME of the variable holding a TrustDesk client token
    seed: int | None = None
    new_employees: int = 10                # hired in the last ~75 days (probation rules)
    tenured_employees: int = 40            # hired 1 to 15 years ago (leave balances, history)
    use_existing: bool = False             # also pick employees that already exist in TrustDesk
    requests: dict[str, int] = field(default_factory=dict)
    problem_pct: float = 25.0              # share of requests built to break a typical policy
    withdraw_pct: float = 3.0              # share withdrawn by the employee after sending
    rate: float = 5.0                      # requests per second, at most
    concurrency: int = 4
    employee_prefix: str = "SIM"

    def total_requests(self) -> int:
        return sum(self.requests.values())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["employees"] = {"new": d.pop("new_employees"), "tenured": d.pop("tenured_employees"),
                          "use_existing": d.pop("use_existing")}
        return d


def _num(value, what: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not lo <= value <= hi:
        raise SpecError(f"{what} must be a number between {lo:g} and {hi:g}")
    return float(value)


def _whole(value, what: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise SpecError(f"{what} must be a whole number from {lo} to {hi}")
    return value


def parse_company(d: dict) -> Company:
    if not isinstance(d, dict):
        raise SpecError("a company must be an object")
    unknown = set(d) - FIELDS
    if unknown:
        raise SpecError(f"unknown company field(s): {', '.join(sorted(unknown))}")
    name = d.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError("name must be lowercase letters, digits and dashes (1 to 40 characters)")
    token_env = d.get("token_env", "TRUSTDESK_CLIENT_TOKEN")
    if not isinstance(token_env, str) or not ENV_NAME.match(token_env) or "TOKEN" not in token_env:
        raise SpecError("token_env is the NAME of an environment variable that holds a token and contains 'TOKEN' "
                        "(e.g. TRUSTDESK_CLIENT_TOKEN), never the token itself")
    seed = d.get("seed")
    if seed is not None:
        seed = _whole(seed, "seed", 0, 2**32 - 1)
    emp = d.get("employees", {})
    if not isinstance(emp, dict) or set(emp) - {"new", "tenured", "use_existing"}:
        raise SpecError("employees must be an object with new, tenured and use_existing")
    new = _whole(emp.get("new", 10), "employees.new", 0, MAX_EMPLOYEES)
    tenured = _whole(emp.get("tenured", 40), "employees.tenured", 0, MAX_EMPLOYEES)
    if new + tenured > MAX_EMPLOYEES:
        raise SpecError(f"at most {MAX_EMPLOYEES} new employees in total")
    use_existing = bool(emp.get("use_existing", False))
    reqs = d.get("requests", {})
    if not isinstance(reqs, dict):
        raise SpecError("requests must map a request type to a count")
    clean: dict[str, int] = {}
    for key, n in reqs.items():
        if key not in TYPES:
            raise SpecError(f"unknown request type {key!r}")
        n = _whole(n, f"requests.{key}", 0, MAX_PER_TYPE)
        if n:
            clean[key] = n
    if sum(clean.values()) > MAX_TOTAL:
        raise SpecError(f"at most {MAX_TOTAL} requests per run")
    if clean and new + tenured == 0 and not use_existing:
        raise SpecError("requests need employees: add new or tenured employees, or tick use_existing")
    prefix = d.get("employee_prefix", "SIM")
    if not isinstance(prefix, str) or not PREFIX.match(prefix):
        raise SpecError("employee_prefix must be 1 to 8 capital letters or digits, starting with a letter")
    return Company(name, check_target(d.get("target_url") or ""), token_env, seed, new, tenured, use_existing, clean,
                   _num(d.get("problem_pct", 25), "problem_pct", 0, 100), _num(d.get("withdraw_pct", 3), "withdraw_pct", 0, 30),
                   _num(d.get("rate", 5), "rate", 0.1, 200), _whole(d.get("concurrency", 4), "concurrency", 1, 16), prefix)
