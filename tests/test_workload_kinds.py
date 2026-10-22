"""Each workload kind: its generator's output against the target's own request rules, the outcome classes, and the sender over a mock
transport (no network)."""
import asyncio
import json
import random
import re
from collections import Counter

import httpx
import pytest
import targets
from pydantic import ValidationError

from cpsim import kinds
from cpsim.kinds import identity, llmgw, pagerlens, paybroker
from cpsim.kinds.base import Http, Result, classify_status, p_mix
from cpsim.model import SpecError
from cpsim.presets import city_from_workload, live_feed
from cpsim.wl_model import parse_workload

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
EXAMPLE = ("@example.com", "@example.org")


def arun(coro):
    return asyncio.run(coro)


def wl(kind: str, params=None, **kw):
    return parse_workload({"name": "t", "kind": kind, "target_url": "https://target.example", "params": params or {}, **kw})


# ======================================================== PayBroker =============================================================
def pb_params(**kw):
    return paybroker.validate(kw)


@pytest.fixture
def created(monkeypatch):
    """Every customer made during a test, retired ones included: slots are refilled when a customer has paid enough."""
    made = []
    real = paybroker.make_customer
    monkeypatch.setattr(paybroker, "make_customer", lambda *a, **k: made.append(real(*a, **k)) or made[-1])
    return made


def test_customers_have_one_unique_luhn_test_card_each_in_the_999999_range():
    rng = random.Random(1)
    used: set[str] = set()
    customers = paybroker.build_customers(rng, 2000, used)
    cards = [c.card for c in customers]
    assert len(set(cards)) == 2000, "a shared card trips PayBroker's velocity rules"
    for n in cards:
        assert n.startswith("999999") and len(n) == 16 and targets.luhn_ok(n)
        assert n[:8] not in ("99999901", "99999902", "99999903"), "an ordinary card must not land on a behaviour prefix"
    assert len({c.id for c in customers}) == 2000 and len({c.email for c in customers}) == 2000
    assert all(c.email.endswith("@example.com") for c in customers)


def test_the_pool_is_sized_to_the_rate():
    assert paybroker.pool_size(0.5, None) == 25 and paybroker.pool_size(2, None) == 60 and paybroker.pool_size(10, None) == 300
    assert paybroker.pool_size(1000, None) == 2000 and paybroker.pool_size(2, 7) == 7


def make_ops(n=600, seed=5, **params):
    p = pb_params(**params)
    rng = random.Random(seed)
    state = paybroker.State("abc123")
    state.customers = paybroker.build_customers(rng, 60, state.used_cards)
    return state, paybroker.plan_payments(state, rng, n, p), p


def test_every_payment_matches_paybrokers_authorize_schema_and_uses_only_fictional_data():
    state, ops, _ = make_ops(1200, capture_pct=10)
    real = None
    try:
        real = targets.real_paybroker_models()["AuthorizeBody"]
    except pytest.skip.Exception:
        pass
    keys = set()
    for op in ops:
        body = json.loads(json.dumps(op.body))
        body["payment_method"]["card_token"] = "tok_vault_card_" + "x" * 14
        targets.PbAuthorize.model_validate(body)
        if real:
            real.model_validate(body)
        keys.add(body["idempotency_key"])
        c = body["customer"]
        assert c["email"].endswith(EXAMPLE) and c["ip_address"].startswith(targets.DOC_NETS)
        assert body["currency"] in ("USD", "GBP", "EUR") and body["telemetry"]["ip_country"]
    assert len(keys) == len(ops), "idempotency keys are unique"
    assert state.seq == 1200


def test_a_bad_body_would_be_caught_by_the_schema_copy():
    state, ops, _ = make_ops(3)
    body = json.loads(json.dumps(ops[0].body))
    body["payment_method"]["card_token"] = "tok_vault_card_" + "x" * 14
    body["surprise"] = 1
    with pytest.raises(ValidationError):
        targets.PbAuthorize.model_validate(body)


