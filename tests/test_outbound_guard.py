"""Where the simulator may send requests and which variables it may send as credentials."""
import pytest
from apgkit.netguard import NetGuard

from cpsim import credentials, kinds, outbound
from cpsim.model import SpecError, parse_city
from cpsim.runtime import Runtime
from cpsim.wl_model import parse_workload

WL = {"name": "w", "kind": "paybroker", "target_url": "https://pb.example", "schedule": {"interval": 1, "batch": 1}}


@pytest.fixture
def clean(monkeypatch):
    for v in ("CPSIM_TARGET_ALLOWLIST", "CPSIM_TOKEN_ENV_ALLOWLIST", "CPSIM_ALLOW_PRIVATE_URLS", "LLM_API_KEY", "PAYBROKER_SECRET_KEY",
              "PAYBROKER_URL", "CITYPULSE_URL", "LLMGW_URL"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(credentials, "_stored", {})
    monkeypatch.setattr(credentials, "_origins", {})


# ---- targets ---------------------------------------------------------------------------------------------------------------
def test_the_default_allow_list_is_the_ecosystem_urls_given_by_environment(clean, monkeypatch):
    assert outbound.allowlist() == []
    monkeypatch.setenv("PAYBROKER_URL", "https://pb-1.sandbox.example/")
    monkeypatch.setenv("CITYPULSE_URL", "https://cp-1.sandbox.example")
    assert outbound.host_allowed("pb-1.sandbox.example") and outbound.host_allowed("CP-1.sandbox.example")
    assert not outbound.host_allowed("evil.example") and not outbound.host_allowed("")
    with pytest.raises(SpecError, match="not an allowed target"):
        outbound.check_allowed("https://evil.example")


def test_extra_hosts_and_suffixes(clean, monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "stub.internal, .corp.example *.lab.example")
    for ok in ("stub.internal", "a.corp.example", "x.y.lab.example"):
        assert outbound.host_allowed(ok), ok
    for bad in ("corp.example", "evilcorp.example", "stub.internal.evil.example", "xstub.internal"):
        assert not outbound.host_allowed(bad), bad


def test_a_start_refuses_a_target_that_is_not_allow_listed(clean, monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "")
    wl = parse_workload(WL)                                    # saving is fine; the guard acts when something starts
    with pytest.raises(SpecError, match="not an allowed target"):
        Runtime.resolved(wl, False)
    monkeypatch.setenv("PAYBROKER_URL", "https://pb.example")
    assert Runtime.resolved(wl, False).target_url == "https://pb.example"
    city = parse_city({"name": "c", "target_url": "https://other.example", "groups": [{"type": "air_quality", "count": 1}]})
    with pytest.raises(SpecError, match="other.example"):
        Runtime.resolved(city, False)


def test_the_netguard_refuses_private_and_link_local_addresses(clean, monkeypatch):
    monkeypatch.setattr(outbound, "guard", NetGuard("CPSIM_ALLOW_PRIVATE_URLS"))
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "localhost 127.0.0.1 169.254.169.254")
    for url in ("http://127.0.0.1:8000", "https://localhost", "http://169.254.169.254"):
        with pytest.raises(SpecError, match="refused"):
            outbound.check_url(url)
    monkeypatch.setenv("CPSIM_ALLOW_PRIVATE_URLS", "true")
    assert outbound.check_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    with pytest.raises(SpecError, match="link-local"):
        outbound.check_url("http://169.254.169.254")           # never allowed, whatever the switch says


def test_a_hostname_that_resolves_to_a_private_address_is_refused(clean, monkeypatch):
    import socket

    monkeypatch.setattr(outbound, "guard", NetGuard("CPSIM_ALLOW_PRIVATE_URLS"))
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "rebind.example")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))])
    with pytest.raises(SpecError, match="private"):
        outbound.check_url("https://rebind.example")


# ---- which variables become a bearer token --------------------------------------------------------------------------------------
def test_token_env_is_an_explicit_allow_list(clean, monkeypatch):
    assert parse_workload(WL).token_env == "PAYBROKER_SECRET_KEY"
    for name in ("LLM_API_KEY", "CPSIM_ENCRYPTION_KEY", "IDENTITY_ENCRYPTION_KEY", "SOME_OTHER_TOKEN", "AWS_SECRET_ACCESS_KEY"):
        with pytest.raises(SpecError, match="token_env"):
            parse_workload({**WL, "token_env": name})
    with pytest.raises(SpecError):
        parse_workload({**WL, "publishable_env": "LLM_API_KEY"})
    monkeypatch.setenv("CPSIM_TOKEN_ENV_ALLOWLIST", "SOME_OTHER_TOKEN LLM_API_KEY FOO_ENCRYPTION_KEY CPSIM_SECRET")
    assert parse_workload({**WL, "token_env": "SOME_OTHER_TOKEN"}).token_env == "SOME_OTHER_TOKEN"
    for never in ("LLM_API_KEY", "FOO_ENCRYPTION_KEY", "CPSIM_SECRET"):          # the operator's list cannot name the app's own secrets
        with pytest.raises(SpecError):
            parse_workload({**WL, "token_env": never})


