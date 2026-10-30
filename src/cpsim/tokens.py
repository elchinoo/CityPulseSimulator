"""The token this app presents to another app of the ecosystem (CityPulse ingest, TrustDesk client API, PagerLens ingest).

A static token (the environment variable a city or company names, its <ENV>_FILE, or the credential saved in the app under that name) wins when set. Otherwise, when the identity
provider is configured (CPSIM_OIDC_ISSUER plus the service client CPSIM_SERVICE_CLIENT_ID / CPSIM_SERVICE_CLIENT_SECRET[_FILE]), a
short-lived token is minted with the client_credentials grant: one TokenSource per callee, asking only for that callee's scope.
"""
import asyncio
import os
from collections.abc import Callable

import httpx
from apgkit.auth import TokenSource
from apgkit.config import read_secret

from . import credentials

CITYPULSE_INGEST = "citypulse.ingest"
TRUSTDESK_CLIENT = "trustdesk.requests.write"
PAGERLENS_INGEST = "pagerlens.ingest"
LLMGW_USE = "llmgw.use"
SHOPFRONT_API = "shopfront.api"

_sources: dict[tuple[str, str, str], TokenSource] = {}


def service_identity() -> tuple[str, str] | None:
    """(issuer, client id) when this app can mint service tokens."""
    issuer = os.environ.get("CPSIM_OIDC_ISSUER", "").strip()
    if issuer and read_secret("CPSIM_SERVICE_CLIENT_SECRET"):
        return issuer, os.environ.get("CPSIM_SERVICE_CLIENT_ID", "cpsim-service")
    return None


def token_source(scope: str) -> TokenSource | None:
    ident = service_identity()
    if ident is None:
        return None
    key = (ident[0], ident[1], scope)
    if key not in _sources:
        _sources[key] = TokenSource(ident[0], ident[1], lambda: read_secret("CPSIM_SERVICE_CLIENT_SECRET"), scope=scope)
    return _sources[key]


def outbound_token(env: str, scope: str, target: str) -> str | Callable[[], str] | None:
    """The static token named by `env` if set (a saved one only for the origin it is bound to), else a callable that returns a fresh
    service token for `scope`, else None. `target` is the address the token is for."""
    static = credentials.get(env, target)
    if static:
        return static
    src = token_source(scope)
    return src.token if src else None


class BearerAuth(httpx.Auth):
    """Adds `Authorization: Bearer <token>` to each request; `token` is a string or a callable (re-evaluated, so it can refresh)."""

    def __init__(self, token: str | Callable[[], str]):
        self._token = token

    async def async_auth_flow(self, request):
        token = self._token if isinstance(self._token, str) else await asyncio.to_thread(self._token)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