def test_the_mix_is_followed_and_each_scenario_looks_like_itself(created):
    state, ops, p = make_ops(6000, seed=2)
    share = Counter(op.scenario for op in ops)
    want = p["mix"]
    for k, pct in want.items():
        assert abs(100 * share[k] / 6000 - pct) < 2.0, (k, share[k])
    by = {k: [op for op in ops if op.scenario == k] for k in want}
    for scenario, prefix in (("decline", "99999901"), ("three_ds", "99999902"), ("connector_failure", "99999903")):
        cards = [op.card for op in by[scenario]]
        assert cards and all(c.startswith(prefix) and len(c) == 16 and targets.luhn_ok(c) for c in cards), scenario
        assert len(set(cards)) == len(cards), f"{scenario}: a fresh card every payment, nothing shared"
    probes = by["card_testing"]
    assert len({op.card for op in probes}) == len(probes), "every probe uses a fresh card"
    assert all(op.card.startswith("999999") and op.body["amount"] <= 900 and op.body["telemetry"]["pasted_card_number"] for op in probes)
    assert all(op.body["customer"]["account_age_hours"] < 3.1 for op in probes)
    assert len({op.body["telemetry"]["device_fingerprint"] for op in probes}) == 1
    owners = {c.card: c for c in created}
    for op in by["regular"]:
        assert owners[op.card].id == op.body["customer"]["id"], "a regular customer pays with their own card"
    takeovers = by["account_takeover"]
    assert all(op.body["amount"] >= 120_000 and op.body["telemetry"]["ip_country"] in paybroker.RISKY_COUNTRIES
               and op.body["shipping_address"]["country"] in paybroker.RISKY_COUNTRIES for op in takeovers)
    assert any("account_age_hours" in op.body["customer"] for op in by["regular"]), "some new accounts"


def test_probe_addresses_never_collide_with_a_customers_address():
    """A customer who shares an address with the card-testing probes inherits their velocity and is rejected as fraud."""
    state, ops, _ = make_ops(3000, seed=9)
    probe_ips = {op.body["customer"]["ip_address"] for op in ops if op.scenario == "card_testing"}
    assert probe_ips and len(probe_ips) <= 3
    customer_ips = {c.ip for c in paybroker.build_customers(random.Random(3), 5000, set())}
    assert not probe_ips & customer_ips
    assert all(op.body["customer"]["ip_address"].startswith(targets.DOC_NETS) for op in ops)


def test_customers_pay_in_shuffled_rounds_so_nobody_bunches_up():
    state = paybroker.State("r1")
    state.customers = paybroker.build_customers(random.Random(1), 25, state.used_cards)
    rng = random.Random(2)
    picks = [paybroker.next_customer(state, rng) for _ in range(75)]
    for k in range(3):
        assert sorted(picks[k * 25:(k + 1) * 25]) == list(range(25)), "each round visits every customer once"
    assert picks[:25] != sorted(picks[:25])


def test_no_card_is_ever_used_twice_except_a_customers_own():
    """Nothing is shared: PayBroker scores a card with many payments an hour as fraud, so the special scenarios and the probes get a
    new card each time and only regular customers reuse theirs (at most `payments_per_customer` times)."""
    state, ops, _ = make_ops(4000, seed=3)
    uses = Counter(op.card for op in ops)
    assert all(uses[op.card] == 1 for op in ops if op.scenario != "regular" and op.one_shot)
    assert max(uses.values()) <= 3 + 1
    assert all(op.one_shot for op in ops if op.scenario in ("decline", "three_ds", "connector_failure", "card_testing"))
    assert not any(op.one_shot for op in ops if op.scenario == "regular")
    assert not hasattr(paybroker, "shared_card_free") and not hasattr(state, "demoted")


def test_the_plan_is_reproducible_from_the_seed():
    a = [op.body for op in make_ops(50, seed=11)[1]]
    b = [op.body for op in make_ops(50, seed=11)[1]]
    c = [op.body for op in make_ops(50, seed=12)[1]]
    assert a == b and a != c


def test_paybroker_params_are_validated():
    assert pb_params()["capture_method"] is None
    assert pb_params(capture_pct=20)["capture_method"] == "manual"                 # only an authorized payment can be captured
    assert pb_params(refund_pct=5)["capture_method"] == "automatic"                # only a captured one can be refunded
    for bad in ({"mix": {"regular": 0, "decline": 0, "three_ds": 0, "connector_failure": 0, "card_testing": 0, "account_takeover": 0}},
                {"mix": {"regular": 101}}, {"mix": {"bogus": 5}}, {"capture_pct": 101}, {"customers": 0}, {"customers": 5001},
                {"capture_method": "later"}, {"nope": 1}):
        with pytest.raises(SpecError):
            paybroker.validate(bad)
    assert p_mix({"mix": {"regular": 50}}, "mix", paybroker.DEFAULT_MIX)["regular"] == 50.0


