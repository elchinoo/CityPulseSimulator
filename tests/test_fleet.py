"""Fleet references (`${CITYPULSE_URL}`): validation, resolution at start (never stored), the URL rules on the resolved address,
the generator defaults and the headless parity. No Postgres needed."""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cpsim import citygen, fleet, presets, runtime
from cpsim.hr_model import parse_company
from cpsim.model import SpecError, parse_city
from cpsim.runtime import Runtime
from cpsim.store import MemoryStore
from cpsim.wl_model import parse_workload

ROOT = Path(__file__).resolve().parents[1]
GROUP = {"type": "temperature", "count": 8}
REQUESTS = {"employees": {"new": 1, "tenured": 1}, "requests": {"vacation": 2}}
WL = {"kind": "paybroker", "schedule": {"interval": 1, "batch": 1}}


def city(url="${CITYPULSE_URL}"):
    return parse_city({"name": "metro", "target_url": url, "groups": [GROUP]})


# ---- validation --------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", fleet.FLEET_VARS)
def test_every_allow_listed_name_is_accepted_by_cities_companies_and_workloads_and_stored_as_written(name):
    ref = "${" + name + "}"
    assert parse_city({"name": "m", "target_url": ref, "groups": [GROUP]}).to_dict()["target_url"] == ref
    assert parse_company({"name": "acme", "target_url": ref, **REQUESTS}).to_dict()["target_url"] == ref
    assert parse_workload({"name": "w", **WL, "target_url": ref}).to_dict()["target_url"] == ref


@pytest.mark.parametrize("bad", [
    "${HOME}", "${DATABASE_URL}", "${CPSIM_ENCRYPTION_KEY}", "${citypulse_url}", "${CITYPULSE_URL:-https://x.example}",      # not the six, or a default
    "https://${CITYPULSE_URL}", "${CITYPULSE_URL}/api", " ${CITYPULSE_URL}", "${CITYPULSE_URL} ", "${CITYPULSE_URL}${PAYBROKER_URL}",   # partial use
    "${${CITYPULSE_URL}}", "${CITYPULSE_${PAYBROKER_URL}}", "${CITYPULSE_URL", "${}", "${", "x${CITYPULSE_URL}", "$(CITYPULSE_URL)x${A}"])
def test_anything_but_exactly_one_allow_listed_reference_is_refused_everywhere(bad):
    with pytest.raises(SpecError, match="fleet reference"):
        parse_city({"name": "m", "target_url": bad, "groups": [GROUP]})
    with pytest.raises(SpecError, match="fleet reference"):
        parse_company({"name": "acme", "target_url": bad, **REQUESTS})
    with pytest.raises(SpecError, match="fleet reference"):
        parse_workload({"name": "w", **WL, "target_url": bad})


def test_plain_urls_are_still_judged_by_the_usual_rules():
    assert city("https://cp.example/").target_url == "https://cp.example"
    assert city("http://localhost:9000").target_url == "http://localhost:9000"
    assert city("").target_url == ""
    with pytest.raises(SpecError, match="https"):
        city("http://cp.example")


# ---- resolution ----------------------------------------------------------------------------------------
def test_resolve_leaves_literals_alone_and_reads_the_environment_for_a_reference(monkeypatch):
    monkeypatch.setenv("CITYPULSE_URL", " https://cp-abc.example/ ")
    assert fleet.resolve("https://other.example") == "https://other.example" and fleet.resolve("") == ""
    assert fleet.resolve("${CITYPULSE_URL}") == "https://cp-abc.example"
    monkeypatch.setenv("CITYPULSE_URL", "https://cp-def.example")             # the app was recreated: the next resolve follows
    assert fleet.resolve("${CITYPULSE_URL}") == "https://cp-def.example"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_reference_says_what_to_do(monkeypatch, value):
    monkeypatch.delenv("CITYPULSE_URL", raising=False)
    if value is not None:
        monkeypatch.setenv("CITYPULSE_URL", value)
    with pytest.raises(SpecError) as e:
        fleet.resolve("${CITYPULSE_URL}")
    assert str(e.value) == "CITYPULSE_URL is not set on this app; set the URL by hand or deploy it with ecoctl"


@pytest.mark.parametrize("value, allow_http, ok", [
    ("https://cp.example", False, True), ("http://localhost:8000", False, True), ("http://127.0.0.1:8000", False, True),
    ("http://cp.example", False, False), ("http://cp.example", True, True),               # CPSIM_ALLOW_HTTP_TARGETS (workloads)
    ("http://169.254.169.254", True, False), ("https://169.254.169.254", False, False),     # link-local, even over https or with the opt-in
    ("https://user:pw@cp.example", False, False), ("https://cp.example/?x=1", False, False), ("ftp://cp.example", True, False),
    ("not a url", False, False)])
