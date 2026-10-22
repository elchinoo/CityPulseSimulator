"""The headless commands need no Postgres: `cpsim workloads run|validate`, `cpsim run` and `cpsim hr-run` as real processes, against a
local stand-in server on loopback. Exit codes: 0 normal end (or Ctrl-C / SIGTERM), 2 fatal misconfiguration, 3 a credential the target refused."""
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_workload_kinds import PayBrokerMock

KEY = "sk_test_CLISENTINEL"


class Standin:
    """A loopback HTTP server that answers through an httpx-style handler and records what it received."""

    def __init__(self, handler):
        self.seen, outer = [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.seen.append((self.command, self.path, self.headers.get("authorization", ""), body))
                resp = handler(httpx.Request(self.command, f"http://127.0.0.1{self.path}", headers=dict(self.headers), content=body))
                self.send_response(resp.status_code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(resp.content)))
                self.end_headers()
                self.wfile.write(resp.content)
            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def pb():
    s = Standin(PayBrokerMock(KEY).handler)
    yield s
    s.close()


def env(**kw):
    e = {k: v for k, v in os.environ.items() if not k.startswith(("CPSIM_", "PAYBROKER_", "LLMGW_", "PAGERLENS_", "TRUSTDESK_", "CITYPULSE_"))}
    e.pop("TEST_DATABASE_URL", None)
    e["DATABASE_URL"] = "postgresql://nobody:nopw@127.0.0.1:9/none"      # nothing listens here: any attempt to use Postgres fails loudly
    e["PYTHONUNBUFFERED"] = "1"
    e.update(kw)
    return e


def cpsim(*args, environ=None, timeout=60):
    return subprocess.run([sys.executable, "-m", "cpsim.cli", *args], env=environ or env(), capture_output=True, text=True, timeout=timeout)


def write(tmp_path, text, name="w.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return str(p)


PB_FILE = """
workloads:
  - name: shop
    kind: paybroker
    target_url: ${PAYBROKER_URL}
    token_env: PAYBROKER_SECRET_KEY
    params: {capture_pct: 50}
    schedule: {interval: 1, batch: 3}
"""


def test_a_workload_file_runs_without_postgres_and_prints_status(tmp_path, pb):
    f = write(tmp_path, PB_FILE)
    r = cpsim("workloads", "run", f, "--seconds", "4.5", "--status-every", "1", environ=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY=KEY))
    assert r.returncode == 0, r.stderr
    assert "starting 1 workload(s)" in r.stdout and "shop paybroker every 1s x3" in r.stdout and "ticks=" in r.stdout
    assert "not localhost" not in r.stdout
    posts = [s for s in pb.seen if s[1] == "/v1/payments/authorize"]
    assert len(posts) >= 9 and all(s[2] == f"Bearer {KEY}" for s in pb.seen if not s[1].startswith("/api/3ds"))
    last = [ln for ln in r.stdout.splitlines() if "shop paybroker" in ln][-1]
    assert " stopped:" in last and "items=" in last and "ok=" in last
    assert KEY not in r.stdout + r.stderr, "the key is never printed"
    ticks = int(last.split("ticks=")[1].split()[0])
    assert 4 <= ticks <= 6, last


def test_a_non_local_target_is_announced_in_one_line_and_the_key_stays_out_of_the_output(tmp_path):
    f = write(tmp_path, PB_FILE.replace("${PAYBROKER_URL}", "https://live.paybroker.invalid"))
    r = cpsim("workloads", "run", f, "--seconds", "1.5", "--status-every", "1", environ=env(PAYBROKER_SECRET_KEY=KEY))
    notices = [ln for ln in r.stdout.splitlines() if ln.startswith("notice:")]
    assert notices == ["notice: shop sends simulated traffic to https://live.paybroker.invalid (not localhost)"], r.stdout
    assert r.returncode == 0                   # an unreachable target is not a misconfiguration: it is counted as network_error
    assert "network_error" in r.stdout and KEY not in r.stdout + r.stderr