class PayBrokerMock:
    """Answers like PayBroker: tokenization, authorization by card, capture, refund and 3-D Secure completion."""

    def __init__(self, key="sk_test_SENTINEL"):
        self.key, self.calls, self.tokens, self.cards = key, [], {}, {}
        self.fail = None          # a (status, body, headers) to answer every authorize with

    def handler(self, req: httpx.Request) -> httpx.Response:
        self.calls.append((req.method, req.url.path))
        if req.url.path.startswith("/api/3ds/"):
            return httpx.Response(200, json={"status": "AUTHORIZED"})
        if req.headers.get("authorization") != f"Bearer {self.key}":
            return httpx.Response(401, json={"detail": "invalid key"})
        body = json.loads(req.content or b"{}")
        if req.url.path == "/v1/vault/tokens":
            tok = f"tok_vault_card_{len(self.tokens):014d}"
            self.tokens[tok] = body["number"]
            return httpx.Response(201, json={"token": tok})
        if req.url.path == "/v1/payments/authorize":
            if self.fail:
                return httpx.Response(self.fail[0], json=self.fail[1], headers=self.fail[2])
            number = self.tokens[body["payment_method"]["card_token"]]
            if number.startswith("99999901"):
                return httpx.Response(402, json={"error": {"code": "CARD_DECLINED"}})
            if body["amount"] >= 120_000:
                return httpx.Response(403, json={"error": {"code": "FRAUD_REJECTED"}})
            if number.startswith("99999902"):
                return httpx.Response(200, json={"payment_id": "pay_3ds", "status": "REQUIRES_ACTION",
                                                 "action_required": {"type": "3DS_CHALLENGE", "redirect_url": "https://pb.example/3ds/ch_abc123"}})
            status = "AUTHORIZED" if body.get("capture_method") == "manual" else "CAPTURED"
            return httpx.Response(200, json={"payment_id": f"pay_{len(self.calls)}", "status": status, "amount": body["amount"]})
        if req.url.path.endswith("/capture"):
            return httpx.Response(200, json={"payment_id": req.url.path.split("/")[3], "status": "CAPTURED", "amount": 5000})
        if req.url.path.endswith("/refund"):
            return httpx.Response(200, json={"status": "PARTIALLY_REFUNDED"})
        return httpx.Response(404)


def pb_sender(mock, *, key="sk_test_SENTINEL", rate=2.0, **params):
    http = Http("https://pb.example", key, transport=httpx.MockTransport(mock.handler))
    return paybroker.PaybrokerSender(pb_params(**params), http, random.Random(4), rate=rate)


def test_the_sender_tokenizes_each_card_once_and_classifies_the_answers():
    mock = PayBrokerMock()
    s = pb_sender(mock, mix={"regular": 60, "decline": 20, "three_ds": 5, "connector_failure": 5, "card_testing": 5, "account_takeover": 5})

    async def go():
        out = []
        for _ in range(8):
            out += await s.run_batch(20, 4)
        await s.close()
        return out
    results = arun(go())
    got = Counter(r.outcome for r in results)
    assert len(results) == 160 and got["ok"] > 50 and got["declined"] > 10 and got["fraud_rejected"] > 0
    assert got["requires_action"] > 0
    assert not any(r.is_error for r in results), "declines and fraud rejections are expected outcomes, not errors"
    tokenize_calls = [c for c in mock.calls if c[1] == "/v1/vault/tokens"]
    assert len(tokenize_calls) == len(set(mock.tokens.values())) == s.setup_calls, "one token per distinct card number"
    assert all(r.latency is not None and r.latency >= 0 for r in results)
    assert not any(n.startswith(("99999901", "99999902", "99999903")) for n in s.tokens), "one-shot scenario cards are not kept"
    assert len(s.tokens) < s.setup_calls


