"""Where readings go: CityPulse over HTTP (with an ingest token), or nowhere."""
import httpx


class Retry(Exception):
    def __init__(self, after: float = 0.0):
        super().__init__("retry")
        self.after = after


class Fatal(Exception):
    """The target refuses us for good (bad token): back off hard and say so."""


class NullSink:
    async def send(self, readings: list[dict]) -> None:
        return None

    async def close(self) -> None:
        return None


class HttpSink:
    """POST /api/readings with an ingest-scope token. Never follows redirects (a redirect would strip the token's
    protection or send it somewhere unintended); the token is only ever sent to the configured URL."""

    def __init__(self, url: str, token: str):
        self.client = httpx.AsyncClient(base_url=url, timeout=20, follow_redirects=False,
                                        headers={"Authorization": f"Bearer {token}"})

    async def send(self, readings: list[dict]) -> None:
        try:
            r = await self.client.post("/api/readings", json={"readings": readings})
        except httpx.HTTPError as exc:
            raise Retry() from exc
        if r.status_code in (401, 403):
            raise Fatal(f"CityPulse refused the token (HTTP {r.status_code}); check the ingest token")
        if r.status_code in (429, 502, 503, 504):
            try:
                after = float(r.headers.get("retry-after", "0") or 0)
            except ValueError:
                after = 0.0
            raise Retry(after)
        if r.status_code >= 300:
            raise Retry()

    async def close(self) -> None:
        await self.client.aclose()