def test_fatal_misconfiguration_exits_non_zero_with_a_clear_message(tmp_path, pb):
    f = write(tmp_path, PB_FILE)
    r = cpsim("workloads", "run", f, "--seconds", "1", environ=env(PAYBROKER_SECRET_KEY=KEY))             # no PAYBROKER_URL
    assert r.returncode == 2 and "PAYBROKER_URL is not set on this app" in r.stderr and "--dry-run" in r.stderr
    r = cpsim("workloads", "run", f, "--seconds", "1", environ=env(PAYBROKER_URL=pb.url))                    # no key
    assert r.returncode == 2 and "PAYBROKER_SECRET_KEY" in r.stderr
    r = cpsim("workloads", "run", write(tmp_path, "workloads: []", "e.yaml"))
    assert r.returncode == 2 and "workloads" in r.stderr
    r = cpsim("workloads", "run", write(tmp_path, PB_FILE.replace("batch: 3", "batch: 501"), "b.yaml"), environ=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY=KEY))
    assert r.returncode == 2 and "batch" in r.stderr
    r = cpsim("workloads", "run", write(tmp_path, PB_FILE.replace("PAYBROKER_SECRET_KEY", "CPSIM_API_KEY"), "k.yaml"), environ=env(PAYBROKER_URL=pb.url))
    assert r.returncode == 2 and "token_env" in r.stderr
    r = cpsim("workloads", "run", f, "--only", "ghost", environ=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY=KEY))
    assert r.returncode == 2 and "ghost" in r.stderr
    r = cpsim("workloads", "run", str(tmp_path / "missing.yaml"))
    assert r.returncode != 0
    assert pb.seen == [], "nothing was sent for a misconfigured run"


def test_dry_run_generates_and_sends_nothing(tmp_path, pb):
    f = write(tmp_path, PB_FILE)
    r = cpsim("workloads", "run", f, "--dry-run", "--seconds", "2.5", "--status-every", "1", environ=env(PAYBROKER_URL=pb.url))
    assert r.returncode == 0 and "generate-only" in r.stdout and "generated=" in r.stdout
    assert pb.seen == []


def test_a_refused_credential_exits_with_code_3(tmp_path, pb):
    f = write(tmp_path, PB_FILE)
    r = cpsim("workloads", "run", f, "--seconds", "20", "--status-every", "1", environ=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY="sk_test_WRONGKEY"))
    assert r.returncode == 3 and "FATAL" in r.stdout and "refused" in r.stdout
    assert "WRONGKEY" not in r.stdout + r.stderr


def test_only_picks_one_workload_and_a_duration_ends_the_run(tmp_path, pb):
    f = write(tmp_path, PB_FILE.replace("schedule: {interval: 1, batch: 3}", "schedule: {interval: 1, batch: 2, duration: 2}") + """
  - name: other
    kind: paybroker
    target_url: ${PAYBROKER_URL}
    schedule: {interval: 1, batch: 1}
""")
    start = time.time()
    r = cpsim("workloads", "run", f, "--only", "shop", "--status-every", "1", environ=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY=KEY))
    assert r.returncode == 0 and "other" not in r.stdout and "shop paybroker" in r.stdout and "done" in r.stdout
    assert time.time() - start < 12, "the duration ended the run by itself"