def test_follow_ups_capture_refund_and_complete_3ds():
    mock = PayBrokerMock()
    s = pb_sender(mock, mix={"regular": 70, "decline": 0, "three_ds": 30, "connector_failure": 0, "card_testing": 0, "account_takeover": 0},
                  capture_pct=100, refund_pct=100, complete_3ds_pct=100)

    async def go():
        first = await s.run_batch(10, 2)
        second = await s.run_batch(10, 2)
        return first, second
    first, second = arun(go())
    outcomes = Counter(r.outcome for r in first + second)
    assert outcomes["capture_ok"] > 0 and outcomes["complete_3ds_ok"] > 0 and outcomes["refund_ok"] >= 0
    paths = {c[1].rsplit("/", 1)[-1] for c in mock.calls}
    assert {"capture", "complete"} <= paths and any(c[1] == "/api/3ds/ch_abc123/complete" for c in mock.calls)
    assert s.extra()["followups"] > 0 and "customers" in s.extra()


def test_an_early_batch_has_no_follow_ups_and_pending_ones_are_capped_per_tick():
    mock = PayBrokerMock()
    s = pb_sender(mock, capture_pct=100)
    ops = s.plan(5)
    assert len(ops) == 5 and all(isinstance(o, paybroker.PaymentOp) for o in ops)
    s.pending.extend(paybroker.FollowUp("capture", f"pay_{i}") for i in range(50))
    ops = s.plan(5)
    assert sum(isinstance(o, paybroker.FollowUp) for o in ops) == 5 and len(ops) == 10 and len(s.pending) == 45


def test_payment_outcome_classes():
    c = paybroker.classify_payment
    assert c(402, "CARD_DECLINED").outcome == "declined" and not c(402, "").is_error
    assert c(403, "FRAUD_REJECTED").outcome == "fraud_rejected" and not c(403, "FRAUD_REJECTED").fatal
    f = c(403, "MERCHANT_MISMATCH")
    assert f.outcome == "auth_error" and f.fatal and f.is_error
    assert c(401, "").fatal
    r = c(429, "", {"retry-after": "7"})
    assert r.outcome == "rate_limited" and r.throttled and r.retry_after == 7.0 and not r.is_error
    assert c(503, "", {}).throttled and c(503, "").outcome == "unavailable"
    assert c(502, "PSP_UNAVAILABLE").outcome == "unavailable" and not c(502, "").throttled
    assert c(422, "IDEMPOTENCY_KEY_REUSED").outcome == "client_error"
    assert c(500, "").outcome == "server_error" and c(200, "").outcome == "ok"


def test_a_wrong_key_is_fatal_and_a_dead_network_is_a_class_not_a_crash():
    mock = PayBrokerMock()
    s = pb_sender(mock, key="sk_test_WRONG")
    results = arun(s.run_batch(3, 2))
    assert all(r.outcome == "auth_error" and r.fatal for r in results)
    assert "WRONG" not in " ".join(r.error for r in results)

    def boom(req):
        raise httpx.ConnectError("refused")
    s2 = paybroker.PaybrokerSender(pb_params(), Http("https://pb.example", "k", transport=httpx.MockTransport(boom)), random.Random(1))
    res = arun(s2.run_batch(2, 2))
    assert [r.outcome for r in res] == ["network_error"] * 2 and "ConnectError" in res[0].error


def test_without_a_target_items_are_generated_and_nothing_is_sent():
    s = paybroker.PaybrokerSender(pb_params(), None, random.Random(1))
    res = arun(s.run_batch(7, 2))
    assert [r.outcome for r in res] == ["generated"] * 7 and s.dry


# ========================================================= LLM gateway ===========================================================
def test_every_llm_call_matches_the_gateways_rules_and_has_no_personal_data():
    p = llmgw.validate({"chat_models": "echo-chat, other-chat", "embedding_models": ["echo-embed"], "chat_pct": 60, "embed_inputs": 8})
    ops = llmgw.plan_calls(random.Random(2), 600, p)
    try:
        real = targets.real_gateway_validate()
    except pytest.skip.Exception:
        real = None
    chat = emb = 0
    for op in ops:
        if isinstance(op, llmgw.ChatOp):
            chat += 1
            targets.check_chat(op.body)
            if real:
                real("chat", op.body)
        else:
            emb += 1
            targets.check_embed(op.body)
            if real:
                real("embeddings", op.body)
        text = json.dumps(op.body)
        assert not EMAIL.search(text) and not IPV4.search(text)
    assert abs(100 * chat / 600 - 60) < 6 and chat + emb == 600
    assert {o.model for o in ops if isinstance(o, llmgw.ChatOp)} == {"echo-chat", "other-chat"}, "aliases rotate"
    sizes = {o.body["max_tokens"] for o in ops if isinstance(o, llmgw.ChatOp)}
    assert min(sizes) >= 16 and max(sizes) <= 128 and len(sizes) > 20
    assert len({o.body["messages"][1]["content"] for o in ops if isinstance(o, llmgw.ChatOp)}) > 100, "prompts rotate"


