"""The AIDB benchmark harness: statistics, the target allow-list, report shape, the scripted model server and the CLI's refusals. None of
this needs AIDB (it runs in CI). The tests at the end are marked `aidb` and skip without AIDB_TEST_URL."""
import json
import os
import threading
import time
import urllib.request

import pytest

from cpsim import cli, model
from cpsim.aidbbench import data, fakemodel, report, stats, target

SECRET = "pw-secret-for-tests-only"  # pragma: allowlist secret
DSN = f"postgresql://bench:{SECRET}@db.lab.example:5432/cpsim_bench"


# ---- statistics ----------------------------------------------------------------------------------------------------------------
def test_percentiles_match_the_textbook_values():
    v = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert stats.percentile(v, 0) == 1.0 and stats.percentile(v, 100) == 5.0 and stats.percentile(v, 50) == 3.0
    assert stats.percentile(v, 25) == 2.0 and stats.percentile(v, 90) == pytest.approx(4.6)
    assert stats.percentile([7.0], 99) == 7.0
    hundred = [float(i) for i in range(1, 101)]
    assert stats.percentile(hundred, 95) == pytest.approx(95.05) and stats.percentile(hundred, 99) == pytest.approx(99.01)
    with pytest.raises(ValueError):
        stats.percentile([], 50)
    with pytest.raises(ValueError):
        stats.percentile(v, 101)


def test_summarize_reports_milliseconds_and_the_sample_count():
    s = stats.summarize([0.010, 0.020, 0.030, 0.040, 1.0])
    assert s["n"] == 5 and s["min_ms"] == 10.0 and s["max_ms"] == 1000.0 and s["p50_ms"] == 30.0
    assert s["p95_ms"] > s["p50_ms"] and s["p99_ms"] >= s["p95_ms"] and s["mean_ms"] == pytest.approx(220.0)
    empty = stats.summarize([])
    assert empty["n"] == 0 and empty["p50_ms"] is None and empty["p99_ms"] is None


def test_recorder_counts_errors_next_to_samples_and_is_thread_safe():
    r = stats.Recorder()

    def work():
        for _ in range(500):
            r.ok(0.001)
            r.fail("boom")
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    s = r.summary()
    assert s["n"] == 2000 and s["errors"] == 2000 and s["error_kinds"] == {"boom": 2000}


def test_slope_and_describe():
    assert stats.slope([(0, 0), (1, 2), (2, 4)]) == pytest.approx(2.0)
    assert stats.slope([(0, 5)]) is None and stats.slope([(1, 1), (1, 2)]) is None
    d = stats.describe([0, 0, 4, 10])
    assert d["max"] == 10 and d["min"] == 0 and d["mean"] == 3.5 and d["n"] == 4
    assert stats.describe([])["max"] is None


# ---- the city and the corpus ---------------------------------------------------------------------------------------------------
def test_city_is_deterministic_and_capped_at_ten_thousand():
    a, b = data.build_city(120, seed=3), data.build_city(120, seed=3)
    assert len(a.sensors) == len(b.sensors) == 120 and [s.id for s in a.sensors] == [s.id for s in b.sensors]
    assert len(data.build_city(10_000).sensors) == 10_000
    for n in (7, 10_001):
        with pytest.raises(model.SpecError):
            data.build_city(n)


def test_corpus_rows_are_sentences_about_sensors_and_queries_are_questions():
    city = data.build_city(60, seed=1)
    c = data.Corpus(city, seed=1)
    rows = c.rows(150)
    assert len(rows) == 150 and all(r.body.endswith(".") and r.sensor_id in r.body and r.district == s.district for r, s in zip(rows, city.sensors * 3))
    assert len({r.sensor_id for r in rows}) == len(city.sensors) >= 50
    qs = c.queries(20)
    assert len(qs) == 20 and all(isinstance(q, str) and len(q) > 10 for q in qs)
    assert data.marker_text("abc123") != data.marker_text("abc124") and "abc123" in data.marker_text("abc123")


