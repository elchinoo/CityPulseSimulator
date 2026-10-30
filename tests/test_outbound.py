"""The tokens this app presents to CityPulse, TrustDesk and PagerLens: static tokens win; otherwise one service token per callee."""
import asyncio
import functools

import httpx
import pytest
from apgkit.auth import TokenSource

from cpsim import reporter, tokens
from cpsim.hr_engine import TrustDeskClient
from cpsim.model import SpecError
from cpsim.runtime import Runtime
from cpsim.sinks import HttpSink

ISS = "https://id.example.org"


class Provider:
    def __init__(self):
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json={"issuer": ISS, "token_endpoint": f"{ISS}/oauth/token"})
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        self.requests.append({"scope": form["scope"].replace("%2E", ".").replace("+", " "), "auth": request.headers["authorization"]})
        n = len(self.requests)
        return httpx.Response(200, json={"access_token": f"minted-{form['scope']}-{n}", "expires_in": 300, "token_type": "Bearer"})

    def http(self):
        return lambda: httpx.Client(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def provider(monkeypatch):
    p = Provider()
    monkeypatch.setenv("CPSIM_OIDC_ISSUER", ISS)
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET", "s3cret-value")  # pragma: allowlist secret
    monkeypatch.delenv("CPSIM_SERVICE_CLIENT_ID", raising=False)
    monkeypatch.setattr(tokens, "TokenSource", functools.partial(TokenSource, http=p.http()))
    monkeypatch.setattr(tokens, "_sources", {})
    for v in ("CITYPULSE_INGEST_TOKEN", "TRUSTDESK_CLIENT_TOKEN", "PAGERLENS_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    return p


def test_without_any_credential_there_is_no_token(monkeypatch):
    monkeypatch.delenv("CITYPULSE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("CPSIM_OIDC_ISSUER", raising=False)
    assert tokens.outbound_token("CITYPULSE_INGEST_TOKEN", tokens.CITYPULSE_INGEST, "https://citypulse.example") is None


def test_a_static_token_wins(provider, monkeypatch):
    monkeypatch.setenv("CITYPULSE_INGEST_TOKEN", "static-ingest")
    assert tokens.outbound_token("CITYPULSE_INGEST_TOKEN", tokens.CITYPULSE_INGEST, "https://citypulse.example") == "static-ingest"
    assert provider.requests == []


def test_one_token_per_callee_each_with_only_its_scope(provider):
    cp = tokens.outbound_token("CITYPULSE_INGEST_TOKEN", tokens.CITYPULSE_INGEST, "https://citypulse.example")
    td = tokens.outbound_token("TRUSTDESK_CLIENT_TOKEN", tokens.TRUSTDESK_CLIENT, "https://td.example")
    pl = tokens.outbound_token("PAGERLENS_TOKEN", tokens.PAGERLENS_INGEST, "https://pl.example")
    assert callable(cp) and cp() == cp() and td().startswith("minted-trustdesk.requests.write") and pl().startswith("minted-pagerlens.ingest")
    assert cp().startswith("minted-citypulse.ingest")
    assert sorted(r["scope"] for r in provider.requests) == ["citypulse.ingest", "pagerlens.ingest", "trustdesk.requests.write"]   # cached: 3 calls only
    assert all(r["auth"].startswith("Basic ") for r in provider.requests)


def test_the_service_client_id_defaults_and_is_configurable(provider, monkeypatch):
    assert tokens.service_identity() == (ISS, "cpsim-service")
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_ID", "my-sim")
    assert tokens.service_identity() == (ISS, "my-sim")
    monkeypatch.delenv("CPSIM_SERVICE_CLIENT_SECRET")
    assert tokens.service_identity() is None            # no secret: the identity provider is not used for outbound calls


async def test_the_bearer_header_carries_the_minted_token_and_refreshes():
    seen = []

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={})

    n = iter(range(1, 10))
    async with httpx.AsyncClient(base_url="https://x.example", transport=httpx.MockTransport(handler),
                                 auth=tokens.BearerAuth(lambda: f"t{next(n)}")) as c:
        await c.get("/a")
        await c.get("/b")
    assert seen == ["Bearer t1", "Bearer t2"]
    async with httpx.AsyncClient(base_url="https://x.example", transport=httpx.MockTransport(handler), auth=tokens.BearerAuth("fixed")) as c:
        await c.get("/a")
    assert seen[-1] == "Bearer fixed"


class Store:
    def __init__(self, **spec):
        self.spec = spec

    def cities(self):
        return {"testville": {"name": "testville", "groups": [{"type": "temperature", "count": 2, "protocol": "modbus_tcp", "frequency": 5}], **self.spec}}

    def companies(self):
        return {"acme": {"name": "acme", "employees": {"new": 1, "tenured": 0}, "requests": {"vacation": 1}, **self.spec}}


def test_the_runtime_mints_a_token_when_the_environment_variable_is_empty(provider):
    rt = Runtime(Store(target_url="https://citypulse.example"))
    sink = rt._sink(rt.get("testville"))
    assert isinstance(sink, HttpSink)
    asyncio.run(sink.close())
    assert [r["scope"] for r in provider.requests] == []        # minted lazily, on the first request


def test_the_runtime_still_names_the_variable_when_nothing_is_configured(monkeypatch):
    monkeypatch.delenv("CPSIM_OIDC_ISSUER", raising=False)
    monkeypatch.delenv("CITYPULSE_INGEST_TOKEN", raising=False)
    rt = Runtime(Store(target_url="https://citypulse.example"))
    with pytest.raises(SpecError, match="CITYPULSE_INGEST_TOKEN"):
        rt._sink(rt.get("testville"))


async def test_the_trustdesk_client_authenticates_with_a_minted_token(provider):
    td = tokens.outbound_token("TRUSTDESK_CLIENT_TOKEN", tokens.TRUSTDESK_CLIENT, "https://td.example")
    seen = []
    client = TrustDeskClient("https://trustdesk.example", td)
    client.http._transport = httpx.MockTransport(lambda r: (seen.append(r.headers["authorization"]), httpx.Response(200, json={}))[1])
    await client.call("GET", "/api/v1/ping")
    await client.close()
    assert seen == ["Bearer minted-trustdesk.requests.write-1"]


def test_the_reporter_starts_with_a_minted_pagerlens_token(provider, monkeypatch):
    monkeypatch.setenv("PAGERLENS_URL", "https://pagerlens.example")
    rep = reporter.maybe_start(None)
    assert rep is not None
    asyncio.run(rep.client.aclose())
    monkeypatch.delenv("CPSIM_SERVICE_CLIENT_SECRET")
    assert reporter.maybe_start(None) is None                   # neither a static token nor a service identity


def test_the_self_report_follows_the_workload_rule_for_plain_http(monkeypatch, capsys):
    monkeypatch.setenv("PAGERLENS_TOKEN", "ingest-token")
    cases = [("http://localhost:8080", False, True), ("http://127.0.0.1:9", False, True), ("https://pagerlens.example", False, True),
             ("http://pagerlens:8000", False, False), ("http://localhost.evil.example", False, False),
             ("http://pagerlens:8000", True, True), ("http://169.254.169.254", True, False), ("http://pagerlens:8000", "off", False)]
    for url, allow, started in cases:
        monkeypatch.setenv("PAGERLENS_URL", url)
        if allow in (True, "on"):
            monkeypatch.setenv("CPSIM_ALLOW_HTTP_TARGETS", "true")
        else:
            monkeypatch.delenv("CPSIM_ALLOW_HTTP_TARGETS", raising=False)
        rep = reporter.maybe_start(None)
        assert (rep is not None) is started, (url, allow)
        if rep:
            asyncio.run(rep.client.aclose())
        else:
            assert "refusing PAGERLENS_URL" in capsys.readouterr().err
