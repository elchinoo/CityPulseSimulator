"""Simulated shoppers for ShopFront's storefront API (`/api/v1`): browse and search, fill a cart, check out.

Every item is ONE shopper session of several calls, decided from a seeded RNG (`plan`, pure) and played against the live catalog
(`execute`). Personas, in the shares of `mix`: `browse` (categories, a product list, maybe a search, product pages, then leaves),
`abandon` (the same, then fills a cart and leaves) and `buy` (then checks out). Buyers pay with a fresh PayBroker TEST card from the
reserved `999999` range, tokenized the way the shop's browser does it: the publishable key the shop itself publishes in
`/api/v1/config` goes to PayBroker's `/v1/vault/tokens`, and only the token goes to the shop (the checkout body has no card field).
A share of buyers uses the behaviour prefixes (decline, 3-D Secure, connector failure; they need a PayBroker that maps them, see
`paybroker.py`); 3-D Secure challenges are completed on PayBroker's hosted test page, then the order is asked where it stands.

Authentication: with a service token (scope `shop` locally, `shopfront.api` from the identity provider) the checkout states the
customer's `client` data (address and account age) and is exempt from the per-address checkout limit. With none (`params.guest`,
or no credential at all) the shoppers are plain guests: carts travel in `X-Cart-Token`, writes carry the browser CSRF header, no
`client` data is sent, and the shop's limit of 20 checkouts per 10 minutes per address applies (counted as `rate_limited`).

Expected outcomes, not errors: declines, fraud declines, 3-D Secure failures, out of stock, rate limits, and `checkout_skipped`
when the shop has no PayBroker connection (shoppers still browse and fill carts). Every identity is fictional: example.com e-mail
addresses, documentation-range IP addresses, test cards only.
"""
import asyncio
import random
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlsplit

from ..model import SpecError, check_target
from .base import (
    BaseSender,
    Http,
    Result,
    classify_status,
    network_result,
    p_bool,
    p_int,
    p_mix,
    p_num,
    pick_weighted,
    unknown_params,
)
from .paybroker import SCENARIO_PREFIX, make_test_card

PERSONAS = ("browse", "abandon", "buy")
DEFAULT_MIX = {"browse": 55, "abandon": 25, "buy": 20}
PARAMS = {"mix", "search_pct", "decline_pct", "three_ds_pct", "connector_failure_pct", "complete_3ds_pct", "max_lines", "max_qty",
          "guest", "paybroker_url", "seed"}
FIRST = ["Ada", "Grace", "Linus", "Margaret", "Alan", "Radia", "Ken", "Barbara", "Dennis", "Frances", "Tim", "Hedy"]
LAST = ["Stone", "Rivera", "Okafor", "Lindqvist", "Tanaka", "Moreau", "Silva", "Novak", "Fischer", "Costa", "Hughes", "Bauer"]
# country: (documentation address range, line1, city, region, postal code)
HOMES = {
    "US": ("198.51.100", "12 Maple Street", "Springfield", "IL", "62704"), "GB": ("203.0.113", "8 Mill Lane", "Leeds", "", "LS1 4AP"),
    "DE": ("192.0.2", "Bahnhofstrasse 4", "Hamburg", "", "20095"), "CA": ("198.51.100", "77 King Street", "Toronto", "ON", "M5H 2N2"),
}
WORDS = ["kit", "cable", "lamp", "bag", "bottle", "notebook", "headphones", "charger", "mug", "backpack", "desk", "keyboard", "shirt",
         "speaker", "camera", "watch", "jacket", "pillow"]
SORTS = ["newest", "price_asc", "price_desc", "name"]
REFRESH_EVERY = 100          # sessions between two reads of the catalog and the payment configuration


@dataclass
class Shopper:
    seq: int
    persona: str
    seed: int                    # decides, at execute time, which products and categories this shopper picks
    search: str | None
    cart: str                    # "cart" (server-side cart, X-Cart-Token) or "inline" (items in the checkout body)
    lines: int
    max_qty: int
    scenario: str                # regular | decline | three_ds | connector_failure (buyers only)
    name: str
    email: str
    country: str
    ip: str
    device: str
    age_hours: int
    complete_3ds: bool


