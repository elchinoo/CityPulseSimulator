"""Fleet references: a target that follows the fleet's addresses instead of naming one.

Every AgentPG sandbox URL contains its sandbox id, so recreating an app changes its address. A city, company or workload may therefore
name an app of the fleet instead of a URL: exactly `${CITYPULSE_URL}` (one of the six names below, nothing around it). The reference is
stored as written and looked up in the process environment each time something STARTS, so an app recreated by ecoctl (which injects
fresh values) is followed without an edit. The URL rules (https, or http for this machine, link-local, ...) apply to the resolved URL.
"""
import os
import re

from .errors import SpecError

FLEET_VARS = ("CITYPULSE_URL", "PAYBROKER_URL", "TRUSTDESK_URL", "PAGERLENS_URL", "LLMGW_URL", "IDENTITY_URL", "SHOPFRONT_URL")
LABELS = {"CITYPULSE_URL": "CityPulse", "PAYBROKER_URL": "PayBroker", "TRUSTDESK_URL": "TrustDesk", "PAGERLENS_URL": "PagerLens",
          "LLMGW_URL": "LLM gateway", "IDENTITY_URL": "identity provider", "SHOPFRONT_URL": "ShopFront"}
# the reference each kind of target offers as a one-click choice
CITY_VAR, COMPANY_VAR = "CITYPULSE_URL", "TRUSTDESK_URL"
KIND_VARS = {"paybroker": "PAYBROKER_URL", "llmgw": "LLMGW_URL", "pagerlens": "PAGERLENS_URL", "trustdesk": "TRUSTDESK_URL",
             "identity": "IDENTITY_URL", "shopfront": "SHOPFRONT_URL"}
EXACT = re.compile(r"^\$\{([A-Z][A-Z0-9_]*)\}$")


def is_reference(url) -> bool:
    """True when the text uses the `${` syntax at all (valid or not): it must then pass `check_reference`, never be taken as a URL."""
    return isinstance(url, str) and "${" in url


def reference_name(url) -> str | None:
    """NAME of a well-formed reference to an allow-listed variable, else None."""
    m = EXACT.match(url) if isinstance(url, str) else None
    return m.group(1) if m and m.group(1) in FLEET_VARS else None


def check_reference(url: str) -> str:
    if reference_name(url) is None:
        raise SpecError("target_url may be a fleet reference: exactly ${NAME} with NAME one of " + ", ".join(FLEET_VARS)
                        + " (nothing before or after it, and no other variable); otherwise a plain http(s) URL")
    return url


def value(name: str) -> str:
    """The address the environment holds for a fleet variable ('' when unset)."""
    return os.environ.get(name, "").strip().rstrip("/")


def resolve(url: str, allow_http: bool = False) -> str:
    """A literal URL stays as it is; a reference becomes the environment's value, checked with the same rules as a typed URL."""
    if not is_reference(url):
        return url
    name = reference_name(check_reference(url))
    found = value(name)
    if not found:
        raise SpecError(f"{name} is not set on this app; set the URL by hand or deploy it with ecoctl")
    from .model import check_target

    try:
        return check_target(found, allow_http)
    except SpecError as exc:
        raise SpecError(f"{name} ({found}) cannot be used as a target: {exc}") from exc


def describe(url: str, allow_http: bool = False) -> dict:
    """What an interface shows for a target: the stored text, the address it currently resolves to and whether it does."""
    if not is_reference(url):
        return {"target_url": url, "reference": None, "resolved_url": url, "resolves": bool(url), "problem": ""}
    name = reference_name(url)
    try:
        return {"target_url": url, "reference": name, "resolved_url": resolve(url, allow_http), "resolves": True, "problem": ""}
    except SpecError as exc:
        return {"target_url": url, "reference": name, "resolved_url": "", "resolves": False, "problem": str(exc)}


def status() -> list[dict]:
    """Which of the six variables are set, and to what (URLs are not secrets)."""
    return [{"name": n, "label": LABELS[n], "set": bool(value(n)), "value": value(n)} for n in FLEET_VARS]


def default_target(var: str = CITY_VAR) -> str:
    """What a new target defaults to: the reference when the variable is set on this app, else empty (generate only)."""
    return "${" + var + "}" if value(var) else ""