def test_the_url_rules_apply_to_the_resolved_address(monkeypatch, value, allow_http, ok):
    monkeypatch.setenv("CITYPULSE_URL", value)
    if ok:
        assert fleet.resolve("${CITYPULSE_URL}", allow_http)
    else:
        with pytest.raises(SpecError, match="CITYPULSE_URL"):
            fleet.resolve("${CITYPULSE_URL}", allow_http)


def test_describe_and_status_show_the_reference_and_the_address(monkeypatch):
    monkeypatch.setenv("CITYPULSE_URL", "https://cp.example")
    assert fleet.describe("${CITYPULSE_URL}") == {"target_url": "${CITYPULSE_URL}", "reference": "CITYPULSE_URL",
                                                    "resolved_url": "https://cp.example", "resolves": True, "problem": ""}
    d = fleet.describe("${PAYBROKER_URL}")
    assert d["resolves"] is False and d["resolved_url"] == "" and "PAYBROKER_URL is not set" in d["problem"]
    assert fleet.describe("https://x.example")["reference"] is None and fleet.describe("")["resolves"] is False
    monkeypatch.setenv("PAYBROKER_URL", "http://insecure.example")
    assert fleet.describe("${PAYBROKER_URL}")["resolves"] is False                    # unusable counts as not resolving
    st = {v["name"]: v for v in fleet.status()}
    assert list(st) == list(fleet.FLEET_VARS)
    assert st["CITYPULSE_URL"] == {"name": "CITYPULSE_URL", "label": "CityPulse", "set": True, "value": "https://cp.example"}
    assert st["LLMGW_URL"]["set"] is False and st["LLMGW_URL"]["value"] == ""


# ---- resolved when something starts, never stored ----------------------------------------------------
class Spy:
    sinks = []

    def __init__(self, url, token):
        self.url, self.token = url, token
        Spy.sinks.append(self)

    async def send(self, readings):
        return None

    async def close(self):
        return None


def test_a_city_resolves_at_start_and_the_store_keeps_the_reference(monkeypatch):
    monkeypatch.setattr(runtime, "HttpSink", Spy)
    monkeypatch.setenv("CITYPULSE_INGEST_TOKEN", "tok")
    rt = Runtime(MemoryStore())
    rt.store.save_city("metro", city().to_dict())

    async def go():
        monkeypatch.delenv("CITYPULSE_URL", raising=False)
        with pytest.raises(SpecError, match="CITYPULSE_URL is not set on this app"):
            await rt.start("metro")
        monkeypatch.setenv("CITYPULSE_URL", "https://cp-1.example")
        runner = await rt.start("metro")
        assert Spy.sinks[-1].url == "https://cp-1.example" and runner.city.target_url == "https://cp-1.example"
        await asyncio.sleep(0.05)
        await rt.stop("metro")
        monkeypatch.setenv("CITYPULSE_URL", "https://cp-2.example")                  # recreated: follows without an edit
        await rt.start("metro")
        assert Spy.sinks[-1].url == "https://cp-2.example"
        await asyncio.sleep(0.05)
        await rt.stop_all()

    asyncio.run(go())
    assert rt.store.cities()["metro"]["target_url"] == "${CITYPULSE_URL}" and rt.get("metro").target_url == "${CITYPULSE_URL}"


def test_a_city_whose_resolved_address_breaks_the_rules_does_not_start_and_no_credential_goes_out(monkeypatch):
    monkeypatch.setattr(runtime, "HttpSink", Spy)
    monkeypatch.setenv("CITYPULSE_INGEST_TOKEN", "tok")
    monkeypatch.setenv("CITYPULSE_URL", "http://cp.example")                         # plain http to another host
    rt = Runtime(MemoryStore())
    rt.store.save_city("metro", city().to_dict())
    Spy.sinks.clear()
    with pytest.raises(SpecError, match="https"):
        asyncio.run(rt.start("metro"))
    assert Spy.sinks == []