def test_llm_params_are_validated():
    for bad in ({"chat_models": []}, {"chat_models": ["bad alias!"]}, {"chat_pct": 101}, {"max_tokens_min": 500, "max_tokens_max": 100},
                {"embed_inputs": 33}, {"temperature": 3}, {"x": 1}):
        with pytest.raises(SpecError):
            llmgw.validate(bad)
    only_embed = llmgw.validate({"chat_pct": 0})
    assert all(isinstance(o, llmgw.EmbedOp) for o in llmgw.plan_calls(random.Random(1), 30, only_embed))


def test_llm_outcome_classes_follow_the_gateways_error_table():
    c = llmgw.classify_llm
    assert c(200, "").outcome == "ok"
    for code in ("budget_exceeded", "rate_limit_exceeded"):
        r = c(429, code, {"retry-after": "3"})
        assert r.outcome == code and r.throttled and not r.is_error and r.retry_after == 3.0
    assert c(429, "upstream_rate_limited").outcome == "rate_limited"
    for code in ("wrong_credential", "client_not_registered", "client_disabled"):
        assert c(403, code).fatal
    assert c(401, "invalid_api_key").fatal
    assert c(403, "model_not_allowed").outcome == "client_error" and not c(403, "model_not_allowed").fatal
    assert c(404, "model_not_found").outcome == "client_error"
    assert c(400, "unknown_field").is_error
    assert c(502, "upstream_unavailable").outcome == "unavailable" and c(503, "vault_locked").throttled


def test_the_llm_sender_posts_to_the_data_plane_and_counts_budget_as_expected():
    seen = []

    def handler(req):
        seen.append((req.url.path, req.headers["authorization"]))
        if len(seen) % 3 == 0:
            return httpx.Response(429, json={"error": {"message": "x", "type": "t", "code": "budget_exceeded"}}, headers={"Retry-After": "30"})
        return httpx.Response(200, json={"id": "x"})
    s = llmgw.LlmgwSender(llmgw.validate({}), Http("https://gw.example", "lg_SENTINEL", transport=httpx.MockTransport(handler)), random.Random(1))
    res = arun(s.run_batch(9, 3))
    got = Counter(r.outcome for r in res)
    assert got == {"ok": 6, "budget_exceeded": 3} and not any(r.is_error for r in res)
    assert {p for p, _ in seen} <= {"/v1/chat/completions", "/v1/embeddings"} and all(a == "Bearer lg_SENTINEL" for _, a in seen)


# ========================================================= PagerLens =============================================================
def pl_calls(ticks=1, **params):
    s = pagerlens.PagerlensSender(pagerlens.validate(params), None, random.Random(3))
    out = []
    for _ in range(ticks):
        out.append(s.plan(3))
    return s, out


def test_every_pagerlens_call_matches_the_ingest_rules():
    try:
        real = targets.real_pagerlens_ingest()
    except pytest.skip.Exception:
        real = None
    for scenario in pagerlens.SCENARIOS:
        s, ticks = pl_calls(6, scenario=scenario)
        for calls in ticks:
            for call in calls:
                targets.check_pagerlens(call.path, call.body)
                svc = call.body.get("service") or call.body["name"]
                assert (real["NAME"] if real else targets.SERVICE_NAME).match(svc)
                for pt in call.body.get("points", []):
                    assert (real["METRIC_NAME"] if real else targets.METRIC_NAME).match(pt["name"])
                text = json.dumps(call.body)
                assert not EMAIL.search(text) and not IPV4.search(text)