def test_cities_and_companies_use_the_same_list(clean):
    from cpsim.hr_model import parse_company
    with pytest.raises(SpecError, match="not an allowed credential variable"):
        parse_city({"name": "c", "token_env": "LLM_API_KEY_TOKEN", "groups": []})
    with pytest.raises(SpecError, match="not an allowed credential variable"):
        parse_company({"name": "co", "token_env": "SECRET_TOKEN", "employees": {"new": 1}, "requests": {"vacation": 1}})


def test_a_refused_variable_is_never_read_or_echoed(clean, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "lg_VALUE_THAT_MUST_NOT_LEAK")
    with pytest.raises(SpecError) as exc:
        credentials.get("LLM_API_KEY", "https://pb.example")
    assert "VALUE_THAT_MUST_NOT_LEAK" not in str(exc.value)
    assert credentials.source("LLM_API_KEY") == "refused"


def test_llm_api_key_goes_only_to_the_gateway_this_app_was_given(clean, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "lg_gw")
    wl = parse_workload({"name": "g", "kind": "llmgw", "target_url": "https://gw.example", "schedule": {"interval": 5, "batch": 1}})
    with pytest.raises(SpecError):
        kinds.credential(wl)
    monkeypatch.setenv("LLMGW_URL", "https://evil.example")
    with pytest.raises(SpecError):
        kinds.credential(wl)
    monkeypatch.setenv("LLMGW_URL", "https://gw.example/")
    assert kinds.credential(wl) == "lg_gw"


# ---- saved credentials are bound to the origin they were saved for ----------------------------------------------------------------
def test_a_saved_credential_goes_only_to_its_origin(clean, monkeypatch):
    credentials._stored["PAYBROKER_SECRET_KEY"] = "sk_test_saved"
    credentials._origins["PAYBROKER_SECRET_KEY"] = "https://pb.example"
    assert credentials.get("PAYBROKER_SECRET_KEY", "https://pb.example") == "sk_test_saved"
    assert credentials.get("PAYBROKER_SECRET_KEY", "https://PB.example:443") == "sk_test_saved"
    for target in ("https://evil.example", "http://pb.example", "https://pb.example:8443", "https://pb.example.evil.example"):
        with pytest.raises(SpecError, match="bound to https://pb.example") as exc:
            credentials.get("PAYBROKER_SECRET_KEY", target)
        assert "sk_test_saved" not in str(exc.value)
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_env")        # an environment value is the operator's, and wins
    assert credentials.get("PAYBROKER_SECRET_KEY", "https://pb.example") == "sk_test_env"


def test_a_saved_value_without_a_stored_origin_follows_its_own_app_or_goes_nowhere(clean, monkeypatch):
    credentials._stored["PAYBROKER_SECRET_KEY"] = "sk_test_old"           # saved before origins existed: bound to PAYBROKER_URL
    with pytest.raises(SpecError):
        credentials.get("PAYBROKER_SECRET_KEY", "https://pb.example")        # no PAYBROKER_URL on this app: nowhere
    monkeypatch.setenv("PAYBROKER_URL", "https://pb.example")
    assert credentials.get("PAYBROKER_SECRET_KEY", "https://pb.example") == "sk_test_old"
    with pytest.raises(SpecError):
        credentials.get("PAYBROKER_SECRET_KEY", "https://evil.example")
    monkeypatch.setenv("CPSIM_TOKEN_ENV_ALLOWLIST", "CUSTOM_TOKEN")
    credentials._stored["CUSTOM_TOKEN"] = "x"
    with pytest.raises(SpecError):
        credentials.get("CUSTOM_TOKEN", "https://pb.example")                # a custom name has no default app


def test_the_origin_to_save_with(clean, monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "pb.example")
    monkeypatch.setenv("CPSIM_TOKEN_ENV_ALLOWLIST", "CUSTOM_TOKEN")
    assert credentials.clean_origin("PAYBROKER_SECRET_KEY", None) == "${PAYBROKER_URL}"
    assert credentials.clean_origin("PAYBROKER_SECRET_KEY", "https://pb.example/v1/x") == "https://pb.example"
    with pytest.raises(SpecError, match="say where"):
        credentials.clean_origin("CUSTOM_TOKEN", None)
    with pytest.raises(SpecError, match="not an allowed target"):
        credentials.clean_origin("CUSTOM_TOKEN", "https://evil.example")
    with pytest.raises(SpecError):
        credentials.clean_origin("CUSTOM_TOKEN", "not a url")


# ---- proxy trust --------------------------------------------------------------------------------------------------------------------
def test_forwarded_allow_ips_is_never_a_wildcard_by_default(monkeypatch):
    from cpsim import cli
    monkeypatch.delenv("CPSIM_FORWARDED_ALLOW_IPS", raising=False)
    assert cli.forwarded_allow_ips() == "127.0.0.1"
    monkeypatch.setenv("CPSIM_FORWARDED_ALLOW_IPS", "10.1.2.3")
    assert cli.forwarded_allow_ips() == "10.1.2.3"
