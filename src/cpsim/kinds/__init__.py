"""The workload kinds: what each one sends, how it authenticates, which parameters it takes and what the UI should ask for.

Each kind module has a pure generator and a sender (see `base.py`); this registry ties them to credentials and to the schedule
limits, and describes the parameters for the interface (`fields`) and a few ready-made `presets`.
"""
import random
from collections.abc import Callable
from dataclasses import dataclass, field

from apgkit.config import read_secret

from ..model import SpecError
from ..tokens import LLMGW_USE, PAGERLENS_INGEST, outbound_token
from . import identity, llmgw, pagerlens, paybroker, trustdesk
from .base import Http


def _f(key, label, type_="number", hint="", **kw) -> dict:
    return {"key": key, "label": label, "type": type_, "hint": hint, **kw}


@dataclass
class Kind:
    name: str
    label: str
    description: str
    validate: Callable[[dict], dict]
    token_env: str = ""                  # the default variable that holds the credential ("" when the kind needs none)
    scope: str | None = None             # the identity provider scope used when that variable is empty
    min_interval: float = 0.2
    max_batch: int = 500
    schedule: dict = field(default_factory=dict)      # defaults offered for a new workload
    fields: list[dict] = field(default_factory=list)
    presets: list[dict] = field(default_factory=list)
    url_hint: str = ""
    env_hint: str = ""

    def defaults(self) -> dict:
        """The parameters of a fresh workload (empty for a kind with a required parameter, like the company of a trustdesk workload)."""
        try:
            return self.validate({})
        except SpecError:
            return {}


PB_MIX = [_f(f"mix.{k}", f"{label} (%)", "number", hint, min=0, max=100) for k, label, hint in (
    ("regular", "Regular customers", "Each has one test card of their own."), ("decline", "Issuer declines", "Expected: 402 declines."),
    ("three_ds", "3-D Secure card", "Needs a challenge; see complete_3ds_pct."), ("connector_failure", "Connector failure card",
                                                                                   "The first connector fails; the cascade recovers."),
    ("card_testing", "Card testing burst", "Tiny payments with a new card each time from one device."),
    ("account_takeover", "Account takeover", "A known customer, a new device abroad, a big pasted order."))]

KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("paybroker", "PayBroker payments", "Payments through PayBroker's merchant API: regular customers, declines, 3-D Secure, "
         "connector failures, card testing and account takeover, with follow-ups.", paybroker.validate, "PAYBROKER_SECRET_KEY",
         schedule={"interval": 2, "batch": 3, "concurrency": 4}, url_hint="https://your-paybroker (the /v1 API is on this host)",
         env_hint="A merchant SECRET key (sk_test_...). Optionally a publishable key variable to tokenize like a browser.",
         fields=PB_MIX + [
             _f("customers", "Customer pool", "number", "Empty: sized to the rate. One unique test card per customer.", min=1, max=5000),
             _f("capture_pct", "Captured later (%)", "number", "Share of authorized payments captured on a later tick.", min=0, max=100),
             _f("refund_pct", "Refunded (%)", "number", "Share of captured payments refunded.", min=0, max=100),
             _f("complete_3ds_pct", "3-D Secure completed (%)", "number", "Share of challenges completed (succeeded).", min=0, max=100),
             _f("capture_method", "Capture method", "select", "Empty: the merchant's default.", options=["", "automatic", "manual"]),
             _f("seed", "Random seed", "number", "Empty: random each run.", min=0)],
         presets=[{"id": "steady", "label": "Steady shop: 3 payments every 2 s", "schedule": {"interval": 2, "batch": 3},
                   "params": {"capture_pct": 50, "refund_pct": 10, "complete_3ds_pct": 80}},
                  {"id": "attack", "label": "Fraud attack: card testing and takeovers every 2 s", "schedule": {"interval": 2, "batch": 5},
                   "params": {"mix": {"regular": 30, "decline": 5, "three_ds": 0, "connector_failure": 0, "card_testing": 45,
                                      "account_takeover": 20}}}]),
    Kind("llmgw", "LLM gateway calls", "Chat and embedding calls through the LLM gateway's data plane, with rotating synthetic prompts.",
         llmgw.validate, "LLMGW_CLIENT_KEY", LLMGW_USE, schedule={"interval": 5, "batch": 2, "concurrency": 2},
         url_hint="https://your-llm-gateway", env_hint="A client key (lg_...). When empty, a service token with scope llmgw.use is used.",
         fields=[_f("chat_models", "Chat model aliases", "list", "Comma separated; the gateway's aliases (echo-chat works out of the box)."),
                 _f("embedding_models", "Embedding model aliases", "list", "Comma separated (echo-embed works out of the box)."),
                 _f("chat_pct", "Chat share (%)", "number", "The rest are embedding calls.", min=0, max=100),
                 _f("max_tokens_min", "Smallest max_tokens", "number", "", min=1, max=4096),
                 _f("max_tokens_max", "Largest max_tokens", "number", "", min=1, max=4096),
                 _f("embed_inputs", "Most inputs per embedding call", "number", "", min=1, max=32),
                 _f("temperature", "Temperature", "number", "", min=0, max=2, step=0.1),
                 _f("seed", "Random seed", "number", "Empty: random each run.", min=0)],
         presets=[{"id": "light", "label": "Light: 2 calls every 5 s", "schedule": {"interval": 5, "batch": 2}, "params": {}}]),
    Kind("pagerlens", "PagerLens telemetry", "Metrics, logs, deploys and alerts of a small fictional shop, with a failure you can switch on.",
         pagerlens.validate, "PAGERLENS_TOKEN", PAGERLENS_INGEST, schedule={"interval": 5, "batch": 3, "concurrency": 4},
         url_hint="https://your-pagerlens", env_hint="A PagerLens ingest token. When empty, a service token with scope pagerlens.ingest is used.",
         fields=[_f("prefix", "Service name prefix", "text", "Namespaces the fictional services, e.g. sim-checkout."),
                 _f("scenario", "Scenario", "select", "Healthy baseline, or a failure aimed at the target service.", options=list(pagerlens.SCENARIOS)),
                 _f("target_service", "Target service", "select", "", options=list(pagerlens.SYSTEM)),
                 _f("alert", "Raise an alert for the failure", "bool", ""),
                 _f("alert_after", "Alert after (ticks)", "number", "Ticks of failure before the alert is raised.", min=0, max=1000),
                 _f("seed", "Random seed", "number", "Empty: random each run.", min=0)],
         presets=[{"id": "baseline", "label": "Healthy baseline every 5 s", "schedule": {"interval": 5, "batch": 3}, "params": {}},
                  {"id": "bad-deploy", "label": "Bad deploy of checkout (incident opens)", "schedule": {"interval": 5, "batch": 3},
                   "params": {"scenario": "bad_deploy"}}]),
    Kind("trustdesk", "TrustDesk recurring requests", "Runs a saved company (its employees and requests) again and again on a schedule.",
         trustdesk.validate, "", min_interval=5.0, max_batch=1, schedule={"interval": 60, "batch": 1, "concurrency": 1},
         env_hint="The company names its own URL and token variable.",
         fields=[_f("company", "Company", "text", "The name of a company (Companies tab, or the `companies:` list of a file).")],
         presets=[{"id": "hourly", "label": "A run every 60 s", "schedule": {"interval": 60}, "params": {}}]),
    Kind("identity", "Identity provider probe", "Load and health probe: client_credentials for the simulator's own service client, "
         "plus discovery and JWKS fetches. Modest by design.", identity.validate, "", min_interval=1.0, max_batch=20,
         schedule={"interval": 10, "batch": 2, "concurrency": 2}, url_hint="https://your-identity",
         env_hint="Uses CPSIM_SERVICE_CLIENT_ID / CPSIM_SERVICE_CLIENT_SECRET.",
         fields=[_f("mix.token", "Token calls (%)", "number", "client_credentials grants.", min=0, max=100),
                 _f("mix.discovery", "Discovery fetches (%)", "number", "", min=0, max=100),
                 _f("mix.jwks", "JWKS fetches (%)", "number", "", min=0, max=100),
                 _f("scope", "Scope to request", "text", "Optional, space separated.")],
         presets=[{"id": "probe", "label": "Probe: 2 calls every 10 s", "schedule": {"interval": 10, "batch": 2}, "params": {}}]),
)}
# Cities are the CityPulse workload (see presets.py): they stream on their own schedule (`poll_interval`, group `frequency`).
MANAGED_ELSEWHERE = {"citypulse": "Cities stream to CityPulse by themselves: create a city (the Live feed preset) under Cities. "
                                  "In a headless file `kind: citypulse` is expanded to a city."}