def test_sigterm_ends_the_run_cleanly(tmp_path, pb):
    f = write(tmp_path, PB_FILE)
    p = subprocess.Popen([sys.executable, "-m", "cpsim.cli", "workloads", "run", f, "--status-every", "1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, env=env(PAYBROKER_URL=pb.url, PAYBROKER_SECRET_KEY=KEY))
    time.sleep(3)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=15)
    assert p.returncode == 0 and "stopped" in out, err


def test_validate_reports_each_workload(tmp_path):
    f = write(tmp_path, PB_FILE + """
  - name: attack
    kind: pagerlens
    target_url: https://pl.example
    params: {scenario: bad_deploy}
    schedule: {interval: 5, batch: 3}
  - name: feed
    kind: citypulse
    target_url: https://cp.example
    schedule: {interval: 5}
""")
    r = cpsim("workloads", "validate", f, environ=env(PAYBROKER_URL="https://pb.example"))
    assert r.returncode == 0, r.stderr
    assert "ok: 'shop' (paybroker): 3 per tick every 1s = 3 items/s" in r.stdout and "'attack' (pagerlens)" in r.stdout and "'feed' (citypulse)" in r.stdout
    r = cpsim("workloads", "validate", write(tmp_path, "name: x\nkind: nope\n", "bad.yaml"))
    assert r.returncode == 2 and "kind" in r.stderr
    r = cpsim("workloads", "validate", write(tmp_path, "name: hr\nkind: trustdesk\nparams: {company: ghost}\nschedule: {interval: 10}\n", "td.yaml"))
    assert r.returncode == 2 and "ghost" in r.stderr and "companies" in r.stderr


def test_a_single_workload_at_the_top_level_and_json_work(tmp_path, pb):
    f = write(tmp_path, json.dumps({"name": "solo", "kind": "paybroker", "target_url": pb.url, "schedule": {"interval": 1, "batch": 1, "ticks": 2}}), "solo.json")
    r = cpsim("workloads", "run", f, "--status-every", "1", environ=env(PAYBROKER_SECRET_KEY=KEY))
    assert r.returncode == 0 and "solo paybroker" in r.stdout and "done" in r.stdout


def test_a_citypulse_entry_streams_a_live_feed_city_to_citypulse(tmp_path):
    readings = []

    def citypulse(req):
        if req.url.path == "/api/readings":
            readings.extend(json.loads(req.content)["readings"])
        return httpx.Response(200, json={"accepted": 1})
    cp = Standin(citypulse)
    try:
        f = write(tmp_path, f"""
workloads:
  - name: feed
    kind: citypulse
    target_url: {cp.url}
    token_env: CITYPULSE_INGEST_TOKEN
    params: {{sensors: 16, seed: 3}}
    schedule: {{interval: 1}}
""")
        r = cpsim("workloads", "run", f, "--seconds", "5", "--status-every", "2", environ=env(CITYPULSE_INGEST_TOKEN="cp_ingest_x"))
        assert r.returncode == 0, r.stderr
        assert "city 'feed' streaming 16 sensors every 1s" in r.stdout
        assert len(readings) >= 16 and all(s[2] == "Bearer cp_ingest_x" for s in cp.seen)
    finally:
        cp.close()


def test_recurring_trustdesk_requests_generate_when_there_is_no_target(tmp_path):
    f = write(tmp_path, """
companies:
  - name: acme
    seed: 4
    employees: {new: 2, tenured: 2}
    requests: {vacation: 3, sick_leave: 2}
workloads:
  - name: hr
    kind: trustdesk
    params: {company: acme}
    schedule: {interval: 5, ticks: 1}
""")
    r = cpsim("workloads", "run", f, "--dry-run", "--status-every", "1")
    assert r.returncode == 0, r.stderr
    assert "hr trustdesk every 5s" in r.stdout and "generated=5" in r.stdout


def test_a_trustdesk_workload_sends_to_its_companys_target_not_its_own(tmp_path):
    """The workload has no target_url (the company has): a real run must not refuse it as having none."""
    td = Standin(lambda request: httpx.Response(401, json={"detail": "bad token"}))
    try:
        f = write(tmp_path, f"""
companies:
  - name: acme
    target_url: {td.url}
    token_env: TRUSTDESK_CLIENT_TOKEN
    seed: 4
    employees: {{new: 1, tenured: 1}}
    requests: {{vacation: 1}}
workloads:
  - name: hr
    kind: trustdesk
    params: {{company: acme}}
    schedule: {{interval: 5, ticks: 1}}
""")
        r = cpsim("workloads", "run", f, "--seconds", "8", "--status-every", "2", environ=env(TRUSTDESK_CLIENT_TOKEN="td_client_x"))
        assert "has no target_url" not in r.stderr, r.stderr
        assert r.returncode == 3, (r.returncode, r.stderr)
        assert td.seen and td.seen[0][2] == "Bearer td_client_x"
    finally:
        td.close()


def test_the_older_headless_commands_need_no_postgres_either(tmp_path):
    city = write(tmp_path, """
name: tiny
seed: 1
groups:
  - {type: temperature, count: 3, protocol: direct, frequency: 1, burst_pct: 0}
""", "city.yaml")
    r = cpsim("run", city, "--seconds", "1")
    assert r.returncode == 0, r.stderr
    assert "city 'tiny' running: 3 sensors" in r.stdout
    company = write(tmp_path, """
name: acme
seed: 1
employees: {new: 1, tenured: 1}
requests: {vacation: 2}
""", "co.yaml")
    r = cpsim("hr-run", company, "--dry-run")
    assert r.returncode == 0, r.stderr
    assert cpsim("validate", city).returncode == 0 and cpsim("hr-validate", company).returncode == 0


def test_every_shipped_example_validates():
    from pathlib import Path

    files = sorted((Path(__file__).resolve().parents[1] / "examples/live").glob("*.yaml"))
    assert {f.stem for f in files} == {"paybroker", "llmgw", "pagerlens", "trustdesk", "identity", "citypulse"}
    urls = {f"{n}_URL": "https://live.example" for n in ("PAYBROKER", "LLMGW", "PAGERLENS", "TRUSTDESK", "CITYPULSE", "IDENTITY")}
    for f in files:
        r = cpsim("workloads", "validate", str(f), environ=env(**urls))
        assert r.returncode == 0 and r.stdout.startswith("ok:"), (f.name, r.stderr)
        intervals = [int(x.split("every ")[1].split("s")[0]) for x in r.stdout.splitlines() if "every " in x]
        assert intervals and all(i in (2, 5, 60) for i in intervals), (f.name, r.stdout)
