"""The `shopfront` workload kind: shopper sessions against a fake ShopFront and a fake PayBroker (one mock transport, no network)."""
import asyncio
import json
import random
import re
from collections import Counter

import httpx
import pytest

from cpsim import kinds
from cpsim.kinds import shopfront
from cpsim.kinds.base import Http
from cpsim.model import SpecError
from cpsim.wl_model import parse_workload

PK = "pk_test_SENTINEL"
SERVICE = "svc_SENTINEL"
PRODUCTS = [{"id": i, "sku": f"SF-{i:03d}", "slug": f"thing-{i}", "name": f"Thing {i}", "price_cents": 1000 + i, "in_stock": True} for i in range(1, 9)]


def arun(coro):
    return asyncio.run(coro)


class Fake:
    """ShopFront's storefront API and PayBroker's tokenization and 3-D Secure endpoints, as far as the sender touches them."""

    def __init__(self, payments=True, pb_url="https://pb.example", checkout_status=None):
        self.payments, self.pb_url, self.checkout_status = payments, pb_url, checkout_status
        self.seen: list[tuple[str, str, str, dict, dict]] = []      # host, "METHOD path", body text, headers, json
        self.tokens: dict[str, str] = {}
        self.orders: dict[str, str] = {}
        self.completed: list[str] = []
        self.states: dict[str, str] = {}

    def pb(self, req, path, body):
        if path == "/v1/vault/tokens":
            if req.headers.get("authorization") != f"Bearer {PK}":
                return httpx.Response(401, json={"error": {"code": "UNAUTHORIZED"}})
            token = f"tok_{len(self.tokens):04d}"
            self.tokens[token] = body["number"]
            return httpx.Response(201, json={"token": token})
        m = re.fullmatch(r"/api/3ds/(\w+)/complete", path)
        if m and req.headers.get("x-paybroker-csrf") == "1":
            self.completed.append(f"{m.group(1)}:{body['outcome']}")
            self.states[m.group(1)] = "paid" if body["outcome"] == "succeeded" else "payment_failed"
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404, json={})

    def shop(self, req, path, body):
        q = dict(req.url.params)
        if req.method == "GET" and path == "/api/v1/config":
            return httpx.Response(200, json={"payments": {"enabled": self.payments, "paybroker_url": self.pb_url,
                                                          "publishable_key": PK if self.payments else ""}})
        if path == "/api/v1/categories":
            return httpx.Response(200, json={"items": [{"slug": "gear", "name": "Gear"}, {"slug": "home", "name": "Home"}]})
        if path == "/api/v1/products":
            return httpx.Response(200, json={"items": PRODUCTS[: int(q.get("limit", 100))]})
        if path == "/api/v1/search":
            return httpx.Response(200, json={"items": PRODUCTS[:3], "mode": "fulltext", "notice": ""})
        m = re.fullmatch(r"/api/v1/products/([\w-]+)", path)
        if m:
            return httpx.Response(200, json=next(p for p in PRODUCTS if p["slug"] == m.group(1)))
        if path == "/api/v1/carts":
            return httpx.Response(201, json={"cart_token": "cart_SENTINEL"})
        if path == "/api/v1/cart/items":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/cart":
            return httpx.Response(200, json={"lines": [], "checkout_ok": True})
        if path == "/api/v1/checkout":
            if self.checkout_status:
                return httpx.Response(self.checkout_status[0], json=self.checkout_status[1], headers=self.checkout_status[2])
            number = self.tokens[body["card_token"]]
            access = f"order_{len(self.orders):04d}"
            self.orders[access] = number
            out, state, extra = "approved", "paid", {}
            if number.startswith("99999901"):
                out, state = "declined", "payment_failed"
            elif number.startswith("99999902"):
                out, state, extra = "requires_3ds", "requires_3ds", {"redirect_url": f"https://pb.example/3ds/ch{len(self.orders)}"}
                self.states[f"ch{len(self.orders)}"] = "requires_3ds"
                self.orders[access] = f"ch{len(self.orders)}"
            elif number.startswith("99999903"):
                out, state = "unavailable", "payment_failed"
            return httpx.Response(200, json={"outcome": out, "order": {"number": "SF-1", "state": state}, "access_token": access, **extra})
        m = re.fullmatch(r"/api/v1/orders/(\w+)/check", path)
        if m:
            return httpx.Response(200, json={"state": self.states.get(self.orders[m.group(1)], "paid")})
        return httpx.Response(404, json={"detail": "no", "code": "NOT_FOUND"})

    def handler(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        self.seen.append((req.url.host, f"{req.method} {req.url.path}", req.content.decode(), dict(req.headers), body))
        return self.pb(req, req.url.path, body) if req.url.host == "pb.example" else self.shop(req, req.url.path, body)

    def calls(self, host="shop.example"):
        return [c[1] for c in self.seen if c[0] == host]


def sender(fake, service=True, seed=3, allow_http=False, **params):
    p = shopfront.validate(params)
    transport = httpx.MockTransport(fake.handler)
    http = Http("https://shop.example", SERVICE if service else None, transport=transport)
    return shopfront.ShopfrontSender(p, http, random.Random(seed), service=service, transport=transport, run_id="abc123", allow_http=allow_http)


def run(s, n=1):
    try:
        return arun(s.run_batch(n, 2))
    finally:
        arun(s.close())


def only(persona):
    return {"mix": {"browse": 100 if persona == "browse" else 0, "abandon": 100 if persona == "abandon" else 0, "buy": 100 if persona == "buy" else 0}}


# ---- the generator --------------------------------------------------------------------------------------------------
def test_the_plan_is_pure_seeded_and_follows_the_mix():
    p = shopfront.validate({"mix": {"browse": 50, "abandon": 0, "buy": 50}, "search_pct": 100, "decline_pct": 100, "three_ds_pct": 0,
                            "connector_failure_pct": 0})
    a = shopfront.plan_shoppers(random.Random(5), 400, p, "r1")
    b = shopfront.plan_shoppers(random.Random(5), 400, p, "r1")
    assert a == b
    got = Counter(x.persona for x in a)
    assert set(got) == {"browse", "buy"} and 150 < got["buy"] < 250
    assert all(x.search for x in a)
    assert {x.scenario for x in a if x.persona == "buy"} == {"decline"} and {x.scenario for x in a if x.persona != "buy"} == {"regular"}
    assert [x.seq for x in a] == list(range(400)) and len({x.email for x in a}) == 400
    for x in a:
        assert x.email.endswith("@example.com") and re.fullmatch(r"(198\.51\.100|203\.0\.113|192\.0\.2)\.\d+", x.ip) and 1 <= x.lines <= 3


def test_params_are_validated():
    assert shopfront.validate({})["mix"] == {"browse": 55, "abandon": 25, "buy": 20}
    for bad in ({"nope": 1}, {"mix": {"browse": 0, "abandon": 0, "buy": 0}}, {"mix": {"sell": 5}}, {"search_pct": 101}, {"max_lines": 0},
                {"max_qty": 6}, {"guest": "yes"}, {"decline_pct": 60, "three_ds_pct": 60}, {"paybroker_url": 5},
                {"paybroker_url": "http://pb.example"}, {"paybroker_url": "${HOME}"}):
        with pytest.raises(SpecError):
            shopfront.validate(bad)
    assert shopfront.validate({"paybroker_url": "${PAYBROKER_URL}"})["paybroker_url"] == "${PAYBROKER_URL}"


def test_a_generate_only_workload_sends_nothing():
    s = shopfront.ShopfrontSender(shopfront.validate({}), None, random.Random(1))
    assert [r.outcome for r in arun(s.run_batch(5, 2))] == ["generated"] * 5


# ---- sessions -------------------------------------------------------------------------------------------------------
def test_browsers_look_around_and_never_touch_a_cart():
    fake = Fake()
    res = run(sender(fake, **only("browse"), search_pct=100), 6)
    assert Counter(r.outcome for r in res) == {"browsed": 6} and not any(r.is_error for r in res)
    calls = fake.calls()
    assert any(c.startswith("GET /api/v1/search") for c in calls) and any(c.startswith("GET /api/v1/products/") for c in calls)
    assert not any(c in ("POST /api/v1/carts", "POST /api/v1/cart/items", "POST /api/v1/checkout") for c in calls)
    assert all(r.latency is not None for r in res)


def test_abandoners_fill_a_cart_and_leave_without_paying():
    fake = Fake()
    res = run(sender(fake, **only("abandon"), search_pct=0), 12)
    assert Counter(r.outcome for r in res) == {"abandoned": 12}
    calls = fake.calls()
    assert "POST /api/v1/cart/items" in calls and "POST /api/v1/checkout" not in calls
    assert not any(c.startswith("GET /api/v1/search") for c in calls) and not fake.tokens, "no card is tokenized without a checkout"


def test_buyers_check_out_with_a_service_token_and_state_the_customer():
    fake = Fake()
    res = run(sender(fake, **only("buy"), decline_pct=0, three_ds_pct=0, connector_failure_pct=0), 8)
    assert Counter(r.outcome for r in res) == {"approved": 8}
    checkouts = [c for c in fake.seen if c[1] == "POST /api/v1/checkout"]
    assert len(checkouts) == 8 and len({c[4]["idempotency_key"] for c in checkouts}) == 8
    for _host, _, text, headers, body in checkouts:
        assert headers["authorization"] == f"Bearer {SERVICE}"
        assert body["client"]["ip"] and body["client"]["account_age_hours"] > 0
        assert body["email"].endswith("@example.com") and body["card_token"].startswith("tok_")
        assert "card_number\":false" in text and not re.search(r"\b9999\d{12}\b", text), "the shop never sees a card number"
        assert 8 <= len(body["idempotency_key"]) <= 128 and set(body) <= {"idempotency_key", "email", "name", "shipping", "card_token",
                                                                          "items", "telemetry", "client"}
    modes = {("items" in c[4], "x-cart-token" in c[3]) for c in checkouts}
    assert modes == {(True, False), (False, True)}, "half use the server-side cart, half send the items inline"


def test_the_card_number_and_the_publishable_key_go_only_to_paybroker():
    fake = Fake()
    run(sender(fake, **only("buy")), 6)
    pb = [c for c in fake.seen if c[0] == "pb.example"]
    assert pb and all(c[3]["authorization"] == f"Bearer {PK}" for c in pb if c[1] == "POST /v1/vault/tokens")
    cards = set(fake.tokens.values())
    assert all(n.startswith("999999") and len(n) == 16 for n in cards) and len(cards) == len(fake.tokens)
    for c in fake.seen:
        if c[0] == "shop.example":
            assert PK not in c[2] and not any(n in c[2] for n in cards) and PK not in str(c[3])


def test_guests_carry_no_token_no_customer_data_and_the_browser_csrf_header():
    fake = Fake()
    res = run(sender(fake, service=False, **only("buy")), 6)
    assert not any(r.is_error for r in res)
    for host, call, _, headers, body in fake.seen:
        if host != "shop.example":
            continue
        assert "authorization" not in headers
        if call.startswith("POST"):
            assert headers["x-shopfront-csrf"] == "1"
        if call == "POST /api/v1/checkout":
            assert "client" not in body


def test_cards_pick_the_scenario_and_outcomes_are_counted_by_class():
    fake = Fake()
    p = {"mix": {"browse": 0, "abandon": 0, "buy": 100}, "decline_pct": 25, "three_ds_pct": 25, "connector_failure_pct": 25, "complete_3ds_pct": 50}
    res = run(sender(fake, seed=11, **p), 120)
    got = Counter(r.outcome for r in res)
    assert set(got) == {"approved", "declined", "approved_3ds", "failed_3ds", "payment_unavailable"}, got
    assert not any(r.is_error for r in res)
    prefixes = Counter(n[:8] for n in fake.tokens.values())
    assert {"99999901", "99999902", "99999903"} <= set(prefixes)
    assert fake.completed and all(c.endswith((":succeeded", ":failed")) for c in fake.completed)


def test_without_a_paybroker_connection_shoppers_still_browse_and_fill_carts():
    fake = Fake(payments=False)
    res = run(sender(fake, **only("buy")), 4)
    assert Counter(r.outcome for r in res) == {"checkout_skipped": 4} and not any(r.is_error for r in res)
    assert "POST /api/v1/checkout" not in fake.calls() and not fake.tokens


def test_the_paybroker_address_follows_the_same_url_rules_as_a_target(monkeypatch):
    plain = Fake(pb_url="http://pb.internal:8000")
    res = run(sender(plain, **only("buy")), 1)
    assert res[0].outcome == "client_error" and "https" in res[0].error and not plain.tokens
    link_local = Fake(pb_url="https://169.254.169.254")
    assert run(sender(link_local, **only("buy")), 1)[0].outcome == "client_error"
    monkeypatch.setenv("PAYBROKER_URL", "https://pb.example")
    fake = Fake(pb_url="https://elsewhere.example")        # the shop's own address is ignored when the operator names one
    res = run(sender(fake, paybroker_url="${PAYBROKER_URL}", **only("buy")), 3)
    assert not any(r.is_error for r in res) and fake.tokens and not [c for c in fake.seen if c[0] == "elsewhere.example"]


def test_refusals_rate_limits_and_stock_are_classified():
    for status, code, outcome, fatal in ((401, "", "auth_error", True), (403, "FORBIDDEN", "auth_error", True), (429, "", "rate_limited", False),
                                         (409, "OUT_OF_STOCK", "out_of_stock", False), (503, "PAYMENTS_UNAVAILABLE", "checkout_skipped", False),
                                         (500, "", "server_error", False), (422, "EMPTY_CART", "client_error", False)):
        fake = Fake(checkout_status=(status, {"detail": "x", "code": code}, {"Retry-After": "7"}))
        res = run(sender(fake, **only("buy"), decline_pct=0, three_ds_pct=0, connector_failure_pct=0), 1)[0]
        assert (res.outcome, res.fatal) == (outcome, fatal), (status, res)
        assert res.throttled == (outcome == "rate_limited")
    assert run(sender(Fake(checkout_status=(429, {}, {"Retry-After": "7"})), **only("buy")), 1)[0].retry_after == 7.0


def test_a_shop_that_does_not_answer_is_a_network_error():
    s = sender(Fake(), **only("browse"))

    def boom(req):
        raise httpx.ConnectError("down")
    s.http.client._transport = httpx.MockTransport(boom)
    assert run(s, 1)[0].outcome == "network_error"


def test_the_catalog_is_read_once_and_refreshed_after_a_while():
    fake = Fake()
    s = sender(fake, **only("browse"))
    assert not any(r.is_error for r in run(s, 3))
    assert fake.calls().count("GET /api/v1/config") == 1
    s2 = sender(fake)
    s2.info = shopfront.Info(PRODUCTS, ["gear"], {"enabled": False}, shopfront.REFRESH_EVERY)
    before = fake.calls().count("GET /api/v1/config")
    run(s2, 1)
    assert fake.calls().count("GET /api/v1/config") == before + 1
    assert s.extra()["catalog_products"] == len(PRODUCTS) and s.extra()["mode"] == "service token"


# ---- registration and credentials -----------------------------------------------------------------------------------
def wl(**kw):
    return parse_workload({"name": "shoppers", "kind": "shopfront", "target_url": "https://shop.example", **kw})


def test_the_kind_is_registered_and_its_defaults_validate():
    k = kinds.KINDS["shopfront"]
    assert k.token_env == "SHOPFRONT_TOKEN" and k.scope == "shopfront.api" and k.token_optional
    assert k.defaults() == shopfront.validate({}) and k.presets and k.fields
    for preset in k.presets:
        assert wl(params=preset["params"], schedule=preset["schedule"]).params["mix"]
    from cpsim import fleet
    assert fleet.KIND_VARS["shopfront"] == "SHOPFRONT_URL" and "SHOPFRONT_URL" in fleet.FLEET_VARS


def test_the_credential_is_a_token_when_there_is_one_and_a_guest_when_there_is_none(monkeypatch):
    assert kinds.credential(wl()) is None, "no token, no service identity: guests"
    monkeypatch.setenv("SHOPFRONT_TOKEN", "svc_X")
    assert kinds.credential(wl()) == "svc_X"
    assert kinds.credential(wl(params={"guest": True})) is None, "guest wins over a token"
    assert kinds.credential(wl(target_url="")) is None
    s = kinds.build_sender(wl(), token="svc_X")
    assert isinstance(s, shopfront.ShopfrontSender) and s.service
    assert not kinds.build_sender(wl(), token=None).service
    arun(s.close())
