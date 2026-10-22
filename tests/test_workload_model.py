"""Workload definitions: strict validation, secrets only by environment variable NAME, limits, targets, credentials."""
import json

import pytest

from cpsim import kinds
from cpsim.model import SpecError
from cpsim.wl_model import check_secret_env, parse_workload

BASE = {"name": "shop", "kind": "paybroker", "target_url": "https://pb.example"}


def test_a_minimal_workload_gets_the_kinds_defaults():
    w = parse_workload(BASE)
    assert (w.token_env, w.publishable_env) == ("PAYBROKER_SECRET_KEY", "") and w.schedule.interval == 5.0 and w.schedule.batch == 1
    assert w.params["mix"]["regular"] == 74.0 and w.params["capture_method"] is None
    assert parse_workload({**BASE, "kind": "llmgw"}).token_env == "LLMGW_CLIENT_KEY"
    assert parse_workload({**BASE, "kind": "pagerlens"}).token_env == "PAGERLENS_TOKEN"
    assert parse_workload({**BASE, "kind": "identity"}).token_env == ""
    assert parse_workload({**BASE, "kind": "trustdesk", "target_url": "", "params": {"company": "acme"}}).params == {"company": "acme"}


def test_the_stored_form_round_trips_and_holds_no_secret(monkeypatch):
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_SUPERSECRETVALUE")
    w = parse_workload({**BASE, "publishable_env": "PAYBROKER_PUBLISHABLE_KEY", "schedule": {"interval": 2, "batch": 3, "jitter_pct": 10}})
    d = w.to_dict()
    assert parse_workload(d).to_dict() == d
    assert "SUPERSECRETVALUE" not in json.dumps(d) and d["token_env"] == "PAYBROKER_SECRET_KEY" and d["publishable_env"] == "PAYBROKER_PUBLISHABLE_KEY"


def test_a_credential_value_pasted_where_a_name_belongs_is_refused():
    for bad in ("sk_test_abc123secretvalue", "lg_abcdef", "Bearer abc", "paybroker_secret_key", "has space TOKEN", "NOCREDENTIALWORD",
                "SOME_TOKEN_FILE", "x" * 80 + "_KEY"):
        with pytest.raises(SpecError):
            parse_workload({**BASE, "token_env": bad})


def test_the_simulators_own_and_the_databases_variables_can_never_be_named():
    for bad in ("CPSIM_API_KEY", "CPSIM_SERVICE_CLIENT_SECRET", "CPSIM_METRICS_TOKEN", "DATABASE_URL_KEY", "DB_EXTERNAL_DSN_SECRET",
                "PSQL_CONNECTION_STRING_KEY", "PGPASSWORD_KEY", "POSTGRES_PASSWORD_SECRET", "AGENTPG_API_KEY"):
        with pytest.raises(SpecError):
            check_secret_env(bad)
    assert check_secret_env("MY_SHOP_SECRET_KEY") == "MY_SHOP_SECRET_KEY"


def test_only_the_kinds_that_take_a_credential_take_a_variable_name():
    with pytest.raises(SpecError):
        parse_workload({**BASE, "kind": "identity", "token_env": "SOME_TOKEN"})
    with pytest.raises(SpecError):
        parse_workload({**BASE, "kind": "llmgw", "publishable_env": "X_KEY"})


@pytest.mark.parametrize("bad", [
    {"name": "Bad_Name"}, {"name": ""}, {"kind": "nope"}, {"kind": "citypulse"}, {"target_url": "http://evil.example"},
    {"target_url": "https://user:pw@x.example"}, {"target_url": "https://x.example?q=1"}, {"target_url": "https://169.254.169.254"},
    {"params": []}, {"params": {"bogus": 1}}, {"schedule": {"interval": 0.1}}, {"schedule": {"batch": 501}}, {"surprise": 1},
    {"schedule": {"interval": 0.2, "batch": 100}},            # 500 items/s is over the default ceiling of 200
])
def test_invalid_workloads_are_refused(bad):
    with pytest.raises(SpecError):
        parse_workload({**BASE, **bad})
    with pytest.raises(SpecError):
        parse_workload("not an object")