# ---- the target: allow-list, pinned address, database name, DSN source -----------------------------------------------------
def test_an_empty_or_missing_allowlist_refuses_everything(monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "")
    with pytest.raises(target.TargetRefused, match="empty"):
        target.resolve(DSN)
    monkeypatch.delenv("CPSIM_TARGET_ALLOWLIST")
    with pytest.raises(target.TargetRefused, match="empty"):
        target.resolve(DSN)


def test_a_host_that_is_not_listed_is_refused_and_the_message_hides_the_dsn(monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "other.lab.example")
    with pytest.raises(target.TargetRefused) as exc:
        target.resolve(DSN)
    assert "db.lab.example" in str(exc.value) and SECRET not in str(exc.value) and "postgresql://" not in str(exc.value)


def test_the_ecosystem_urls_do_not_allow_a_database_host(monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "other.lab.example")
    monkeypatch.setenv("CITYPULSE_URL", "https://db.lab.example")
    with pytest.raises(target.TargetRefused, match="not in CPSIM_TARGET_ALLOWLIST"):
        target.resolve(DSN)


@pytest.mark.parametrize("dsn", ["postgresql:///cpsim_bench", "postgresql://u@/cpsim_bench?host=/var/run/postgresql",
                                 "postgresql://u@a.lab.example,b.lab.example/cpsim_bench", "postgresql://u@db.lab.example/cpsim_bench?hostaddr=10.0.0.1"])
def test_unix_sockets_multi_host_and_a_preset_address_are_refused(monkeypatch, dsn):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "db.lab.example a.lab.example b.lab.example")
    with pytest.raises(target.TargetRefused):
        target.resolve(dsn)


def test_listed_hosts_pass_and_the_domain_pattern_works(monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", ".lab.example")
    t = target.resolve(DSN)
    assert t.database == "cpsim_bench" and "db.lab.example" in t.conninfo
    with pytest.raises(target.TargetRefused):
        target.resolve("postgresql://u:p@db.other.example/cpsim_bench")


def test_private_addresses_need_the_same_switch_as_an_http_target(monkeypatch):
    monkeypatch.undo()                                                # the real NetGuard, no offline stand-in
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "127.0.0.1 169.254.169.254")
    monkeypatch.delenv("CPSIM_ALLOW_PRIVATE_URLS", raising=False)
    with pytest.raises(target.TargetRefused, match="CPSIM_ALLOW_PRIVATE_URLS"):
        target.resolve("postgresql://u:p@127.0.0.1:5432/x_bench")
    monkeypatch.setenv("CPSIM_ALLOW_PRIVATE_URLS", "true")
    t = target.resolve("postgresql://u:p@127.0.0.1:5433/x_bench")
    assert "hostaddr=127.0.0.1" in t.conninfo and "port=5433" in t.conninfo
    with pytest.raises(target.TargetRefused, match="link-local"):
        target.resolve("postgresql://u:p@169.254.169.254/x_bench")


def test_the_database_name_must_look_throwaway(monkeypatch):
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", ".lab.example")
    with pytest.raises(target.TargetRefused, match="must end in"):
        target.resolve("postgresql://u:p@db.lab.example/production")
    assert target.resolve("postgresql://u:p@db.lab.example/production", allow_any_database=True).database == "production"
    assert target.resolve("postgresql://u:p@db.lab.example/app_test").database == "app_test"


def test_dsn_comes_from_the_environment_a_file_or_a_prompt_and_never_otherwise(monkeypatch, tmp_path):
    monkeypatch.delenv("CPSIM_AIDB_DSN", raising=False)
    monkeypatch.delenv("CPSIM_AIDB_DSN_FILE", raising=False)
    with pytest.raises(target.TargetRefused, match="CPSIM_AIDB_DSN"):
        target.get_dsn(interactive=False)
    assert target.get_dsn(prompt=lambda _: "  postgresql://asked  ", interactive=True) == "postgresql://asked"
    with pytest.raises(target.TargetRefused):
        target.get_dsn(prompt=lambda _: "  ", interactive=True)
    f = tmp_path / "dsn"
    f.write_text("postgresql://fromfile\n")
    monkeypatch.setenv("CPSIM_AIDB_DSN_FILE", str(f))
    assert target.get_dsn(interactive=False) == "postgresql://fromfile"
    monkeypatch.setenv("CPSIM_AIDB_DSN", "postgresql://fromenv")
    assert target.get_dsn(interactive=False) == "postgresql://fromenv"