def test_services_register_once_with_their_dependencies_and_a_baseline_deploy():
    s, ticks = pl_calls(3, prefix="sim-")
    first = [c for c in ticks[0] if c.path == "/api/ingest/services"]
    assert len(first) == 5 and not any(c.path == "/api/ingest/services" for t in ticks[1:] for c in t)
    deps = {c.body["name"]: c.body["depends_on"] for c in first}
    assert deps["sim-checkout"] == ["sim-payments", "sim-db"] and deps["sim-db"] == []
    assert sum(c.path == "/api/ingest/deploys" for t in ticks for c in t) == 1
    for t in ticks:
        assert sum(c.path == "/api/ingest/metrics" for c in t) == 5 and sum(c.path == "/api/ingest/logs" for c in t) == 5


def metric(calls, service, name):
    for c in calls:
        if c.path == "/api/ingest/metrics" and c.body["service"] == service:
            return next(p["value"] for p in c.body["points"] if p["name"] == name)
    return None


def test_failure_scenarios_change_the_signals_of_the_target():
    healthy = [metric(t, "sim-checkout", "error_rate") for t in pl_calls(8)[1]]
    assert max(healthy) < 1.5
    s, ticks = pl_calls(8, scenario="bad_deploy")
    errs = [metric(t, "sim-checkout", "error_rate") for t in ticks]
    assert min(errs) > 15 and metric(ticks[0], "sim-web", "error_rate") < 12 and metric(ticks[0], "sim-web", "error_rate") > 2
    deploys = [c for t in ticks for c in t if c.path == "/api/ingest/deploys"]
    assert len(deploys) == 2 and deploys[1].body["service"] == "sim-checkout" and deploys[1].body["version"] == "v1.1.0"
    logs = [e["message"] for t in ticks for c in t if c.path == "/api/ingest/logs" and c.body["service"] == "sim-checkout" for e in c.body["events"]]
    assert sum(m.startswith("ERROR") for m in logs) > len(logs) / 2
    alerts = [c for t in ticks for c in t if c.path == "/api/ingest/alerts"]
    assert len(alerts) == 1 and alerts[0].body["name"] == "HighErrorRate" and alerts[0].body["service"] == "sim-checkout"
    lat = [metric(t, "sim-checkout", "latency_p95_ms") for t in pl_calls(4, scenario="latency")[1]]
    assert min(lat) > 800 and max(metric(t, "sim-checkout", "error_rate") for t in pl_calls(4, scenario="latency")[1]) < 1.5
    silent = pl_calls(3, scenario="silence")[1]
    assert all(metric(t, "sim-checkout", "error_rate") is None and metric(t, "sim-web", "error_rate") is not None for t in silent)
    assert not any(c.path == "/api/ingest/alerts" for t in silent for c in t)


def test_the_scenario_can_be_switched_live_and_a_target_chosen():
    s = pagerlens.PagerlensSender(pagerlens.validate({"target_service": "db"}), None, random.Random(1))
    s.plan(1)
    assert metric(s.plan(1), "sim-db", "error_rate") < 1.5
    s.set_scenario("error_spike")
    calls = s.plan(1)
    assert metric(calls, "sim-db", "error_rate") > 15 and metric(calls, "sim-checkout", "error_rate") > 2     # dependents see errors
    assert s.extra()["scenario"] == "error_spike"
    with pytest.raises(SpecError):
        s.set_scenario("meltdown")
    s.set_scenario("healthy")
    assert not any(c.path == "/api/ingest/alerts" for c in s.plan(1))


def test_pagerlens_params_and_alerts_can_be_switched_off():
    for bad in ({"scenario": "x"}, {"target_service": "nope"}, {"prefix": "Bad Prefix"}, {"alert_after": -1}, {"y": 1}):
        with pytest.raises(SpecError):
            pagerlens.validate(bad)
    s, ticks = pl_calls(8, scenario="error_spike", alert=False)
    assert not any(c.path == "/api/ingest/alerts" for t in ticks for c in t)


