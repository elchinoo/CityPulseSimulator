"""Payments for PayBroker's merchant API (`/v1`), with the mix of customers a payment broker really sees.

Per tick, `batch` payments: regular customers, issuer declines, a 3-D Secure card, a card whose first connector fails, a card
testing burst and an account takeover, in the shares of `mix`. Every regular customer has ONE test card of its own (a card shared
by many customers trips PayBroker's velocity rules and everything is rejected): a pool of customers, sized to the rate, each with a
unique Luhn-valid number in the reserved `999999` test range. Optional follow-ups act on earlier payments: capture, refund and
completing the 3-D Secure challenge.

Customers pay in shuffled rounds (never five times within a minute) and the card-testing probes use addresses no customer has.
The decline, 3-D Secure and connector-failure scenarios each use a FRESH unique card per payment, in the reserved test prefixes
PayBroker maps to those behaviours (`99999901` decline, `99999902` 3-D Secure, `99999903` first connector fails). The public
shared numbers would reach hundreds of payments an hour, and PayBroker scores such a card as fraud (card velocity) before it can
reach the issuer, the 3-D Secure step or the connector. This needs a PayBroker with those prefixes (see its README, "test cards").

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
# Reserved prefixes inside PayBroker's test range, each carrying a behaviour (PayBroker's TEST_PREFIX_BEHAVIOR). Needs that PayBroker.
DECLINE_PREFIX = "99999901"             # the issuer declines (do_not_honor)
THREE_DS_PREFIX = "99999902"            # the issuer demands 3-D Secure
CONNECTOR_FAILURE_PREFIX = "99999903"   # the first connector answers 5xx; the cascade recovers
SCENARIO_PREFIX = {"decline": DECLINE_PREFIX, "three_ds": THREE_DS_PREFIX, "connector_failure": CONNECTOR_FAILURE_PREFIX}
SCENARIOS = ("regular", "decline", "three_ds", "connector_failure", "card_testing", "account_takeover")
DEFAULT_MIX = {"regular": 74, "decline": 8, "three_ds": 5, "connector_failure": 5, "card_testing": 5, "account_takeover": 3}
PARAMS = {"mix", "customers", "payments_per_customer", "capture_pct", "refund_pct", "complete_3ds_pct", "capture_method", "publishable_env", "seed"}
MAX_PENDING = 2000
PROBE_HOST = 251                # card-testing probes use .251 to .253 of 203.0.113.0/24; customers only get .2 to .250


def luhn_ok(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def make_test_card(rng: random.Random, used: set[str] | None = None, prefix: str = TEST_BIN) -> str:
    """A fresh Luhn-valid 16-digit number in PayBroker's reserved test range (999999...), unique within `used`. `prefix` is
    `999999` (random behaviour) or one of the behaviour prefixes (`99999901`...)."""
    while True:
        body = prefix + "".join(str(rng.randrange(10)) for _ in range(15 - len(prefix)))
        number = next(body + str(c) for c in range(10) if luhn_ok(body + str(c)))
        if prefix == TEST_BIN and number[:8] in SCENARIO_PREFIX.values():
            continue        # an ordinary card must not land on a behaviour prefix by chance (3 in 1000 would)
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
    card: str                       # the test card number to tokenize (a customer's own card is cached; one-shot cards are not)
    body: dict                      # POST /v1/payments/authorize body, with `card_token` set at send time
    customer_index: int = -1
    one_shot: bool = False          # a fresh card used for this payment only: do not keep its token


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
    order: list[int] = field(default_factory=list)      # the customers in a shuffled round: nobody pays twice before everybody paid once
    cursor: int = 0
    uses: dict[int, int] = field(default_factory=dict)  # payments made by the customer now at this index
    serial: int = 0                                      # the last customer number handed out (0: the pool was just built)


def make_customer(rng: random.Random, serial: int, used: set[str]) -> Customer:
    """A shopper nobody has seen before: own id, device, test card and address. The address is in the IPv6 documentation range
    (2001:db8::/32), which has room for every shopper of a long run; a small shared range would make PayBroker count many
    shoppers as one address and reject them for its velocity."""
    country = rng.choice(list(HOMES))
    _, currency = HOMES[country]
    ip = f"2001:db8:{(serial >> 16) & 0xffff:x}:{serial & 0xffff:x}::{rng.randint(1, 0xfff):x}"
    return Customer(f"cus_wl_{serial:04d}", f"shopper{serial:04d}@example.com", ip, country, currency,
                    f"dev_wl_{serial:04d}", make_test_card(rng, used), rng.choice([1500, 3200, 6400, 9900, 14000]))


def build_customers(rng: random.Random, count: int, used: set[str]) -> list[Customer]:
    return [make_customer(rng, i, used) for i in range(1, count + 1)]


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


def next_customer(state: State, rng: random.Random) -> int:
    """The next customer in a shuffled round. Each customer's payments are evenly spaced (a random pick bunches them up and a few
    customers pay five times in a minute, which PayBroker's per-user velocity rule rejects as fraud)."""
    if state.cursor >= len(state.order) or len(state.order) != len(state.customers):
        state.order = list(range(len(state.customers)))
        rng.shuffle(state.order)
        state.cursor = 0
    state.cursor += 1
    return state.order[state.cursor - 1]


