import argparse
import asyncio
import json
import os
import sys

from . import model


def _serve() -> None:
    import uvicorn

    from . import migrate

    migrate.run()
    uvicorn.run("cpsim.api:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*", log_level=os.environ.get("LOG_LEVEL", "info"))


def _load(path: str) -> model.City:
    import yaml

    with open(path) as fh:
        doc = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
    return model.parse_city(doc)


async def _run(city: model.City, seconds: float | None) -> None:
    from .runtime import Runtime
    from .store import open_store

    rt = Runtime(open_store())
    rt.store.save_city(city.name, city.to_dict())
    runner = await rt.start(city.name)
    print(f"city {city.name!r} running: {len(runner.sensors)} sensors, endpoints {json.dumps(runner.endpoints)}", flush=True)
    try:
        elapsed = 0.0
        while seconds is None or elapsed < seconds:
            await asyncio.sleep(5)
            elapsed += 5
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
    from .tokens import TRUSTDESK_CLIENT, outbound_token

    make = None
    if company.target_url and not dry:
        token = outbound_token(company.token_env, TRUSTDESK_CLIENT)
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
    sub.add_parser("migrate", help="apply the SQL migrations (serve does this at start)")
    imp = sub.add_parser("import-files", help="one-shot upgrade: load cities and companies saved by the first release's file store")
    imp.add_argument("directory", help="the old CPSIM_DATA_DIR (holds state.json)")
    sub.add_parser("reset-admin", help="forget the admin password so setup can run again")
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
            from . import auth
            from .core import db

            with db.connect() as conn:
                auth.reset_admin(conn)
            print("Admin password cleared. Restart the app; it prints a new setup code.")
    except model.SpecError as exc:
        sys.exit(f"invalid city: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