def test_the_pagerlens_sender_counts_requests_and_an_ingest_refusal_is_fatal():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={"ok": True})
    s = pagerlens.PagerlensSender(pagerlens.validate({}), Http("https://pl.example", "ing_x", transport=httpx.MockTransport(handler)), random.Random(1))
    res = arun(s.run_batch(2, 4))
    assert len(res) == len(seen) == 5 + 1 + 10 and all(r.outcome == "ok" for r in res) and s.extra()["deploys"] == 1
    refusing = pagerlens.PagerlensSender(pagerlens.validate({}), Http("https://pl.example", "bad", transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"detail": "forbidden"}))), random.Random(1))
    assert all(r.fatal and r.outcome == "auth_error" for r in arun(refusing.run_batch(1, 2)))


# ========================================================= identity ==============================================================
ISS = "https://id.example.org"


class IdentityMock:
    def __init__(self, token_endpoint=f"{ISS}/oauth/token", jwks=f"{ISS}/jwks.json", token_status=200):
        self.calls, self.doc, self.token_status = [], {"issuer": ISS, "token_endpoint": token_endpoint, "jwks_uri": jwks}, token_status

    def handler(self, req):
        self.calls.append((req.method, req.url.path, req.headers.get("authorization", ""), req.content.decode()))
        if req.url.path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.doc)
        if req.url.path == "/jwks.json":
            return httpx.Response(200, json={"keys": []})
        if req.url.path == "/oauth/token":
            return httpx.Response(self.token_status, json={"access_token": "SECRET-ACCESS-TOKEN", "expires_in": 300})
        return httpx.Response(404)


def id_sender(mock, **params):
    return identity.IdentitySender(identity.validate(params), Http(ISS, transport=httpx.MockTransport(mock.handler)), random.Random(1), ISS)


def test_identity_probe_mixes_tokens_discovery_and_jwks(monkeypatch):
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET", "svc-secret-SENTINEL")
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_ID", "cpsim-service")
    mock = IdentityMock()
    s = id_sender(mock, scope="pagerlens.ingest")
    res = arun(s.run_batch(20, 2))
    assert all(r.outcome == "ok" for r in res) and len(res) == 20
    paths = Counter(c[1] for c in mock.calls)
    assert paths["/oauth/token"] > 3 and paths["/jwks.json"] > 0 and paths["/.well-known/openid-configuration"] > 0
    tok = next(c for c in mock.calls if c[1] == "/oauth/token")
    assert tok[2].startswith("Basic ") and "grant_type=client_credentials" in tok[3] and "scope=pagerlens.ingest" in tok[3]
    assert "SECRET-ACCESS-TOKEN" not in repr(res) and "SENTINEL" not in repr(res), "the access token is discarded unread"


def test_identity_secret_goes_only_to_the_providers_own_origin(monkeypatch):
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET", "svc-secret-SENTINEL")
    evil = IdentityMock(token_endpoint="https://evil.example/oauth/token")
    s = id_sender(evil, mix={"token": 100, "discovery": 0, "jwks": 0})
    res = arun(s.run_batch(2, 1))
    assert all(r.outcome == "auth_error" and r.fatal for r in res)
    assert not any(c[1] == "/oauth/token" for c in evil.calls), "the secret was never sent"


def test_identity_refusal_and_missing_secret_are_fatal(monkeypatch):
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET", "x")
    res = arun(id_sender(IdentityMock(token_status=401), mix={"token": 100, "discovery": 0, "jwks": 0}).run_batch(1, 1))
    assert res[0].fatal and res[0].outcome == "auth_error"
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET_FILE", "/nonexistent")
    monkeypatch.delenv("CPSIM_SERVICE_CLIENT_SECRET")
    res = arun(id_sender(IdentityMock(), mix={"token": 100, "discovery": 0, "jwks": 0}).run_batch(1, 1))
    assert res[0].fatal and "CPSIM_SERVICE_CLIENT_SECRET" in res[0].error


def test_identity_is_kept_modest():
    for bad in ({"schedule": {"interval": 0.5}}, {"schedule": {"batch": 21}}):
        with pytest.raises(SpecError):
            parse_workload({"name": "i", "kind": "identity", "target_url": ISS, **bad})
    assert parse_workload({"name": "i", "kind": "identity", "target_url": ISS, "schedule": {"interval": 1, "batch": 20}}).schedule.batch == 20


