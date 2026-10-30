import argparse
import asyncio
import json
import os
import re
import signal
import sys
from urllib.parse import urlsplit

from . import fleet, model


def forwarded_allow_ips() -> str:
    """Peers whose X-Forwarded-* headers are believed (comma-separated addresses or networks). Default: loopback only. Behind a reverse
    proxy set CPSIM_FORWARDED_ALLOW_IPS to that proxy's address (AgentPG puts one proxy in front of an app); never '*'."""
    return os.environ.get("CPSIM_FORWARDED_ALLOW_IPS", "").strip() or "127.0.0.1"


def _serve() -> None:
    import uvicorn

    from . import migrate

    migrate.run()
    uvicorn.run("cpsim.api:app", host=os.environ.get("CPSIM_HOST") or os.environ.get("HOST") or "127.0.0.1", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips=forwarded_allow_ips(), log_level=os.environ.get("LOG_LEVEL", "info"))


def _load(path: str) -> model.City:
    import yaml

    with open(path) as fh:
        doc = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
    return model.parse_city(doc)


async def _run(city: model.City, seconds: float | None) -> None:
    from .runtime import Runtime
    from .store import MemoryStore

    rt = Runtime(MemoryStore())         # headless: no Postgres
    rt.store.save_city(city.name, city.to_dict())
    runner = await rt.start(city.name)
    print(f"city {city.name!r} running: {len(runner.sensors)} sensors, endpoints {json.dumps(runner.endpoints)}", flush=True)
    try:
        elapsed = 0.0
        while seconds is None or elapsed < seconds:
            step = min(5, seconds) if seconds else 5
            await asyncio.sleep(step)
            elapsed += step
            s = runner.snapshot()
            print(f"  sent={s['sent']} buffer={s['buffer']} rate={s['send_rate']}/s bursts={s['bursts']} problem={s['problem'] or '-'}", flush=True)
    finally:
        await rt.stop_all()


def _load_company(path: str):
    import yaml

    from .hr_model import parse_company

    with open(path) as fh:
        doc = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
    return parse_company(doc)


async def _run_company(company, dry: bool) -> int:
    from .hr_engine import HrRun, TrustDeskClient
    from .runtime import Runtime
    from .tokens import TRUSTDESK_CLIENT, outbound_token

    make = None
    if company.target_url and not dry:
        company = Runtime.resolved(company)          # a fleet reference becomes the address of the environment (SpecError: exit 2)
        token = outbound_token(company.token_env, TRUSTDESK_CLIENT, company.target_url)
        if not token:
            sys.exit(f"{company.token_env} is empty: set it to a TrustDesk client token (or configure CPSIM_SERVICE_CLIENT_SECRET)")

        def make():  # noqa: E306
            return TrustDeskClient(company.target_url, token)
    run = HrRun(company, make)
    run.start()
    while run.running:
        await asyncio.sleep(2)
        s = run.snapshot()["totals"]
        print(f"  sent {s['sent']}/{s['planned']}  accepted={s['accepted']} invalid={s['invalid']} errors={s['errors']}", flush=True)
    snap = run.snapshot()
    print(json.dumps({k: snap[k] for k in ("state", "fatal", "employees", "totals", "errors")}, indent=1))
    return 0 if snap["state"] == "done" else 1


# ---- workloads, headless -----------------------------------------------------------------------------------------------
ENV_REF = re.compile(r"\$\{([A-Z][A-Z0-9_]*)(?::-([^}]*))?\}")
EXIT_MISCONFIGURED, EXIT_REFUSED = 2, 3


def expand_env(text: str) -> str:
    """`${NAME}` and `${NAME:-default}` in a target_url; an unset variable becomes the default, or empty. Only URLs are expanded:
    credentials are never read from a file, only from the environment variable a workload names.

    A target that is exactly a fleet reference (`${CITYPULSE_URL}`, see fleet.py) is NOT expanded here: it is kept as written and
    resolved when the workload starts, by the same code as in server mode (an unset one then fails with a clear message)."""
    if fleet.reference_name(text):
        return text
    return ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or m.group(2) or "", text)


