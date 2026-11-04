"""The report: one JSON document (everything measured, raw percentiles included) and a Markdown rendering of it.

No DSN, host name, password or key is ever put in a report (the runner passes only what is safe) and `check_clean` refuses to write one
that contains the DSN or a connection URL."""
import datetime as dt
import json
import os
import platform
import re
from pathlib import Path

SCHEMA = "cpsim-aidb-bench/1"
DISCLAIMER = ("INDICATIVE ONLY. These numbers come from one run on the machine and database described below (for example Docker Desktop on a "
              "laptop: shared CPU, a virtual disk, a client and a server competing for the same cores). They are not a benchmark of AIDB, "
              "of EDB Postgres AI or of any platform, they are not comparable across machines, and they say nothing about quality of "
              "answers. Repeat the run on your own target before relying on any figure.")
_URL = re.compile(r"postgres(?:ql)?://", re.I)


def client_hardware() -> dict:
    out = {"os": f"{platform.system()} {platform.release()}", "machine": platform.machine(), "cpus": os.cpu_count(), "python": platform.python_version()}
    try:
        out["ram_bytes"] = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        out["ram_bytes"] = None
    return out


def versions() -> dict:
    import apgkit
    import psycopg

    from .. import __version__

    return {"cpsim": __version__, "agentpg_kit": apgkit.__version__, "psycopg": psycopg.__version__}


def now_utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_clean(doc: dict, secrets: tuple = ()) -> None:
    text = json.dumps(doc)
    if _URL.search(text) or any(s and s in text for s in secrets):
        raise ValueError("refusing to write a report that contains a connection URL or a secret")