@dataclass
class Info:
    products: list
    categories: list
    payments: dict
    sessions: int = 0
    stamp: float = field(default_factory=time.monotonic)


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    pb = params.get("paybroker_url", "")
    if not isinstance(pb, str):
        raise SpecError("params.paybroker_url must be a URL or a fleet reference such as ${PAYBROKER_URL}, or empty")
    if pb:
        from ..wl_model import allow_http_targets  # not at import time: wl_model reaches the kinds
        check_target(pb, allow_http=allow_http_targets())
    out = {"mix": p_mix(params, "mix", DEFAULT_MIX),
           "search_pct": p_num(params, "search_pct", 40, 0, 100),
           "decline_pct": p_num(params, "decline_pct", 8, 0, 100),
           "three_ds_pct": p_num(params, "three_ds_pct", 6, 0, 100),
           "connector_failure_pct": p_num(params, "connector_failure_pct", 3, 0, 100),
           "complete_3ds_pct": p_num(params, "complete_3ds_pct", 80, 0, 100),
           "max_lines": p_int(params, "max_lines", 3, 1, 5),
           "max_qty": p_int(params, "max_qty", 2, 1, 5),
           "guest": p_bool(params, "guest", False),
           "paybroker_url": pb.rstrip("/"),
           "seed": p_int(params, "seed", None, 0, 2**32 - 1)}
    if out["decline_pct"] + out["three_ds_pct"] + out["connector_failure_pct"] > 100:
        raise SpecError("params.decline_pct + three_ds_pct + connector_failure_pct must not exceed 100")
    return out


def plan_shoppers(rng: random.Random, n: int, params: dict, run_id: str, seq: int = 0) -> list[Shopper]:
    """The next `n` shopper sessions. Pure given the RNG: personas by `mix`, buyers' cards by the decline, 3-D Secure and
    connector-failure shares, identities fictional."""
    out = []
    for i in range(n):
        persona = pick_weighted(rng, params["mix"])
        roll, scenario = rng.random() * 100, "regular"
        for name, key in (("decline", "decline_pct"), ("three_ds", "three_ds_pct"), ("connector_failure", "connector_failure_pct")):
            if roll < params[key]:
                scenario = name
                break
            roll -= params[key]
        country = rng.choice(list(HOMES))
        n_ser = seq + i
        out.append(Shopper(
            seq=n_ser, persona=persona, seed=rng.randrange(2**32),
            search=rng.choice(WORDS) if rng.random() * 100 < params["search_pct"] else None,
            cart="cart" if rng.random() < 0.5 else "inline", lines=rng.randint(1, params["max_lines"]), max_qty=params["max_qty"],
            scenario=scenario if persona == "buy" else "regular",
            name=f"{rng.choice(FIRST)} {rng.choice(LAST)}", email=f"sim.shopper{n_ser:05d}.{run_id}@example.com", country=country,
            ip=f"{HOMES[country][0]}.{rng.randint(2, 250)}", device=f"dev_sim_{run_id}_{n_ser:05d}",
            age_hours=rng.choice([30, 200, 900, 4000, 9000]), complete_3ds=rng.random() * 100 < params["complete_3ds_pct"]))
    return out


def shop_code(response) -> str:
    """The `code` of ShopFront's error body (`{"detail": ..., "code": ...}`), '' if none."""
    try:
        body = response.json()
    except ValueError:
        return ""
    return body.get("code", "") if isinstance(body, dict) and isinstance(body.get("code"), str) else ""


def classify_shop(status: int, code: str = "", headers=None) -> Result:
    """The shop's answer to any call that is not a successful one. 401 and 403 mean the credential is wrong (a guest is never
    refused with 403 because it states no customer data); 429, 409 out of stock and 503 payments off are expected."""
    if status == 409 and code in ("OUT_OF_STOCK", "PRODUCT_UNAVAILABLE"):
        return Result("out_of_stock")
    if status == 503 and code == "PAYMENTS_UNAVAILABLE":
        return Result("checkout_skipped")
    if status == 403:
        return Result("auth_error", error="HTTP 403: the shop refused the credential (a service token needs scope shop or shopfront.api)",
                      fatal=True)
    return classify_status(status, headers)


def classify_checkout(outcome: str) -> str:
    return {"approved": "approved", "declined": "declined", "fraud_declined": "fraud_declined", "unavailable": "payment_unavailable",
            "pending": "pending", "requires_3ds": "requires_3ds"}.get(outcome, "client_error")