def test_scrub_removes_the_dsn_urls_and_the_password():
    text = f"could not connect to {DSN}: password authentication failed for password={SECRET} and postgres://x:y@h/d"
    out = target.scrub(text, DSN)
    assert SECRET not in out and "<hidden>" in out
    assert "postgresql://" not in out and "postgres://" not in out


def test_the_callback_host_must_be_a_private_or_loopback_literal():
    assert target.callback_host_ok("192.168.65.254") and target.callback_host_ok("127.0.0.1") and target.callback_host_ok("10.1.2.3")
    assert not target.callback_host_ok("8.8.8.8") and not target.callback_host_ok("host.docker.internal") and not target.callback_host_ok("")


# ---- the report ----------------------------------------------------------------------------------------------------------------
def _doc():
    lat = stats.summarize([0.01, 0.02, 0.03])
    lat["errors"], lat["error_kinds"] = 0, {}
    q = {"queue_depth_rows": stats.describe([0, 1, 2]), "queue_depth_trend_rows_per_s": 0.1, "samples": 3, "pipeline_status_seen": ["UpToDate"],
         "pipeline_error_rows": 0, "embedded_rows_per_s": 40.0, "drain_after_inserts_stop": {"seconds": 1.0, "timed_out": False, "lag_left": 0}}
    ins = {"target_rows_per_s": 50, "achieved_rows_per_s": 48.0, "rows": 480, "seconds": 10.0, "batches_behind_schedule": 0, "batch_size": 10,
           "writers": 2, "batch_commit_latency": lat}
    one = {"latency": lat, "queries_per_s": 100.0, "first_call_s": 0.1, "inserts": ins, "pipeline": q}
    return {"schema": report.SCHEMA, "started_at": "2026-11-04T10:00:00Z", "finished_at": "2026-11-04T10:05:00Z", "indicative": True,
            "disclaimer": report.DISCLAIMER,
            "hardware": {"client": report.client_hardware(), "server_host_best_effort": {}, "note": "a laptop"},
            "versions": {**report.versions(), "postgres": "17", "aidb": "7.9.0", "pgvector": "0.8.7", "pgfs": None, "settings": {"shared_buffers": "128MB"}},
            "parameters": {"history": 1, "sensors": 50}, "city": {"name": "c", "seed": 1, "sensors": 50, "districts": 3, "types": 9, "preloaded_rows": 50},
            "setup": {"preload": {"rows_per_table": 50}}, "notes": ["n"], "skipped": {"agents": "failed: x"},
            "scenarios": {"pipeline": {"background_sync_interval_s": 2.0, "modes": {"live": {"inserts": ins, "freshness": {"sent": 3, "retrievable": 3, "timed_out": 0, "latency": lat, "resolution_note": "r"}, "pipeline": q}}},
                          "retrieval": {"k": 5, "queries": "q", "backends": {"app_fts": {"idle": one, "loaded": one}, "aidb": {"error": "boom"}}},
                          "agents": {"model": "fake", "levels": {"4": {"turns_per_conversation": 2, "outcomes": {"ok": 8, "error": 0, "timeout": 0}, "calls_ok_per_s": 5.0,
                                                                       "call_latency": lat}}},
                          "governance": {"conversations": 4, "calls_per_pass": 12, "repetitions": 1, "model": "dummy", "telemetry_otel_client": "database", "decisions_recorded": 12,
                                         "open": {"passes": [{"call_latency": lat, "outcomes": {"ok": 12, "error": 0, "timeout": 0}}]},
                                         "governed": {"passes": [{"call_latency": lat, "outcomes": {"ok": 12, "error": 0, "timeout": 0}}]},
                                         "overhead": {"p50_ms": 1.0, "p50_ratio": 1.05, "p95_ms": 2.0, "p95_ratio": 1.1, "note": "x",
                                                      "p50_spread_between_passes_ms": {"open": 0.0, "governed": 0.0}}}}}


