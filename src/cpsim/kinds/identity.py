"""A load and health probe for the identity provider: `client_credentials` for the simulator's own service client at a
configurable rate, plus discovery and JWKS fetches. Keep it modest (at least 1 s between ticks, at most 20 calls per tick):
every token call is a real sign-in of a service client and is audited by the provider.

The client id and secret are the ones the simulator already uses for outbound calls: `CPSIM_SERVICE_CLIENT_ID` (default
`cpsim-service`) and `CPSIM_SERVICE_CLIENT_SECRET[_FILE]`. The secret is read from the environment, sent only to the provider's
token endpoint (which must be on the same origin as the workload's URL: a discovery document cannot redirect the secret
elsewhere) and never stored or logged. The access tokens that come back are discarded unread.
"""
import os
from urllib.parse import urlsplit

import httpx
from apgkit.config import read_secret

from ..model import SpecError
from .base import (
    BaseSender,
    Result,
    classify_status,
    network_result,
    p_mix,
    pick_weighted,
    unknown_params,
)

PARAMS = {"mix", "scope"}
DEFAULT_MIX = {"token": 60, "discovery": 20, "jwks": 20}
DISCOVERY = "/.well-known/openid-configuration"


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    scope = params.get("scope", "")
    if not isinstance(scope, str) or len(scope) > 200 or any(c in scope for c in "\r\n\"\\"):
        raise SpecError("params.scope must be a short space-separated list of scopes, or empty")
    return {"mix": p_mix(params, "mix", DEFAULT_MIX), "scope": scope.strip()}


def client_credentials() -> tuple[str, str] | None:
    secret = read_secret("CPSIM_SERVICE_CLIENT_SECRET")
    return (os.environ.get("CPSIM_SERVICE_CLIENT_ID", "cpsim-service"), secret) if secret else None


def same_origin(base: str, url: str) -> bool:
    a, b = urlsplit(base), urlsplit(url)
    return (a.scheme, a.netloc) == (b.scheme, b.netloc)


class IdentitySender(BaseSender):
    def __init__(self, params: dict, http, rng, base_url: str = ""):
        super().__init__(params, http, rng)
        self.base = base_url
        self.doc: dict | None = None

    def plan(self, n: int) -> list[str]:
        return [pick_weighted(self.rng, self.params["mix"]) for _ in range(n)]

    async def _discover(self) -> Result | None:
        if self.doc is not None:
            return None
        r, latency, err = await self.http.call("GET", DISCOVERY)
        if r is None:
            return network_result(latency, err)
        if r.status_code != 200:
            res = classify_status(r.status_code, r.headers)
            res.latency = latency
            return res if res.is_error else Result("client_error", latency, error=f"HTTP {r.status_code} for the discovery document")
        self.doc = r.json()
        return None

    def _path(self, key: str, default: str) -> str | None:
        url = (self.doc or {}).get(key)
        if not url:
            return default
        return urlsplit(url).path if same_origin(self.base, url) else None

    async def execute(self, op: str) -> Result:
        if op == "discovery":
            r, latency, err = await self.http.call("GET", DISCOVERY)
            if r is not None and r.status_code == 200:
                self.doc = r.json()
        elif op == "jwks":
            failed = await self._discover()
            if failed:
                return failed
            path = self._path("jwks_uri", "/jwks.json")
            if path is None:
                return Result("auth_error", error="the discovery document names a JWKS on another origin; refusing", fatal=True)
            r, latency, err = await self.http.call("GET", path)
        else:
            creds = client_credentials()
            if not creds:
                return Result("auth_error", error="CPSIM_SERVICE_CLIENT_SECRET is empty", fatal=True)
            failed = await self._discover()
            if failed:
                return failed
            path = self._path("token_endpoint", "/oauth/token")
            if path is None:
                return Result("auth_error", error="the discovery document names a token endpoint on another origin; refusing the secret", fatal=True)
            data = {"grant_type": "client_credentials"}
            if self.params["scope"]:
                data["scope"] = self.params["scope"]
            r, latency, err = await self.http.call("POST", path, data=data, auth=httpx.BasicAuth(*creds))
            if r is not None and r.status_code in (400, 401, 403):
                return Result("auth_error", latency, error=f"HTTP {r.status_code}: the provider refused the service client", fatal=True)
        if r is None:
            return network_result(latency, err)
        res = classify_status(r.status_code, r.headers)
        res.latency = latency
        return res