# ========================================================= TrustDesk and CityPulse ================================================
def test_a_trustdesk_workload_advances_a_fixed_seed_per_run_and_keeps_a_random_one_random():
    from cpsim.hr_model import parse_company
    from cpsim.kinds.trustdesk import next_company
    fixed = parse_company({"name": "acme", "seed": 5, "employees": {"new": 1}, "requests": {"vacation": 1}})
    assert [next_company(fixed, i).seed for i in range(3)] == [5, 6, 7] and fixed.seed == 5
    assert next_company(fixed.__class__(**{**fixed.__dict__, "seed": 2**32 - 1}), 1).seed == 0
    rnd = parse_company({"name": "acme", "employees": {"new": 1}, "requests": {"vacation": 1}})
    assert next_company(rnd, 3).seed is None


def test_a_citypulse_entry_becomes_a_live_feed_city():
    spec = live_feed("feed", "https://cp.example", interval=2.0, sensors=60, seed=1)
    assert sum(g["count"] for g in spec["groups"]) == 60 and all(g["frequency"] == 2.0 and g["protocol"] == "direct" for g in spec["groups"])
    city = city_from_workload({"name": "feed", "kind": "citypulse", "target_url": "https://cp.example", "schedule": {"interval": 5}, "params": {"sensors": 100}})
    assert city.sensor_count() == 100 and city.groups[0].frequency == 5.0 and city.poll_interval == 5.0
    for bad in ({"schedule": {"interval": 0.5}}, {"schedule": {"batch": 2}}, {"params": {"sensors": 3}}, {"params": {"x": 1}}):
        with pytest.raises(SpecError):
            city_from_workload({"name": "feed", "kind": "citypulse", **bad})


def test_the_generic_status_mapping():
    assert classify_status(204).outcome == "ok" and classify_status(404).outcome == "client_error"
    assert classify_status(401).fatal and classify_status(500).outcome == "server_error" and classify_status(504).outcome == "unavailable"
    assert Result("ok").is_error is False and Result("network_error").is_error


def test_every_kind_has_defaults_that_validate_and_fields_the_ui_can_render():
    for k in kinds.KINDS.values():
        assert k.defaults() == (k.validate(k.defaults()) if k.defaults() else {})
        for f in k.fields:
            assert f["type"] in ("number", "text", "list", "bool", "select") and f["label"]
        for p in k.presets:
            sched = {**k.schedule, **p["schedule"]}
            params = {"company": "acme"} if k.name == "trustdesk" else p["params"]
            assert parse_workload({"name": "p", "kind": k.name, "target_url": "https://x.example", "schedule": sched, "params": params})
    with pytest.raises(SpecError, match="Cities"):
        kinds.kind_of("citypulse")


def test_a_customer_leaves_after_a_few_payments_so_a_long_run_never_looks_like_a_velocity_attack(created):
    """PayBroker's fraud model declines a customer with ~5 payments an hour (and a card, device or address with as many), so a small
    pool reused for hours ended with every payment declined. Each shopper now pays a few times and is replaced by a new one."""
    state, ops, _ = make_ops(2000, seed=11, payments_per_customer=3, mix={"regular": 100})
    per_customer: dict[str, int] = {}
    for op in ops:
        if op.scenario == "card_testing":      # the probes are three fixed identities on purpose
            continue
        cid = op.body["customer"]["id"]
        per_customer[cid] = per_customer.get(cid, 0) + 1
    assert max(per_customer.values()) <= 3, "nobody pays more than three times"
    assert len(per_customer) > 600, "about a third as many shoppers as payments, not the same 60 again and again"
    shoppers = [c for c in created]
    assert len({c.card for c in shoppers}) == len(shoppers) and len({c.device for c in shoppers}) == len(shoppers)
    assert len({c.ip for c in shoppers}) == len(shoppers), "every shopper has an address of their own"
    assert all(c.ip.startswith("2001:db8:") for c in shoppers), "documentation range only (RFC 3849)"
    assert len(state.customers) == 60, "the pool keeps its size"


def test_payments_per_customer_is_validated_and_defaults_to_three():
    assert paybroker.validate({})["payments_per_customer"] == 3
    assert paybroker.validate({"payments_per_customer": 1})["payments_per_customer"] == 1
    for bad in (0, 21, "many"):
        with pytest.raises(kinds.SpecError):
            paybroker.validate({"payments_per_customer": bad})