def write(doc: dict, out_dir: str, secrets: tuple = ()) -> tuple[Path, Path]:
    check_clean(doc, secrets)
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    stem = f"aidb-bench-{doc['started_at'].replace(':', '').replace('-', '')}"
    j, m = d / f"{stem}.json", d / f"{stem}.md"
    j.write_text(json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n")
    m.write_text(markdown(doc))
    return j, m


def _ms(v) -> str:
    return "-" if v is None else f"{v:.1f}"


def _lat(s: dict | None) -> list[str]:
    s = s or {}
    return [str(s.get("n", "-")), _ms(s.get("p50_ms")), _ms(s.get("p95_ms")), _ms(s.get("p99_ms")), _ms(s.get("max_ms")), str(s.get("errors", 0))]


def _table(head: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join(["---"] * len(head)) + "|"]
    return out + ["| " + " | ".join(r) + " |" for r in rows] + [""]


def _gb(b) -> str:
    return "unknown" if not b else f"{b / 2**30:.1f} GiB"


def markdown(doc: dict) -> str:
    L: list[str] = [f"# AIDB benchmark report ({doc['started_at']})", "", f"> {doc['disclaimer']}", ""]
    hw, v, p, city = doc["hardware"], doc["versions"], doc["parameters"], doc["city"]
    L += ["## Environment", ""]
    L += [f"- Client: {hw['client']['os']} {hw['client']['machine']}, {hw['client']['cpus']} CPUs, {_gb(hw['client'].get('ram_bytes'))} RAM, Python {hw['client']['python']}"]
    srv = hw.get("server_host_best_effort") or {}
    L += [f"- Database server (best effort, as the server's OS reports it): {srv.get('cpus_seen_by_server_os', 'unknown')} CPUs, "
          f"{_gb(srv.get('ram_bytes_seen_by_server_os'))} RAM" + (f"; cgroup limits cpu={srv.get('cgroup_cpu_max')} memory={srv.get('cgroup_memory_max')}" if srv.get("cgroup_cpu_max") else "")]
    if hw.get("note"):
        L += [f"- Operator note: {hw['note']}"]
    L += [f"- Versions: cpsim {v['cpsim']}, agentpg-kit {v['agentpg_kit']}, psycopg {v['psycopg']}, PostgreSQL {v.get('postgres')}, AIDB {v.get('aidb')}, "
          f"pgvector {v.get('pgvector')}, pgfs {v.get('pgfs')}",
          "- Server settings: " + ", ".join(f"{k}={x}" for k, x in (v.get("settings") or {}).items()),
          f"- City: {city['sensors']} sensors, {city['districts']} districts, {city['types']} sensor types, seed {city['seed']}, "
          f"{p['history']} preloaded observation(s) per sensor ({city['preloaded_rows']} rows per table)",
          f"- Parameters: `{json.dumps(p, sort_keys=True)}`", ""]
    sk = doc.get("skipped") or {}
    if sk:
        L += ["## Skipped or failed", ""] + [f"- **{k}**: {x}" for k, x in sk.items()] + [""]
    for n in doc.get("notes", []):
        L += [f"- Note: {n}"]
    if doc.get("notes"):
        L.append("")
    s = doc.get("setup") or {}
    if s:
        L += ["## Setup timings", ""]
        for k, x in s.items():
            L += [f"- {k}: {json.dumps(x)}"]
        L.append("")
    sc = doc["scenarios"]
    if "pipeline" in sc and "modes" in sc["pipeline"]:
        L += ["## (a) Pipeline throughput and freshness under sustained inserts", "",
              "Live mode: triggers embed inside the inserting transaction (no queue by design; the cost is in insert latency and freshness is the commit). "
              f"Background mode: a worker drains a backlog every {sc['pipeline']['background_sync_interval_s']:g} s (`background_sync_interval`); "
              "queue depth and freshness lag show the backlog.", ""]
        for mode, a in sc["pipeline"]["modes"].items():
            if "error" in a:
                L += [f"### {mode}", "", f"- error: {a['error']}", ""]
                continue
            i, f, q = a["inserts"], a["freshness"], a["pipeline"]
            L += [f"### {mode}", "",
                  f"- Offered {i['target_rows_per_s']} rows/s ({i['writers']} writers, batches of {i['batch_size']}); achieved **{i['achieved_rows_per_s']} rows/s**, "
                  f"{i['rows']} rows in {i['seconds']} s; {i['batches_behind_schedule']} batches ran behind schedule",
                  f"- Pipeline embedded **{q.get('embedded_rows_per_s')} rows/s** over the run (status seen: {', '.join(q['pipeline_status_seen']) or '-'}; "
                  f"error rows: {q['pipeline_error_rows']})",
                  f"- Queue depth (source rows minus rows in the knowledge base, from `aidb.pipeline_metrics`, sampled each second): max {q['queue_depth_rows']['max']}, mean {q['queue_depth_rows']['mean']}, "
                  f"p95 {q['queue_depth_rows']['p95']}; trend {q['queue_depth_trend_rows_per_s']} rows/s per s (positive means falling behind)",
                  f"- Drain after inserts stopped: {q['drain_after_inserts_stop']}",
                  f"- Freshness: {f['retrievable']}/{f['sent']} probe rows became retrievable ({f['timed_out']} timed out). {f['resolution_note']}", ""]
            L += _table(["measure", "n", "p50 ms", "p95 ms", "p99 ms", "max ms", "errors"],
                        [["freshness lag (insert commit to retrievable)"] + _lat(f["latency"]), ["insert batch commit"] + _lat(i["batch_commit_latency"])])
    if "retrieval" in sc and "backends" in sc["retrieval"]:
        r = sc["retrieval"]
        L += ["## (b) Retrieval latency, app vs AIDB backend (apgkit.retrieval)", "",
              f"k={r['k']}, {r['queries']}. `idle`: no inserts; `loaded`: inserts at the offered rate during the pass (AIDB backends: once per pipeline mode). "
              "`app_vector` and `app_hybrid` embed each query with AIDB's local model (`aidb.encode_text_query`), the only offline embedder here; "
              "that call is included in their latency and also listed separately. Rows inserted during a pass are not embedded for the app backends. "
              "Each reader session is warmed up before timing (a new session loads the embedding model on its first call).", ""]
        rows = []
        for name, b in r["backends"].items():
            if "error" in b:
                rows.append([name, "-", "error: " + b["error"], "", "", "", "", "", ""])
                continue
            for mode in [k for k in b if k == "idle" or k.startswith("loaded")]:
                x = b[mode]
                rows.append([name, mode] + _lat(x["latency"])[:5] + [str(x["latency"]["errors"]), str(x["queries_per_s"])])
        L += _table(["backend", "pass", "n", "p50 ms", "p95 ms", "p99 ms", "max ms", "errors", "queries/s"], rows)
        for name, b in r["backends"].items():
            if "error" not in b and "query_embedding_latency" in b["idle"]:
                e = b["idle"]["query_embedding_latency"]
                L += [f"- {name}: query embedding alone (idle pass): p50 {_ms(e['p50_ms'])} ms, p95 {_ms(e['p95_ms'])} ms, n={e['n']}"]
        for name, b in r["backends"].items():
            if "error" in b:
                continue
            for mode in [k for k in b if k.startswith("loaded")]:
                x = b[mode]
                ins = x.get("inserts", {})
                extra = ""
                if "pipeline" in x:
                    q = x["pipeline"]
                    extra = f"; pipeline queue max {q['queue_depth_rows']['max']} rows"
                L += [f"- {name} {mode}: inserts achieved {ins.get('achieved_rows_per_s')} rows/s{extra}"]
        L.append("")
    if "agents" in sc and "levels" in sc["agents"]:
        a = sc["agents"]
        L += ["## (c) Concurrent agent conversations", "", f"Model: **{a['model']}** (see notes). Governed agent (purpose role), tool = SQL read of the latest observations.", ""]
        rows = []
        for n, x in a["levels"].items():
            o = x["outcomes"]
            rows.append([n, str(x["turns_per_conversation"]), str(o["ok"]), str(o["error"]), str(o["timeout"]), str(x["calls_ok_per_s"])] + _lat(x["call_latency"])[1:5])
        L += _table(["parallel", "turns", "ok", "errors", "timeouts", "calls/s", "p50 ms", "p95 ms", "p99 ms", "max ms"], rows)
        for n, x in a["levels"].items():
            if x["call_latency"]["error_kinds"]:
                L += [f"- {n} parallel, error kinds: {json.dumps(x['call_latency']['error_kinds'])}"]
            if "model_server" in x:
                L += [f"- {n} parallel: scripted model server saw {x['model_server']['requests']} requests, max {x['model_server']['max_in_flight']} in flight, "
                      f"{x['model_server']['delay_s']} s added per answer"]
        L.append("")
    if "governance" in sc and "overhead" in sc["governance"]:
        g = sc["governance"]
        o = g["overhead"]
        L += ["## (d) Governance overhead: purpose role vs none", "",
              f"{g['conversations']} parallel conversations, {g['calls_per_pass']} calls per pass, {g['repetitions']} interleaved repetitions, model {g['model']}, "
              f"aidb.otel_client = {g['telemetry_otel_client'] or 'unset'}; decisions recorded during the governed passes: {g['decisions_recorded']}.", ""]
        rows = []
        for label in ("open", "governed"):
            for i, pas in enumerate(g[label]["passes"], 1):
                rows.append([label, str(i)] + _lat(pas["call_latency"])[:5] + [str(pas["outcomes"]["error"] + pas["outcomes"]["timeout"])])
        L += _table(["agent", "pass", "n", "p50 ms", "p95 ms", "p99 ms", "max ms", "failed"], rows)
        L += [f"- Median-of-passes overhead: p50 {_ms(o['p50_ms'])} ms (x{o['p50_ratio']}), p95 {_ms(o['p95_ms'])} ms (x{o['p95_ratio']})",
              f"- Spread of p50 between passes: open {o['p50_spread_between_passes_ms']['open']} ms, governed {o['p50_spread_between_passes_ms']['governed']} ms. {o['note']}", ""]
    L += ["## Caveats", "",
          "- Indicative, single run, single client process; no warm-up beyond the few calls noted; no confidence intervals.",
          "- The scripted model server returns canned text: agent numbers measure AIDB's agent loop, SQL tool and database work plus a fixed model delay.",
          "- Answer quality, recall and ranking quality are NOT measured.", ""]
    return "\n".join(L)
