"""Payments for PayBroker's merchant API (`/v1`), with the mix of customers a payment broker really sees.

Per tick, `batch` payments: regular customers, issuer declines, a 3-D Secure card, a card whose first connector fails, a card
testing burst and an account takeover, in the shares of `mix`. Every regular customer has ONE test card of its own (a card shared
by many customers trips PayBroker's velocity rules and everything is rejected): a pool of customers, sized to the rate, each with a
unique Luhn-valid number in the reserved `999999` test range. Optional follow-ups act on earlier payments: capture, refund and
completing the 3-D Secure challenge.

Declines (402) and fraud rejections (403) are EXPECTED outcomes and counted by class. Every identity is fictional: e-mail
addresses on example.com/example.org, IP addresses in the documentation ranges (the "risky" country comes from
`telemetry.ip_country`, not from a real address).
"""
import random
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import date

from ..model import SpecError
from .base import (
    BaseSender,
    Result,
    classify_status,
    json_error_code,
    network_result,
    p_int,
    p_mix,
    p_num,
    pick_weighted,
    unknown_params,
)

TEST_BIN = "999999"
HOMES = {"US": ("198.51.100", "USD"), "GB": ("203.0.113", "GBP"), "DE": ("192.0.2", "EUR")}   # country: (documentation range, currency)
RISKY_COUNTRIES = ("RU", "NG")
DECLINE_CARDS = ["4000000000000002", "4000000000009995", "4100000000000019"]   # PayBroker's public test cards
THREE_DS_CARD = "4000002500003155"
CONNECTOR_FAILURE_CARD = "4000000000000119"
SCENARIOS = ("regular", "decline", "three_ds", "connector_failure", "card_testing", "account_takeover")
DEFAULT_MIX = {"regular": 74, "decline": 8, "three_ds": 5, "connector_failure": 5, "card_testing": 5, "account_takeover": 3}
PARAMS = {"mix", "customers", "capture_pct", "refund_pct", "complete_3ds_pct", "capture_method", "publishable_env", "seed"}
MAX_PENDING = 2000


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def make_test_card(rng: random.Random, used: set[str] | None = None) -> str:
    """A fresh Luhn-valid 16-digit number in PayBroker's reserved test range (999999...), unique within `used`."""
    while True:
        body = TEST_BIN + "".join(str(rng.randrange(10)) for _ in range(9))
        number = next(body + str(c) for c in range(10) if luhn_ok(body + str(c)))
        if used is None or number not in used:
            if used is not None:
                used.add(number)
            return number


@dataclass
class Customer:
    id: str
    email: str
    ip: str
    country: str
    currency: str
    device: str
    card: str
    typical: int            # the customer's usual basket, in cents
    token: str = ""         # filled in by the sender after tokenizing `card`


@dataclass
class PaymentOp:
    scenario: str
    card: str                       # the test card number to tokenize (cached per number)
    body: dict                      # POST /v1/payments/authorize body, with `card_token` set at send time
    customer_index: int = -1


@dataclass
class FollowUp:
    action: str                     # capture | refund | complete_3ds
    ref: str                        # payment id, or 3-D Secure challenge id
    amount: int = 0


@dataclass
class State:
    """What the generator remembers between ticks."""
    run_id: str
    customers: list[Customer] = field(default_factory=list)
    used_cards: set[str] = field(default_factory=set)
    seq: int = 0


def build_customers(rng: random.Random, count: int, used: set[str]) -> list[Customer]:
    out = []
    for i in range(1, count + 1):
        country = rng.choice(list(HOMES))
        base, currency = HOMES[country]
        out.append(Customer(f"cus_wl_{i:04d}", f"shopper{i:04d}@example.com", f"{base}.{rng.randint(2, 250)}", country, currency,
                            f"dev_wl_{i:04d}", make_test_card(rng, used), rng.choice([1500, 3200, 6400, 9900, 14000])))
    return out


def pool_size(rate: float, customers: int | None) -> int:
    """Customers in the pool: explicit, or sized to the rate (about 30 seconds of payments, between 25 and 2000)."""
    if customers:
        return customers
    return max(25, min(2000, round(rate * 30)))


def _base_body(rng: random.Random, c: Customer, key: str, method: str | None) -> dict:
    body = {"idempotency_key": key, "currency": c.currency,
            "customer": {"id": c.id, "email": c.email, "ip_address": c.ip},
            "payment_method": {"type": "card", "card_token": "", "cvv_check": True},
            "telemetry": {"device_fingerprint": c.device, "checkout_duration_ms": rng.randint(25_000, 120_000),
                          "user_agent": "Mozilla/5.0 (workload)", "pasted_card_number": False, "ip_country": c.country},
            "shipping_address": {"country": c.country}, "amount": max(500, int(c.typical * rng.uniform(0.5, 1.6)))}
    if method:
        body["capture_method"] = method
    return body