def test_report_has_json_and_markdown_with_the_required_sections(tmp_path):
    j, m = report.write(_doc(), str(tmp_path), secrets=(DSN,))
    doc = json.loads(j.read_text())
    assert {"hardware", "versions", "parameters", "scenarios", "disclaimer", "indicative", "city"} <= set(doc) and doc["indicative"] is True
    assert set(doc["scenarios"]) == {"pipeline", "retrieval", "agents", "governance"}
    assert doc["scenarios"]["agents"]["levels"]["4"]["call_latency"]["p99_ms"] is not None
    text = m.read_text()
    for needle in ("INDICATIVE ONLY", "## Environment", "(a) Pipeline", "(b) Retrieval", "(c) Concurrent agent", "(d) Governance overhead", "p99 ms",
                   "Skipped or failed", "a laptop", "error: boom", "## Caveats"):
        assert needle in text, needle
    assert j.name.startswith("aidb-bench-") and m.suffix == ".md"


def test_a_report_holding_the_dsn_or_a_connection_url_is_refused(tmp_path):
    doc = _doc()
    doc["notes"] = [f"connected to {DSN}"]
    with pytest.raises(ValueError):
        report.write(doc, str(tmp_path), secrets=(DSN,))
    doc["notes"] = ["see postgresql://somewhere/db"]
    with pytest.raises(ValueError):
        report.write(doc, str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_client_hardware_records_cpu_and_ram():
    hw = report.client_hardware()
    assert hw["cpus"] and hw["ram_bytes"] and hw["python"]


# ---- the scripted model server ---------------------------------------------------------------------------------------------------
def _post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
        return json.loads(r.read())


def test_fake_model_calls_the_tool_once_then_answers():
    f = fakemodel.FakeModel(host="127.0.0.1", delay=0, tool="recent", tool_args={"district": "d1"})
    try:
        first = _post(f.port, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "Prior actions this request: none yet"}]})
        text = first["choices"][0]["message"]["content"]
        assert json.loads(text) == {"tool_calls": [{"name": "recent", "arguments": {"district": "d1"}}]}
        second = _post(f.port, "/v1/chat/completions", {"model": "m", "messages": [{"role": "user", "content": "- [tool_response:recent] [{\"id\":1}]"}]})
        assert "tool_calls" not in second["choices"][0]["message"]["content"]
        emb = _post(f.port, "/v1/embeddings", {"model": "m", "input": ["a", "b"]})
        assert len(emb["data"]) == 2
        assert f.stats()["requests"] == 3 and f.stats()["tool_calls_issued"] == 1
    finally:
        f.close()