def test_the_citypulse_kind_points_at_cities():
    with pytest.raises(SpecError, match="Cities"):
        parse_workload({**BASE, "kind": "citypulse"})


def test_the_global_ceiling_applies_to_one_workload(monkeypatch):
    monkeypatch.setenv("CPSIM_MAX_RATE", "1000")
    assert parse_workload({**BASE, "schedule": {"interval": 0.2, "batch": 100}}).schedule.rate() == 500.0
    monkeypatch.setenv("CPSIM_MAX_RATE", "10")
    with pytest.raises(SpecError, match="CPSIM_MAX_RATE"):
        parse_workload({**BASE, "schedule": {"interval": 1, "batch": 11}})
    assert parse_workload({**BASE, "schedule": {"interval": 1, "batch": 10}})


def test_plain_http_is_for_this_machine_unless_explicitly_allowed(monkeypatch):
    assert parse_workload({**BASE, "target_url": "http://127.0.0.1:8000/"}).target_url == "http://127.0.0.1:8000"
    with pytest.raises(SpecError):
        parse_workload({**BASE, "target_url": "http://paybroker:8000"})
    monkeypatch.setenv("CPSIM_ALLOW_HTTP_TARGETS", "true")
    assert parse_workload({**BASE, "target_url": "http://paybroker:8000"}).target_url == "http://paybroker:8000"
    with pytest.raises(SpecError):
        parse_workload({**BASE, "target_url": "http://169.254.169.254"})      # link-local stays refused


def test_credentials_come_from_the_environment_or_the_identity_provider(monkeypatch):
    w = parse_workload(BASE)
    monkeypatch.delenv("PAYBROKER_SECRET_KEY", raising=False)
    with pytest.raises(SpecError, match="PAYBROKER_SECRET_KEY"):
        kinds.credential(w)
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_x")
    assert kinds.credential(w) == "sk_test_x"
    gw = parse_workload({**BASE, "kind": "llmgw"})
    monkeypatch.delenv("LLMGW_CLIENT_KEY", raising=False)
    monkeypatch.delenv("CPSIM_OIDC_ISSUER", raising=False)
    with pytest.raises(SpecError, match="llmgw.use"):
        kinds.credential(gw)
    monkeypatch.setenv("LLMGW_CLIENT_KEY", "lg_x")
    assert kinds.credential(gw) == "lg_x"
    assert kinds.credential(parse_workload({**BASE, "target_url": ""})) is None, "generate-only needs no credential"
    ident = parse_workload({**BASE, "kind": "identity", "target_url": "https://id.example.org"})
    monkeypatch.delenv("CPSIM_SERVICE_CLIENT_SECRET", raising=False)
    with pytest.raises(SpecError, match="CPSIM_SERVICE_CLIENT_SECRET"):
        kinds.credential(ident)


def test_an_empty_variable_falls_back_to_a_service_token_with_the_kinds_scope(monkeypatch):
    from cpsim import tokens
    monkeypatch.setenv("CPSIM_OIDC_ISSUER", "https://id.example.org")
    monkeypatch.setenv("CPSIM_SERVICE_CLIENT_SECRET", "svc")
    monkeypatch.setattr(tokens, "_sources", {})
    monkeypatch.delenv("LLMGW_CLIENT_KEY", raising=False)
    monkeypatch.delenv("PAGERLENS_TOKEN", raising=False)
    gw = kinds.credential(parse_workload({**BASE, "kind": "llmgw"}))
    pl = kinds.credential(parse_workload({**BASE, "kind": "pagerlens"}))
    assert callable(gw) and callable(pl)
    assert {k[2] for k in tokens._sources} == {"llmgw.use", "pagerlens.ingest"}
    monkeypatch.setenv("LLMGW_CLIENT_KEY", "lg_static")
    assert kinds.credential(parse_workload({**BASE, "kind": "llmgw"})) == "lg_static", "a static key wins"