def load_workload_file(path: str) -> tuple[list[dict], list[dict]]:
    """(workloads, companies) from a YAML or JSON file: `workloads:` (and optionally `companies:`), or one workload at the top level."""
    import yaml

    with open(path) as fh:
        doc = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
    if isinstance(doc, dict) and "kind" in doc:
        doc = {"workloads": [doc]}
    if not isinstance(doc, dict) or not isinstance(doc.get("workloads"), list) or not doc["workloads"]:
        raise model.SpecError("the file needs a non-empty `workloads:` list (or be one workload with a `kind`)")
    extra = set(doc) - {"workloads", "companies"}
    if extra:
        raise model.SpecError(f"unknown top-level key(s): {', '.join(sorted(extra))}")
    companies = doc.get("companies") or []
    if not isinstance(companies, list):
        raise model.SpecError("companies must be a list")
    for item in (*doc["workloads"], *companies):
        if isinstance(item, dict) and isinstance(item.get("target_url"), str):
            item["target_url"] = expand_env(item["target_url"])
    return doc["workloads"], companies


def _is_local(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")


def status_line(s: dict, elapsed: float) -> str:
    out = " ".join(f"{k}={n}" for k, n in sorted(s["outcomes"].items(), key=lambda kv: -kv[1])[:6])
    ms = lambda v: "-" if v is None else f"{v * 1000:.0f}ms"  # noqa: E731
    line = (f"[{elapsed:5.0f}s] {s['name']} {s['kind']} every {s['interval']:g}s x{s['batch']} {s['state']}: ticks={s['ticks']} "
            f"skipped={s['skipped']} items={s['items']}" + (f" {out}" if out else "") + f" p50={ms(s['latency_p50'])} p95={ms(s['latency_p95'])}")
    if s["next_tick_in"] is not None:
        line += f" next={s['next_tick_in']:g}s"
    if s["backing_off"]:
        line += " BACKING-OFF"
    if s["fatal"]:
        line += f" FATAL: {s['fatal'][:100]}"
    elif s["last_error"]:
        line += f" last_error={s['last_error'][:80]!r}"
    return line


async def _run_workloads(path: str, only: str | None, seconds: float | None, dry_run: bool, every: float) -> int:
    from . import presets, wl_model
    from .hr_model import parse_company
    from .runtime import Runtime
    from .store import MemoryStore
    from .wl_model import parse_workload

    raw_workloads, raw_companies = load_workload_file(path)
    if only:
        raw_workloads = [w for w in raw_workloads if w.get("name") == only]
        if not raw_workloads:
            raise model.SpecError(f"no workload named {only!r} in {path}")
    rt = Runtime(MemoryStore())
    for c in raw_companies:
        if dry_run:
            c["target_url"] = ""
        company = parse_company(c)
        rt.store.save_company(company.name, company.to_dict())
    names, cities = [], []
    for w in raw_workloads:
        if dry_run:
            w["target_url"] = ""
        if w.get("kind") == "citypulse":
            city = presets.city_from_workload(w)
            rt.store.save_city(city.name, city.to_dict())
            cities.append(city)
            continue
        wl = parse_workload(w)
        rt.store.save_workload(wl.name, wl.to_dict())
        names.append(wl.name)
    # a trustdesk workload sends to its company's target_url (checked below), it has no URL of its own
    targets = [(w.name, w.target_url) for w in rt.workloads().values() if w.kind != "trustdesk"] + [(c.name, c.target_url) for c in cities]
    used = {w.params["company"] for w in rt.workloads().values() if w.kind == "trustdesk"}
    targets += [(c.name, c.target_url) for c in rt.companies().values() if c.name in used]
    for name, url in targets:
        try:
            url = fleet.resolve(url, wl_model.allow_http_targets())     # what a start would use; unresolved: say so before anything starts
        except model.SpecError as exc:
            print(f"cpsim: {name}: {exc}; or use --dry-run to generate without sending", file=sys.stderr)
            return EXIT_MISCONFIGURED
        if not url and not dry_run:
            print(f"cpsim: {name} has no target_url (is its URL variable set?); use --dry-run to generate without sending", file=sys.stderr)
            return EXIT_MISCONFIGURED
        if url and not _is_local(url):
            print(f"notice: {name} sends simulated traffic to {url} (not localhost)", flush=True)
        elif not url:
            print(f"notice: {name} is generate-only: nothing is sent", flush=True)
    total_rate = sum(w.schedule.rate() for w in rt.workloads().values() if w.kind != "trustdesk")
    print(f"starting {len(names) + len(cities)} workload(s), about {total_rate:g} items/s in all", flush=True)
    started = []
    try:
        for name in names:
            rt.start_workload(name)
            started.append(name)
        for city in cities:
            runner = await rt.start(city.name)
            print(f"city {city.name!r} streaming {len(runner.sensors)} sensors every {city.groups[0].frequency:g}s", flush=True)
    except (model.SpecError, KeyError, RuntimeError) as exc:
        print(f"cpsim: {exc}", file=sys.stderr)
        await rt.stop_all()
        return EXIT_MISCONFIGURED
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:      # e.g. Windows
            pass
    t0 = loop.time()
    code = 0
    try:
        while not stop.is_set() and (seconds is None or loop.time() - t0 < seconds):
            try:
                await asyncio.wait_for(stop.wait(), min(every, seconds - (loop.time() - t0)) if seconds else every)
            except TimeoutError:
                pass
            for s in rt.workload_snapshots():
                print(status_line(s, loop.time() - t0), flush=True)
            for city_snap in rt.snapshots():
                print(f"  city {city_snap['name']}: sent={city_snap['sent']} buffer={city_snap['buffer']} problem={city_snap['problem'] or '-'}", flush=True)
            snaps = rt.workload_snapshots()
            if snaps and not cities and not any(s["state"] == "running" for s in snaps):
                break                    # every workload finished (duration, total or a refused credential)
    finally:
        await rt.stop_all()
        final = rt.workload_snapshots()
        for s in final:
            print(status_line(s, loop.time() - t0), flush=True)
        if any(s["state"] == "failed" for s in final):
            code = EXIT_REFUSED
    return code


def _generate_cities(args) -> int:
    """`cpsim cities generate`: varied demo cities as YAML/JSON files in the existing city format (no Postgres, no network)."""
    from pathlib import Path

    from . import citygen

    out = Path(args.out) if args.out else None
    taken = set()
    if out is not None and out.is_dir():
        taken = {f.stem for f in out.iterdir() if f.suffix in (".yaml", ".yml", ".json")}
    size = int(args.size) if str(args.size).isdigit() else args.size
    cities = citygen.generate_cities(args.count, size, args.profile, args.seed, args.name, taken,
                                     target_url=args.target_url, token_env=args.token_env, autostart=args.autostart, real=True)
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        for g in cities:
            (out / f"{g.name}.{args.format}").write_text(citygen.render(g, args.format))
    print(f"{'city':<26}{'seed':>11}{'sensors':>9}{'districts':>10}{'groups':>8}  protocols")
    for g in cities:
        r = g.report()
        print(f"{r['name']:<26}{r['seed']:>11}{r['sensors']:>9}{len(r['districts']):>10}{r['groups']:>8}  {', '.join(r['protocols'])}")
        if args.verbose:
            for d in r["districts"]:
                print(f"    {d['id']:<24} {d['kind']:<12} {d['sensors']:>6} sensors  {', '.join(d['types'])}")
    print(f"{len(cities)} cities" + (f" written to {out}" if out else " (use --out DIR to write the files)"))
    return 0


CONFIRM_DELETE = "delete credentials"


def reset_admin(conn, assume_yes: bool = False, ask=input) -> bool:
    """Forget the admin password. The kit resets the vault with it (the data key and every wrapping of it), so the credentials saved
    under Settings > Credentials would stay in the database as ciphertext nobody can read. Name them, ask for confirmation (typing
    `delete credentials`; `--yes` skips it), delete them with the reset. False (nothing changed) when the answer is not the phrase."""
    from . import auth, credentials

    names = sorted(credentials.saved_names(conn))
    if names:
        print(f"Resetting the admin password also resets the vault, so the {len(names)} saved credential(s) become unreadable "
              f"and will be DELETED: {', '.join(names)}.\nSave them again afterwards (Settings > Credentials), or set the environment "
              "variables they stand for. To keep them, cancel and change the password under Settings > Admin password instead.")
        if not assume_yes:
            try:
                answer = ask(f'Type "{CONFIRM_DELETE}" to continue: ').strip()
            except EOFError:
                answer = ""
            if answer != CONFIRM_DELETE:
                print("Cancelled: nothing was changed.")
                return False
        conn.execute("DELETE FROM cpsim_credential")
    auth.reset_admin(conn)
    conn.commit()
    print("Admin password cleared" + (f" and {len(names)} saved credential(s) deleted" if names else "")
          + ". Restart the app; it prints a new setup code.")
    return True


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cpsim", description="CityPulse Simulator: cities of simulated sensors speaking industrial protocols.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web app and API")
    v = sub.add_parser("validate", help="check a city file (YAML or JSON)")
    v.add_argument("file")
    r = sub.add_parser("run", help="run a city file headless, until interrupted")
    r.add_argument("file")
    r.add_argument("--seconds", type=float, default=None)
    hv = sub.add_parser("hr-validate", help="check a company file (employees and request counts for TrustDesk)")
    hv.add_argument("file")
    hr = sub.add_parser("hr-run", help="send a company's employees and requests to TrustDesk, then exit")
    hr.add_argument("file")
    hr.add_argument("--dry-run", action="store_true", help="generate everything, send nothing")
    w = sub.add_parser("workloads", help="scheduled workloads for every service of the ecosystem, headless (no Postgres needed)")
    wsub = w.add_subparsers(dest="wcmd", required=True)
    wv = wsub.add_parser("validate", help="check a workloads file (YAML or JSON)")
    wv.add_argument("file")
    wr = wsub.add_parser("run", help="run the workloads of a file until Ctrl-C (or --seconds), printing a status line every few seconds")
    wr.add_argument("file")
    wr.add_argument("--only", default=None, help="run only the workload with this name")
    wr.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    wr.add_argument("--dry-run", action="store_true", help="generate everything, send nothing (also what an empty target_url means)")
    wr.add_argument("--status-every", type=float, default=5.0, help="seconds between status lines (default 5)")
    cs = sub.add_parser("cities", help="demo cities, headless (no Postgres needed)")
    csub = cs.add_subparsers(dest="ccmd", required=True)
    cg = csub.add_parser("generate", help="generate varied demo cities: same seed, same city; another seed, another city")
    cg.add_argument("--count", type=int, default=1, help="how many cities, 1 to 50 (default 1)")
    cg.add_argument("--size", default="medium", help="small (~200 sensors), medium (~2000), large (~20000, clamped to 10000) or a number")
    cg.add_argument("--profile", default="mixed", help="balanced, industrial, coastal, tourist, residential or mixed (default)")
    cg.add_argument("--seed", type=int, default=None, help="city i uses seed+i; random when omitted")
    cg.add_argument("--name", default=None, help="the name (or the stem name-1, name-2... for several); made up when omitted")
    cg.add_argument("--out", default=None, help="write one file per city into this directory (created if needed)")
    cg.add_argument("--format", choices=("yaml", "json"), default="yaml")
    cg.add_argument("--target-url", default=None, help="the CityPulse URL, or ${CITYPULSE_URL} to follow the fleet, to put in each city "
                    "(default: ${CITYPULSE_URL} when that variable is set, else empty: generate only)")
    cg.add_argument("--token-env", default="CITYPULSE_INGEST_TOKEN", help="the NAME of the variable holding the ingest token")
    cg.add_argument("--autostart", action="store_true", help="mark the cities to start when the app starts")
    cg.add_argument("-v", "--verbose", action="store_true", help="list each city's districts and sensor types")
    sub.add_parser("migrate", help="apply the SQL migrations (serve does this at start)")
    imp = sub.add_parser("import-files", help="one-shot upgrade: load cities and companies saved by the first release's file store")
    imp.add_argument("directory", help="the old CPSIM_DATA_DIR (holds state.json)")
    ra = sub.add_parser("reset-admin", help="forget the admin password so setup can run again (also deletes saved credentials: they cannot "
                        "be read after the vault is reset)")
    ra.add_argument("--yes", action="store_true", help="do not ask before deleting the saved credentials")
    args = p.parse_args(argv)
    try:
        if args.cmd == "serve":
            _serve()
        elif args.cmd == "validate":
            c = _load(args.file)
            print(f"ok: city {c.name!r}, {c.sensor_count()} sensors in {len(c.groups)} groups")
        elif args.cmd == "run":
            asyncio.run(_run(_load(args.file), args.seconds))
        elif args.cmd == "hr-validate":
            c = _load_company(args.file)
            print(f"ok: company {c.name!r}, {c.new_employees + c.tenured_employees} employees, {c.total_requests()} requests "
                  f"in {len(c.requests)} types, {c.problem_pct:g}% problem requests")
        elif args.cmd == "hr-run":
            sys.exit(asyncio.run(_run_company(_load_company(args.file), args.dry_run)))
        elif args.cmd == "workloads" and args.wcmd == "validate":
            from .wl_model import parse_workload

            raw, companies = load_workload_file(args.file)
            known = {c.get("name") for c in companies}
            for item in raw:
                if item.get("kind") == "citypulse":
                    from .presets import city_from_workload

                    c = city_from_workload(item)
                    print(f"ok: {c.name!r} (citypulse): a city of {c.sensor_count()} sensors, a reading every {c.groups[0].frequency:g}s")
                    continue
                wl = parse_workload(item)
                if wl.kind == "trustdesk" and wl.params["company"] not in known:
                    raise model.SpecError(f"workload {wl.name!r} runs company {wl.params['company']!r}, which is not in the file's `companies:` list")
                print(f"ok: {wl.name!r} ({wl.kind}): " + (f"a run every {wl.schedule.interval:g}s" if wl.kind == "trustdesk" else
                      f"{wl.schedule.batch} per tick every {wl.schedule.interval:g}s = {wl.schedule.rate():g} items/s"
                      + ("" if wl.target_url else " (no target_url: generate only)")))
            for c in companies:
                from .hr_model import parse_company

                co = parse_company(c)
                print(f"ok: company {co.name!r}, {co.total_requests()} requests per run")
        elif args.cmd == "workloads":
            sys.exit(asyncio.run(_run_workloads(args.file, args.only, args.seconds, args.dry_run, max(args.status_every, 0.5))))
        elif args.cmd == "cities":
            sys.exit(_generate_cities(args))
        elif args.cmd == "migrate":
            from . import migrate

            applied = migrate.run()
            print("applied: " + (", ".join(applied) if applied else "nothing to do"))
        elif args.cmd == "import-files":
            from .store import import_files

            done = import_files(args.directory)
            print(f"imported {done['cities']} cities and {done['companies']} companies; {done['skipped']} skipped (invalid); "
                  f"admin password {'imported' if done['admin_password'] else 'left as it is'}")
        elif args.cmd == "reset-admin":
            from .core import db

            with db.connect() as conn:
                if not reset_admin(conn, assume_yes=args.yes):
                    sys.exit(EXIT_MISCONFIGURED)
    except model.SpecError as exc:
        print(f"invalid: {exc}", file=sys.stderr)
        sys.exit(EXIT_MISCONFIGURED)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