def plan_payments(state: State, rng: random.Random, n: int, params: dict) -> list[PaymentOp]:
    """The next `n` payments. Pure: same state and RNG, same output. Advances `state.seq`."""
    mix, method = params["mix"], params["capture_method"]
    ops = []
    for _ in range(n):
        scenario = pick_weighted(rng, mix)
        idx = rng.randrange(len(state.customers))
        c = state.customers[idx]
        key = f"wl-{state.run_id}-{state.seq:07d}"
        state.seq += 1
        body = _base_body(rng, c, key, method)
        card = c.card
        if scenario == "decline":
            card = rng.choice(DECLINE_CARDS)
        elif scenario == "three_ds":
            card = THREE_DS_CARD
        elif scenario == "connector_failure":
            card = CONNECTOR_FAILURE_CARD
        elif scenario == "card_testing":
            # one device, one address, a new test card every time, tiny amounts, a hurried pasted checkout
            probe = state.seq % 3
            card = make_test_card(rng, state.used_cards)
            body["customer"] = {"id": f"cus_wl_probe_{state.run_id}_{probe}", "email": "probe@example.com",
                                "ip_address": f"203.0.113.{200 + probe}", "account_age_hours": round(rng.uniform(0.1, 3), 2)}
            body["telemetry"] = {"device_fingerprint": f"dev_wl_probe_{state.run_id}", "checkout_duration_ms": rng.randint(900, 2500),
                                 "user_agent": "Mozilla/5.0 (workload)", "pasted_card_number": True, "ip_country": rng.choice(RISKY_COUNTRIES)}
            body["amount"] = rng.randint(100, 900)
        elif scenario == "account_takeover":
            # a known customer, a new device in another country, a pasted card, a big order shipped abroad
            body["telemetry"] |= {"device_fingerprint": f"dev_wl_stolen_{state.run_id}_{state.seq}", "pasted_card_number": True,
                                  "checkout_duration_ms": rng.randint(4_000, 9_000), "ip_country": rng.choice(RISKY_COUNTRIES)}
            body["amount"] = rng.randint(120_000, 350_000)
            body["shipping_address"] = {"country": rng.choice(RISKY_COUNTRIES)}
        elif scenario == "regular" and rng.random() < 0.04:
            # a brand-new account: young account, hurried checkout (a mild risk signal, usually still approved)
            body["customer"]["account_age_hours"] = round(rng.uniform(0.5, 12), 2)
            body["telemetry"]["checkout_duration_ms"] = rng.randint(6_000, 14_000)
        ops.append(PaymentOp(scenario, card, body, idx if card == c.card else -1))
    return ops


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    out = {"mix": p_mix(params, "mix", DEFAULT_MIX),
           "customers": p_int(params, "customers", None, 1, 5000),
           "capture_pct": p_num(params, "capture_pct", 0, 0, 100),
           "refund_pct": p_num(params, "refund_pct", 0, 0, 100),
           "complete_3ds_pct": p_num(params, "complete_3ds_pct", 0, 0, 100),
           "capture_method": params.get("capture_method"),
           "seed": p_int(params, "seed", None, 0, 2**32 - 1)}
    if out["capture_method"] not in (None, "automatic", "manual"):
        raise SpecError("params.capture_method must be automatic, manual or empty (the merchant's default)")
    if out["capture_pct"] > 0:
        out["capture_method"] = "manual"        # only an authorized payment can be captured later
    elif out["refund_pct"] > 0:
        out["capture_method"] = "automatic"     # only a captured payment can be refunded
    return out