def test_a_company_resolves_for_a_run_and_for_its_schedule(monkeypatch):
    seen = []
    monkeypatch.setattr(runtime, "TrustDeskClient", lambda url, token: seen.append((url, token)) or object())
    monkeypatch.setenv("TRUSTDESK_CLIENT_TOKEN", "tok")
    rt = Runtime(MemoryStore())
    co = parse_company({"name": "acme", "target_url": "${TRUSTDESK_URL}", **REQUESTS})
    rt.store.save_company("acme", co.to_dict())
    with pytest.raises(SpecError, match="TRUSTDESK_URL is not set on this app"):
        rt._hr_client_factory(rt.company("acme"))
    monkeypatch.setenv("TRUSTDESK_URL", "https://td-9.example/")
    rt._hr_client_factory(rt.company("acme"))()
    assert seen == [("https://td-9.example", "tok")]
    assert rt.store.companies()["acme"]["target_url"] == "${TRUSTDESK_URL}"
    monkeypatch.setenv("TRUSTDESK_URL", "http://td.example")
    with pytest.raises(SpecError, match="https"):
        rt._hr_client_factory(rt.company("acme"))


def test_a_workload_resolves_at_start_with_the_http_opt_in_and_stores_the_reference(monkeypatch):
    monkeypatch.setenv("PAYBROKER_SECRET_KEY", "sk_test_x")
    rt = Runtime(MemoryStore())
    wl = parse_workload({"name": "shop", **WL, "target_url": "${PAYBROKER_URL}"})
    rt.store.save_workload("shop", wl.to_dict())
    built = []
    monkeypatch.setattr(runtime.kinds, "build_sender", lambda w, **kw: built.append((w.target_url, kw["token"])) or object())

    def sender_url():
        rt._runner(rt.workload("shop")).make_sender()
        return built[-1]

    with pytest.raises(SpecError, match="PAYBROKER_URL is not set on this app; set the URL by hand or deploy it with ecoctl"):
        rt._runner(rt.workload("shop"))
    monkeypatch.setenv("PAYBROKER_URL", "https://pb-1.example/")
    assert sender_url() == ("https://pb-1.example", "sk_test_x")                      # the credential goes to the resolved URL only
    monkeypatch.setenv("PAYBROKER_URL", "http://paybroker:8000")
    with pytest.raises(SpecError, match="https"):
        rt._runner(rt.workload("shop"))
    monkeypatch.setenv("CPSIM_ALLOW_HTTP_TARGETS", "true")                            # the explicit opt-in applies to the resolved URL
    assert sender_url()[0] == "http://paybroker:8000"
    assert rt.store.workloads()["shop"]["target_url"] == "${PAYBROKER_URL}"


def test_a_trustdesk_workload_follows_its_company_reference(monkeypatch):
    monkeypatch.setenv("TRUSTDESK_CLIENT_TOKEN", "tok")
    rt = Runtime(MemoryStore())
    rt.store.save_company("acme", parse_company({"name": "acme", "target_url": "${TRUSTDESK_URL}", **REQUESTS}).to_dict())
    rt.store.save_workload("hr", parse_workload({"name": "hr", "kind": "trustdesk", "params": {"company": "acme"},
                                                 "schedule": {"interval": 60, "batch": 1}}).to_dict())
    with pytest.raises(SpecError, match="TRUSTDESK_URL is not set"):
        rt._runner(rt.workload("hr"))
    monkeypatch.setenv("TRUSTDESK_URL", "https://td.example")
    rt._runner(rt.workload("hr"))
    assert rt.store.companies()["acme"]["target_url"] == "${TRUSTDESK_URL}"


def test_a_company_schedule_resolves_when_it_starts(monkeypatch):
    monkeypatch.setenv("TRUSTDESK_CLIENT_TOKEN", "tok")
    rt = Runtime(MemoryStore())
    co = parse_company({"name": "acme", "target_url": "${TRUSTDESK_URL}", "schedule": {"interval": 60}, **REQUESTS})
    rt.store.save_company("acme", co.to_dict())
    with pytest.raises(SpecError, match="TRUSTDESK_URL is not set"):
        rt.start_company_schedule("acme")
    monkeypatch.setenv("TRUSTDESK_URL", "https://td.example")

    captured = []

    class Stub:
        def start(self):
            pass

    monkeypatch.setattr(rt, "_runner", lambda wl, name=None: captured.append(wl) or Stub())      # no network: only what would run
    rt.start_company_schedule("acme")
    assert captured[0].target_url == "https://td.example" and rt.store.companies()["acme"]["target_url"] == "${TRUSTDESK_URL}"