def test_fake_model_serves_requests_in_parallel_so_concurrency_tests_are_not_a_queue():
    f = fakemodel.FakeModel(host="127.0.0.1", delay=0.3)
    try:
        t0 = time.perf_counter()
        ts = [threading.Thread(target=_post, args=(f.port, "/v1/chat/completions", {"model": "m", "messages": []})) for _ in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert time.perf_counter() - t0 < 1.2 and f.stats()["max_in_flight"] >= 3 and f.stats()["requests"] == 6
    finally:
        f.close()


# ---- the CLI ---------------------------------------------------------------------------------------------------------------------
def test_cli_refuses_a_target_that_is_not_allowlisted_and_never_prints_the_dsn(monkeypatch, capsys):
    monkeypatch.setenv("CPSIM_AIDB_DSN", DSN)
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", "other.example")
    for argv in (["aidb-bench", "--check"], ["aidb-bench"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 3
    out = capsys.readouterr()
    assert "not in CPSIM_TARGET_ALLOWLIST" in out.err and SECRET not in out.out + out.err and "postgresql://" not in out.out + out.err


def test_cli_without_a_target_database_exits_2(monkeypatch, capsys):
    monkeypatch.delenv("CPSIM_AIDB_DSN", raising=False)
    monkeypatch.delenv("CPSIM_AIDB_DSN_FILE", raising=False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-bench"])
    assert e.value.code == 2 and "CPSIM_AIDB_DSN" in capsys.readouterr().err


@pytest.mark.parametrize("args", [["--sensors", "10001"], ["--sensors", "3"], ["--scenarios", "pipeline,bogus"], ["--conversations", "0"],
                                  ["--conversations", "a"], ["--duration", "0"], ["--readers", "0"]])
def test_cli_rejects_bad_parameters_before_touching_anything(monkeypatch, capsys, args):
    monkeypatch.setenv("CPSIM_AIDB_DSN", DSN)
    with pytest.raises(SystemExit) as e:
        cli.main(["aidb-bench", *args])
    assert e.value.code == 2 and "invalid" in capsys.readouterr().err


def test_the_benchmark_is_not_loaded_by_the_normal_commands():
    import subprocess
    import sys
    code = "import sys, cpsim.cli, cpsim.api; print('cpsim.aidbbench' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env={**os.environ, "PYTHONPATH": ""})
    assert r.stdout.strip() == "False", r.stderr[-300:]


# ---- against a real AIDB (skipped without AIDB_TEST_URL) ----------------------------------------------------------------------------
AIDB_URL = os.environ.get("AIDB_TEST_URL", "")
aidb_only = pytest.mark.aidb
needs_aidb = pytest.mark.skipif(not AIDB_URL, reason="set AIDB_TEST_URL (a superuser DSN of the aidb-postgres:17 image) to run the AIDB tests")


@aidb_only
@needs_aidb
def test_end_to_end_small_run_writes_a_clean_report_and_removes_what_it_created(monkeypatch, tmp_path):
    import uuid

    import psycopg
    from apgkit.netguard import NetGuard
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from cpsim import outbound
    from cpsim.aidbbench import runner, scenarios

    monkeypatch.setattr(outbound, "guard", NetGuard("CPSIM_ALLOW_PRIVATE_URLS"))         # the real one, not the offline stand-in
    monkeypatch.setenv("CPSIM_ALLOW_PRIVATE_URLS", "true")
    monkeypatch.setenv("CPSIM_TARGET_ALLOWLIST", conninfo_to_dict(AIDB_URL).get("host") or "127.0.0.1")
    name = "cpsim_bench_" + uuid.uuid4().hex[:8] + "_test"
    admin = psycopg.connect(AIDB_URL, autocommit=True)
    admin.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(name)))
    dsn = make_conninfo(AIDB_URL, dbname=name)
    try:
        params = scenarios.Params(sensors=30, duration=3, insert_rate=20, retrieval_seconds=2, readers=1, conversations=(1,), rounds=1, turns=1,
                                  gov_conversations=1, gov_reps=1, model="dummy", pipeline_modes=("live",), probe_every=1, drain_timeout=60)
        assert runner.run(dsn, params, str(tmp_path), "pytest") == 0
        (j,) = tmp_path.glob("aidb-bench-*.json")
        doc = json.loads(j.read_text())
        assert doc["skipped"] == {} and doc["versions"]["aidb"].startswith("7.") and doc["indicative"] is True
        live = doc["scenarios"]["pipeline"]["modes"]["live"]
        assert live["inserts"]["rows"] > 0 and live["freshness"]["retrievable"] >= 1 and live["freshness"]["latency"]["p99_ms"] is not None
        for b in ("app_fts", "app_vector", "app_hybrid", "aidb", "aidb_hybrid"):
            assert doc["scenarios"]["retrieval"]["backends"][b]["idle"]["latency"]["n"] > 0, b
        assert doc["scenarios"]["agents"]["levels"]["1"]["outcomes"]["ok"] >= 1
        assert doc["scenarios"]["governance"]["overhead"]["p50_ms"] is not None
        assert dsn not in j.read_text() and "postgresql://" not in j.read_text()
        with psycopg.connect(dsn) as c:                                                      # cleanup removed everything it created
            assert c.execute("SELECT count(*) FROM pg_tables WHERE tablename LIKE 'cpsim_bench%'").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM aidb.agents WHERE name LIKE 'cpsim_bench%'").fetchone()[0] == 0
            assert c.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'aidb_p_cpsim_bench%'").fetchone()[0] == 0
    finally:
        admin.execute(psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(psycopg.sql.Identifier(name)))
        admin.close()
