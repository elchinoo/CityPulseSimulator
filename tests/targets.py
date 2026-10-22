"""The request rules of the apps the workloads talk to, copied from their repositories (see the notes), so a generator that drifts
from a target's schema fails here. When the target repository is checked out next to this one, `real_*` also load the target's own
validation code and the generators are checked against that too."""
import importlib.util
import re
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

SIBLINGS = Path(__file__).resolve().parents[2]


# ---- PayBroker: paybroker/src/paybroker/api.py (the /v1 models) --------------------------------------------------------
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PbCard(_Body):
    number: str = Field(max_length=25)
    exp_month: int = Field(ge=1, le=12)
    exp_year: int = Field(ge=0, le=2100)
    cvc: str | None = Field(None, max_length=4)


class PbMethod(_Body):
    type: str = Field(pattern="^card$")
    card_token: str = Field(min_length=8, max_length=80)
    cvv_check: bool = True


class PbCustomer(_Body):
    id: str = Field(min_length=1, max_length=64)
    email: str | None = Field(None, max_length=254)
    phone: str | None = Field(None, max_length=30)
    ip_address: str = Field(max_length=45)
    account_age_hours: float | None = Field(None, ge=0, le=1_000_000)


class PbTelemetry(_Body):
    device_fingerprint: str | None = Field(None, max_length=80)
    user_agent: str | None = Field(None, max_length=300)
    checkout_duration_ms: int | None = Field(None, ge=0, le=86_400_000)
    pasted_card_number: bool = False
    ip_country: str | None = Field(None, min_length=2, max_length=2)


class PbShipping(_Body):
    line1: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=100)
    state: str | None = Field(None, max_length=50)
    postal_code: str | None = Field(None, max_length=20)
    country: str | None = Field(None, min_length=2, max_length=2)


class PbAuthorize(_Body):
    merchant_id: str | None = Field(None, max_length=64)
    idempotency_key: str = Field(min_length=8, max_length=128)
    amount: int = Field(ge=1, le=99_999_999)
    currency: str = Field(pattern="^[A-Za-z]{3}$")
    payment_method: PbMethod
    customer: PbCustomer
    telemetry: PbTelemetry = PbTelemetry()
    shipping_address: PbShipping | None = None
    capture_method: str | None = Field(None, pattern="^(manual|automatic)$")


def real_paybroker_models():
    """The models as defined in the PayBroker repository (the source between `class _Body` and `def _merchant`), or skip."""
    path = SIBLINGS / "paybroker/src/paybroker/api.py"
    if not path.exists():
        pytest.skip("paybroker is not checked out next to this repository")
    src = path.read_text()
    segment = src[src.index("class _Body"):src.index("def _merchant(")]
    ns = {"BaseModel": BaseModel, "ConfigDict": ConfigDict, "Field": Field}
    exec(segment, ns)  # noqa: S102 - the sibling repository's own model definitions
    return ns


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


DOC_NETS = ("198.51.100.", "203.0.113.", "192.0.2.", "2001:db8:")      # RFC 5737 and RFC 3849 documentation ranges


# ---- LLM gateway: llm-gateway/API.md and src/llmgw/gateway.py ------------------------------------------------------------
CHAT_FIELDS = {"model", "messages", "max_tokens", "temperature", "top_p", "stop", "response_format", "seed", "presence_penalty",
               "frequency_penalty", "n"}
EMBED_FIELDS = {"model", "input", "dimensions", "encoding_format"}
ROLES = {"system", "user", "assistant", "developer"}


def check_chat(body: dict) -> None:
    assert set(body) <= CHAT_FIELDS, set(body) - CHAT_FIELDS
    assert isinstance(body["model"], str) and 0 < len(body["model"]) <= 80
    assert 1 <= len(body["messages"]) <= 500
    for m in body["messages"]:
        assert set(m) <= {"role", "content", "name"} and m["role"] in ROLES and isinstance(m["content"], str) and m["content"]
    assert isinstance(body["max_tokens"], int) and 1 <= body["max_tokens"] <= 131_072
    assert 0 <= body["temperature"] <= 2


def check_embed(body: dict) -> None:
    assert set(body) <= EMBED_FIELDS, set(body) - EMBED_FIELDS
    items = body["input"] if isinstance(body["input"], list) else [body["input"]]
    assert 1 <= len(items) <= 256 and all(isinstance(s, str) and s for s in items)


def real_gateway_validate():
    """llmgw.gateway.validate from the sibling repository, or skip (it needs the gateway's own dependencies)."""
    path = SIBLINGS / "llm-gateway/src"
    if not path.exists():
        pytest.skip("llm-gateway is not checked out next to this repository")
    sys.path.insert(0, str(path))
    try:
        from llmgw import gateway
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"llmgw does not import here: {type(exc).__name__}")
    finally:
        sys.path.remove(str(path))
    return gateway.validate


# ---- PagerLens: pagerlens/src/pagerlens/ingest.py and api.py (bodies) ------------------------------------------------------
SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
METRIC_NAME = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
SEVERITIES = {"sev1", "sev2", "sev3", "sev4"}
MAX_BATCH = 1000


def check_pagerlens(path: str, body: dict) -> None:
    if path == "/api/ingest/services":
        assert set(body) <= {"name", "owner", "tier", "description", "depends_on"}
        assert SERVICE_NAME.match(body["name"]) and all(SERVICE_NAME.match(d) for d in body["depends_on"]) and len(body["depends_on"]) <= 20
        assert len(body.get("owner", "")) <= 80 and len(body.get("tier", "")) <= 40 and len(body.get("description", "")) <= 300
    elif path == "/api/ingest/deploys":
        assert set(body) <= {"service", "version", "git_sha", "author", "summary", "ts"}
        assert SERVICE_NAME.match(body["service"]) and len(body["version"]) <= 80 and len(body.get("summary", "")) <= 300
    elif path == "/api/ingest/logs":
        assert set(body) == {"service", "events"} and SERVICE_NAME.match(body["service"]) and 1 <= len(body["events"]) <= MAX_BATCH
        for ev in body["events"]:
            assert isinstance(ev["message"], str) and ev["level"] in {"debug", "info", "warn", "warning", "error", "fatal", "critical"}
    elif path == "/api/ingest/metrics":
        assert set(body) == {"service", "points"} and SERVICE_NAME.match(body["service"]) and 1 <= len(body["points"]) <= MAX_BATCH
        for p in body["points"]:
            assert METRIC_NAME.match(p["name"]) and isinstance(p["value"], (int, float)) and abs(p["value"]) <= 1e15
    elif path == "/api/ingest/alerts":
        assert set(body) <= {"service", "name", "summary", "severity", "labels", "source", "investigate"}
        assert SERVICE_NAME.match(body["service"]) and 1 <= len(body["name"]) <= 200 and body["severity"] in SEVERITIES
        assert len(body.get("summary", "")) <= 500 and len(body.get("source", "")) <= 40
    else:
        raise AssertionError(f"unknown PagerLens path {path}")


def real_pagerlens_ingest():
    """pagerlens.ingest from the sibling repository (its own name and metric rules), or skip."""
    path = SIBLINGS / "pagerlens/src/pagerlens/ingest.py"
    if not path.exists():
        pytest.skip("pagerlens is not checked out next to this repository")
    src = path.read_text()
    ns = {"re": re, "timedelta": timedelta}
    exec(src[src.index("MAX_BATCH"):src.index("class IngestError")], ns)  # noqa: S102 - the constants only
    return ns


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