# ---- generator and presets ---------------------------------------------------------------------------
def test_generated_cities_and_the_live_feed_default_to_the_fleet_when_it_is_set(monkeypatch):
    monkeypatch.delenv("CITYPULSE_URL", raising=False)
    assert citygen.generate_city(5, "small").city["target_url"] == "" and presets.live_feed("feed")["target_url"] == ""
    monkeypatch.setenv("CITYPULSE_URL", "https://cp.example")
    assert citygen.generate_city(5, "small").city["target_url"] == "${CITYPULSE_URL}"
    assert [g.city["target_url"] for g in citygen.generate_cities(3, "small", seed=1)] == ["${CITYPULSE_URL}"] * 3
    assert presets.live_feed("feed")["target_url"] == "${CITYPULSE_URL}"
    assert citygen.generate_city(5, "small", target_url="").city["target_url"] == ""          # an explicit empty stays empty
    assert citygen.generate_city(5, "small", target_url="https://x.example").city["target_url"] == "https://x.example"
    assert citygen.generate_city(5, "small", target_url="${CITYPULSE_URL}").city["target_url"] == "${CITYPULSE_URL}"
    with pytest.raises(SpecError):
        citygen.generate_city(5, "small", target_url="${HOME}")


def test_the_cli_generator_defaults_to_the_fleet_and_writes_the_reference(tmp_path):
    def run(env, *extra):
        e = {k: v for k, v in os.environ.items() if k not in fleet.FLEET_VARS}
        e.update(PYTHONPATH=str(ROOT / "src"), **env)
        return subprocess.run([sys.executable, "-m", "cpsim.cli", "cities", "generate", "--seed", "3", "--size", "small", "--out", str(tmp_path), *extra],
                              capture_output=True, text=True, env=e, timeout=60)

    assert run({"CITYPULSE_URL": "https://cp.example"}, "--name", "a").returncode == 0
    assert '"${CITYPULSE_URL}"' in (tmp_path / "a.yaml").read_text() or "${CITYPULSE_URL}" in (tmp_path / "a.yaml").read_text()
    assert run({}, "--name", "b").returncode == 0 and "target_url: ''" in (tmp_path / "b.yaml").read_text().replace('""', "''")
    assert run({}, "--name", "c", "--target-url", "${PAYBROKER_URL}").returncode == 0
    assert run({}, "--name", "d", "--target-url", "${HOME}").returncode == 2


# ---- headless parity ---------------------------------------------------------------------------------
def test_the_headless_loader_keeps_a_fleet_reference_for_start_time_and_expands_the_rest_as_before(monkeypatch, tmp_path):
    from cpsim.cli import expand_env, load_workload_file

    monkeypatch.setenv("PAYBROKER_URL", "https://pb.example")
    monkeypatch.setenv("OTHER_URL", "https://other.example")
    assert expand_env("${PAYBROKER_URL}") == "${PAYBROKER_URL}"                       # a reference: resolved at start, by the runtime
    assert expand_env("${OTHER_URL}") == "https://other.example" and expand_env("${UNSET_URL:-https://d.example}") == "https://d.example"
    assert expand_env("${PAYBROKER_URL:-https://d.example}") == "https://pb.example"      # the default form is the old expansion
    f = tmp_path / "w.json"
    f.write_text(json.dumps({"workloads": [{"name": "w", **WL, "target_url": "${PAYBROKER_URL}"}], "companies": []}))
    assert load_workload_file(str(f))[0][0]["target_url"] == "${PAYBROKER_URL}"


def test_a_headless_run_resolves_references_with_the_server_code_and_names_the_missing_variable(tmp_path):
    f = tmp_path / "w.yaml"
    f.write_text("name: shop\nkind: paybroker\ntarget_url: ${PAYBROKER_URL}\nschedule: {interval: 1, batch: 1}\n")
    base = {k: v for k, v in os.environ.items() if k not in fleet.FLEET_VARS and k != "DATABASE_URL"}
    base.update(PYTHONPATH=str(ROOT / "src"), PAYBROKER_SECRET_KEY="sk_test_x")
    r = subprocess.run([sys.executable, "-m", "cpsim.cli", "workloads", "run", str(f), "--seconds", "1"], capture_output=True, text=True, env=base, timeout=60)
    assert r.returncode == 2 and "PAYBROKER_URL is not set on this app" in r.stderr and "--dry-run" in r.stderr
    r = subprocess.run([sys.executable, "-m", "cpsim.cli", "workloads", "run", str(f), "--seconds", "1"], capture_output=True, text=True,
                       env={**base, "PAYBROKER_URL": "http://paybroker.example"}, timeout=60)
    assert r.returncode == 2 and "https" in r.stderr                                  # the URL rules apply to the resolved address
    r = subprocess.run([sys.executable, "-m", "cpsim.cli", "workloads", "validate", str(f)], capture_output=True, text=True, env=base, timeout=60)
    assert r.returncode == 0                                                          # validating needs no address