class ShopfrontSender(BaseSender):
    def __init__(self, params: dict, http, rng, *, service: bool = False, transport=None, run_id: str | None = None,
                 allow_http: bool = False):
        super().__init__(params, http, rng)
        self.service, self.transport, self.allow_http = service, transport, allow_http
        self.run_id = run_id or f"{rng.randrange(16**6):06x}"
        self.seq = 0
        self.used_cards: set[str] = set()
        self.info: Info | None = None
        self.pb: Http | None = None
        self.pb_key = ""
        self.sessions = self.checkouts = 0
        self._lock = asyncio.Lock()

    def plan(self, n: int) -> list[Shopper]:
        shoppers = plan_shoppers(self.rng, n, self.params, self.run_id, self.seq)
        self.seq += n
        return shoppers

    def extra(self) -> dict:
        pay = (self.info.payments if self.info else {}) or {}
        return {"mode": "service token" if self.service else "guest", "sessions": self.sessions, "checkouts": self.checkouts,
                "catalog_products": len(self.info.products) if self.info else 0, "payments_enabled": bool(pay.get("enabled")),
                "setup_calls": self.setup_calls}

    # ---- calls ----------------------------------------------------------------------------------------------------
    async def _call(self, method: str, path: str, *, json=None, headers=None):
        """(response, None) when the shop answered below 400, else (None, the Result that ends the session)."""
        h = dict(headers or {})
        if not self.service and method != "GET":
            h["X-ShopFront-CSRF"] = "1"           # what the browser sends on writes; a bearer token needs none
        r, latency, err = await self.http.call(method, path, json=json, headers=h)
        if r is None:
            return None, network_result(latency, err)
        if r.status_code >= 400:
            return None, classify_shop(r.status_code, shop_code(r), r.headers)
        return r, None

    async def _info(self) -> Info | Result:
        async with self._lock:
            if self.info and self.info.sessions < REFRESH_EVERY:
                self.info.sessions += 1
                return self.info
            r, bad = await self._call("GET", "/api/v1/config")
            if bad:
                return bad
            payments = r.json().get("payments") or {}
            r, bad = await self._call("GET", "/api/v1/products?limit=100&in_stock=true")
            if bad:
                return bad
            products = [p for p in r.json().get("items", []) if isinstance(p, dict) and p.get("id") and p.get("sku") and p.get("slug")]
            r, bad = await self._call("GET", "/api/v1/categories")
            categories = [] if bad else [c["slug"] for c in r.json().get("items", []) if isinstance(c, dict) and c.get("slug")]
            self.setup_calls += 3
            self.info = Info(products, categories, payments, 1)
            return self.info

    async def _paybroker(self, info: Info) -> Http | Result:
        """The client that tokenizes cards at PayBroker: the address of `params.paybroker_url` (a fleet reference is resolved now), else
        the one the shop publishes; it must pass the same URL rules as any target."""
        async with self._lock:
            if self.pb:
                return self.pb
            from .. import fleet
            try:
                url = fleet.resolve(self.params["paybroker_url"], self.allow_http) if self.params["paybroker_url"] else \
                    check_target(str(info.payments.get("paybroker_url") or ""), allow_http=self.allow_http)
            except SpecError as exc:
                return Result("client_error", error=f"PayBroker address for tokenizing cards: {exc}")
            key = str(info.payments.get("publishable_key") or "")
            if not url or not key:
                return Result("client_error", error="the shop published no PayBroker address or publishable key although payments are enabled")
            self.pb = Http(url, key, transport=self.transport)
            return self.pb

    async def _tokenize(self, pb: Http, number: str) -> tuple[str | None, Result | None]:
        r, latency, err = await pb.call("POST", "/v1/vault/tokens", json={"number": number, "exp_month": 12, "exp_year": time.gmtime().tm_year + 4,
                                                                           "cvc": "123"})
        self.setup_calls += 1
        if r is None:
            return None, network_result(latency, err)
        if r.status_code != 201:
            res = classify_status(r.status_code, r.headers)
            return None, (res if res.outcome != "ok" else Result("client_error", error=f"HTTP {r.status_code} while tokenizing"))
        return r.json()["token"], None

    # ---- one session ----------------------------------------------------------------------------------------------
    async def execute(self, op: Shopper) -> Result:
        t0 = time.monotonic()
        res = await self._session(op)
        res.latency = time.monotonic() - t0
        self.sessions += 1
        return res

    async def _session(self, op: Shopper) -> Result:
        info = await self._info()
        if isinstance(info, Result):
            return info
        if not info.products:
            return Result("client_error", error="the catalog has no products in stock")
        rng = random.Random(op.seed)
        query = f"sort={rng.choice(SORTS)}&in_stock=true&limit=24"
        if info.categories and rng.random() < 0.7:
            query += f"&category={quote(rng.choice(info.categories))}"
        r, bad = await self._call("GET", f"/api/v1/products?{query}")
        if bad:
            return bad
        listed = [p for p in r.json().get("items", []) if isinstance(p, dict) and p.get("id") and p.get("sku") and p.get("slug")]
        pool = listed
        if op.search:
            r, bad = await self._call("GET", f"/api/v1/search?q={quote(op.search)}&in_stock=true&limit=12")
            if bad:
                return bad
            pool = [p for p in r.json().get("items", []) if isinstance(p, dict) and p.get("id") and p.get("sku") and p.get("slug")] or listed
        pool = pool or info.products
        picks = rng.sample(pool, min(op.lines, len(pool)))
        for p in picks[:2]:
            _, bad = await self._call("GET", f"/api/v1/products/{quote(p['slug'])}")
            if bad:
                return bad
        if op.persona == "browse":
            return Result("browsed")
        lines = [(p, rng.randint(1, op.max_qty)) for p in picks]
        headers: dict = {}
        if op.cart == "cart":
            r, bad = await self._call("POST", "/api/v1/carts")
            if bad:
                return bad
            headers = {"X-Cart-Token": r.json()["cart_token"]}
            for p, qty in lines:
                _, bad = await self._call("POST", "/api/v1/cart/items", json={"product_id": p["id"], "qty": qty}, headers=headers)
                if bad:
                    return bad
            _, bad = await self._call("GET", f"/api/v1/cart?country={op.country}", headers=headers)
            if bad:
                return bad
        if op.persona == "abandon":
            return Result("abandoned")
        return await self._checkout(op, info, lines, headers)

    async def _checkout(self, op: Shopper, info: Info, lines: list, headers: dict) -> Result:
        if not info.payments.get("enabled"):
            return Result("checkout_skipped")
        pb = await self._paybroker(info)
        if isinstance(pb, Result):
            return pb
        prefix = SCENARIO_PREFIX.get(op.scenario)
        number = make_test_card(self.rng, self.used_cards, prefix) if prefix else make_test_card(self.rng, self.used_cards)
        token, bad = await self._tokenize(pb, number)
        if bad:
            return bad
        _, line1, city, region, postal = HOMES[op.country]
        body = {"idempotency_key": f"sim-{self.run_id}-{op.seq:06d}", "email": op.email, "name": op.name,
                "shipping": {"line1": line1, "city": city, "region": region, "postal_code": postal, "country": op.country},
                "card_token": token,
                "telemetry": {"device_fingerprint": op.device, "checkout_duration_ms": random.Random(op.seed).randint(25_000, 120_000),
                              "pasted_card_number": False}}
        if self.service:
            body["client"] = {"ip": op.ip, "account_age_hours": op.age_hours}
        if op.cart == "inline":
            body["items"] = [{"sku": p["sku"], "qty": q} for p, q in lines]
        self.checkouts += 1
        r, bad = await self._call("POST", "/api/v1/checkout", json=body, headers=headers)
        if bad:
            return bad
        reply = r.json()
        outcome = classify_checkout(str(reply.get("outcome")))
        if outcome == "client_error":
            return Result("client_error", error=f"the shop answered an unknown checkout outcome {str(reply.get('outcome'))[:30]!r}")
        access = str(reply.get("access_token") or "")
        if outcome == "pending" and access:
            outcome = await self._settle(access, "pending")
        elif outcome == "requires_3ds" and access:
            outcome = await self._three_ds(op, pb, reply, access)
        return Result(outcome)

    async def _check(self, access: str) -> str:
        r, _ = await self._call("POST", f"/api/v1/orders/{quote(access)}/check")
        return str(r.json().get("state") or "") if r else ""

    async def _settle(self, access: str, waiting: str) -> str:
        """Ask the shop where an unfinished payment stands (never a new idempotency key), a few times."""
        for _ in range(5):
            state = await self._check(access)
            if state == "paid":
                return "approved"
            if state == "payment_failed":
                return "declined"
            await asyncio.sleep(0.2)
        return waiting

    async def _three_ds(self, op: Shopper, pb: Http, reply: dict, access: str) -> str:
        """What the tester does on PayBroker's hosted page: press succeed or fail; then the shop is asked where the order stands."""
        challenge = urlsplit(str(reply.get("redirect_url") or "")).path.rstrip("/").split("/")[-1]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", challenge or ""):
            return "requires_3ds"
        r, _, _ = await pb.call("POST", f"/api/3ds/{challenge}/complete", json={"outcome": "succeeded" if op.complete_3ds else "failed"},
                                headers={"X-PayBroker-CSRF": "1"})
        if r is None or r.status_code != 200:
            return "requires_3ds"
        for _ in range(5):
            state = await self._check(access)
            if state == "paid":
                return "approved_3ds"
            if state == "payment_failed":
                return "failed_3ds"
            await asyncio.sleep(0.2)
        return "requires_3ds"

    async def close(self) -> None:
        await super().close()
        if self.pb:
            await self.pb.close()