def plan_payments(state: State, rng: random.Random, n: int, params: dict) -> list[PaymentOp]:
    """The next `n` payments. Pure: same state and RNG, same output. Advances `state.seq`. Decline, 3-D Secure and connector-failure
    payments each get a fresh unique card of the matching prefix (nothing is shared, so no card ever has a velocity)."""
    mix, method = params["mix"], params["capture_method"]
    ops = []
    for _ in range(n):
        scenario = pick_weighted(rng, mix)
        idx = next_customer(state, rng)
        c = state.customers[idx]
        key = f"wl-{state.run_id}-{state.seq:07d}"
        state.seq += 1
        body = _base_body(rng, c, key, method)
        card = c.card
        if scenario in SCENARIO_PREFIX:
            card = make_test_card(rng, state.used_cards, SCENARIO_PREFIX[scenario])
        if scenario == "card_testing":
            # one device, one address, a new test card every time, tiny amounts, a hurried pasted checkout
            probe = state.seq % 3
            card = make_test_card(rng, state.used_cards)
            body["customer"] = {"id": f"cus_wl_probe_{state.run_id}_{probe}", "email": "probe@example.com",
                                "ip_address": f"203.0.113.{PROBE_HOST + probe}", "account_age_hours": round(rng.uniform(0.1, 3), 2)}
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
        ops.append(PaymentOp(scenario, card, body, idx if card == c.card else -1, one_shot=card != c.card))
        if scenario != "card_testing":        # the probes use their own identities; everybody else pays as customer `c`
            retire_if_spent(state, rng, idx, params["payments_per_customer"])
    return ops


def retire_if_spent(state: State, rng: random.Random, idx: int, limit: int) -> None:
    """A shopper who has paid `limit` times leaves and a new one takes the slot. PayBroker counts a customer's, device's, card's and
    address's payments per hour, and a handful already scores as fraud (a shopper with 5 payments an hour is declined), so a small
    pool reused for hours ends with every payment declined. Real shoppers come and go."""
    state.uses[idx] = state.uses.get(idx, 0) + 1
    if state.uses[idx] >= limit:
        state.serial = max(state.serial, len(state.customers)) + 1
        state.customers[idx] = make_customer(rng, state.serial, state.used_cards)
        state.uses[idx] = 0


def validate(params: dict) -> dict:
    unknown_params(params, PARAMS)
    out = {"mix": p_mix(params, "mix", DEFAULT_MIX),
           "customers": p_int(params, "customers", None, 1, 5000),
           "payments_per_customer": p_int(params, "payments_per_customer", 3, 1, 20),
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

    async def _token(self, card: str, keep: bool = True) -> tuple[str | None, Result | None]:
        if keep and card in self.tokens:
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
        token = r.json()["token"]
        if keep:
            self.tokens[card] = token
        return token, None

    async def execute(self, op) -> Result:
        if isinstance(op, FollowUp):
            return await self._followup(op)
        token, failed = await self._token(op.card, keep=not op.one_shot)
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