def kind_of(name) -> Kind:
    if name in MANAGED_ELSEWHERE:
        raise SpecError(MANAGED_ELSEWHERE[name])
    if name not in KINDS:
        raise SpecError(f"kind must be one of {', '.join(KINDS)}")
    return KINDS[name]


def credential(wl):
    """The credential for a workload's target: the static value of its variable, else a service token for the kind's scope.

    Raises SpecError when the target needs one and neither exists. Returns None for kinds that need no bearer credential."""
    kind = KINDS[wl.kind]
    if kind.name == "identity" and wl.target_url and not identity.client_credentials():
        raise SpecError("CPSIM_SERVICE_CLIENT_SECRET is empty: the identity probe needs the simulator's service client")
    if not kind.token_env or not wl.target_url:
        return None
    token = outbound_token(wl.token_env, kind.scope) if kind.scope else (read_secret(wl.token_env) or None)
    if not token:
        extra = (f" (or configure the service client of the identity provider for scope {kind.scope}: see CPSIM_SERVICE_CLIENT_SECRET)"
                 if kind.scope else "")
        raise SpecError(f"the environment variable {wl.token_env} is empty: set it for {wl.target_url}{extra}")
    return token


def build_sender(wl, *, token=None, transport=None, rng: random.Random | None = None, company=None, start_run=None):
    """The sender for a workload (no network until it sends). `token` comes from `credential(wl)`."""
    params = wl.params
    rng = rng or random.Random(params.get("seed"))
    if wl.kind == "trustdesk":
        return trustdesk.TrustdeskSender(params, company, start_run)
    http = Http(wl.target_url, token, transport=transport) if wl.target_url else None
    if wl.kind == "paybroker":
        pub = None
        if wl.publishable_env and wl.target_url:
            key = read_secret(wl.publishable_env)
            if not key:
                raise SpecError(f"the environment variable {wl.publishable_env} is empty: set it to a publishable key")
            pub = Http(wl.target_url, key, transport=transport)
        return paybroker.PaybrokerSender(params, http, rng, rate=wl.schedule.rate(), publishable_http=pub)
    if wl.kind == "llmgw":
        return llmgw.LlmgwSender(params, http, rng)
    if wl.kind == "pagerlens":
        return pagerlens.PagerlensSender(params, http, rng)
    if wl.kind == "identity":
        return identity.IdentitySender(params, http, rng, wl.target_url)
    raise SpecError(f"unknown kind {wl.kind!r}")