def _challenge_id(response_json: dict) -> str:
    url = ((response_json.get("action_required") or {}).get("redirect_url")) or ""
    m = re.search(r"/3ds/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else ""


class PaybrokerSender(BaseSender):
    def __init__(self, params: dict, http, rng, *, rate: float = 1.0, publishable_http=None, run_id: str | None = None):
        super().__init__(params, http, rng)
        self.pub = publishable_http                       # optional: tokenize like a browser would, with the publishable key
        self.state = State(run_id or f"{rng.randrange(16**6):06x}")
        self.state.customers = build_customers(rng, pool_size(rate, params["customers"]), self.state.used_cards)
        self.tokens: dict[str, str] = {}                  # test card number -> vault token
        self.pending: deque[FollowUp] = deque(maxlen=MAX_PENDING)
        self.followups = 0

    def plan(self, n: int) -> list:
        ops: list = []
        for _ in range(min(n, len(self.pending))):     # follow-ups of earlier payments go first, at most `n` per tick
            ops.append(self.pending.popleft())
        return ops + plan_payments(self.state, self.rng, n, self.params)

    def extra(self) -> dict:
        return {"customers": len(self.state.customers), "pending_followups": len(self.pending), "followups": self.followups,
                "setup_calls": self.setup_calls}

    async def _token(self, card: str) -> tuple[str | None, Result | None]:
        if card in self.tokens:
            return self.tokens[card], None
        exp_year = date.today().year + 4
        client = self.pub or self.http
        r, latency, err = await client.call("POST", "/v1/vault/tokens", json={"number": card, "exp_month": 12, "exp_year": exp_year, "cvc": "123"})
        self.setup_calls += 1
        if r is None:
            return None, network_result(latency, err)
        if r.status_code != 201:
            res = classify_status(r.status_code, r.headers)
            if res.outcome == "ok":
                res = Result("client_error", error=f"HTTP {r.status_code} while tokenizing")
            return None, res
        self.tokens[card] = r.json()["token"]
        return self.tokens[card], None

    async def execute(self, op) -> Result:
        if isinstance(op, FollowUp):
            return await self._followup(op)
        token, failed = await self._token(op.card)
        if failed:
            return failed
        op.body["payment_method"]["card_token"] = token
        r, latency, err = await self.http.call("POST", "/v1/payments/authorize", json=op.body)
        if r is None:
            return network_result(latency, err)
        res = classify_payment(r.status_code, json_error_code(r), r.headers)
        res.latency = latency
        if r.status_code == 200:
            payment = r.json()
            if payment.get("status") == "REQUIRES_ACTION":
                res.outcome = "requires_action"
            self._schedule_followups(payment, op)
        return res

    def _schedule_followups(self, payment: dict, op: PaymentOp) -> None:
        status, p = payment.get("status"), self.params
        if status == "AUTHORIZED" and payment.get("payment_id") and self.rng.random() * 100 < p["capture_pct"]:
            self.pending.append(FollowUp("capture", payment["payment_id"]))
        elif status == "CAPTURED" and payment.get("payment_id") and self.rng.random() * 100 < p["refund_pct"]:
            self.pending.append(FollowUp("refund", payment["payment_id"], max(1, op.body["amount"] // 2)))
        elif status == "REQUIRES_ACTION" and self.rng.random() * 100 < p["complete_3ds_pct"]:
            challenge = _challenge_id(payment)
            if challenge:
                self.pending.append(FollowUp("complete_3ds", challenge))

    async def _followup(self, f: FollowUp) -> Result:
        self.followups += 1
        if f.action == "complete_3ds":
            r, latency, err = await self.http.call("POST", f"/api/3ds/{f.ref}/complete", json={"outcome": "succeeded"})
        elif f.action == "capture":
            r, latency, err = await self.http.call("POST", f"/v1/payments/{f.ref}/capture", json={})
        else:
            r, latency, err = await self.http.call("POST", f"/v1/payments/{f.ref}/refund", json={"amount": f.amount})
        if r is None:
            return network_result(latency, err)
        if r.status_code == 200:
            if f.action == "capture" and self.rng.random() * 100 < self.params["refund_pct"] and r.json().get("payment_id"):
                self.pending.append(FollowUp("refund", f.ref, max(1, int(r.json().get("amount", 200)) // 2)))
            res = Result(f"{f.action}_ok", latency)
        else:
            res = classify_status(r.status_code, r.headers)
            if res.outcome == "client_error":
                res = Result("followup_refused", latency)      # e.g. the payment was already in another state: not a fault
            res.latency = latency
        return res

    async def close(self) -> None:
        await super().close()
        if self.pub:
            await self.pub.close()


def classify_payment(status: int, code: str, headers=None) -> Result:
    """PayBroker's answers to an authorization. 402 and 403 FRAUD_REJECTED are business outcomes, not faults."""
    if status == 200:
        return Result("ok")        # the sender refines it: REQUIRES_ACTION is its own class
    if status == 402:
        return Result("declined")
    if status == 403 and code == "FRAUD_REJECTED":
        return Result("fraud_rejected")
    if status == 403:
        return Result("auth_error", error=f"HTTP 403 {code or 'forbidden'}: check the merchant key", fatal=True)
    if status == 422:
        return Result("client_error", error=f"HTTP 422 {code or 'validation'}")
    return classify_status(status, headers)
