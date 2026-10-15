import argparse
import asyncio
import json
import os
import sys

from . import auth, model
from .store import open_store


def _serve() -> None:
    import uvicorn

    uvicorn.run("cpsim.api:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*", log_level=os.environ.get("LOG_LEVEL", "info"))


def _load(path: str) -> model.City:
    import yaml

    with open(path) as fh:
        doc = yaml.safe_load(fh) if path.endswith((".yaml", ".yml")) else json.load(fh)
    return model.parse_city(doc)


async def _run(city: model.City, seconds: float | None) -> None:
    from .runtime import Runtime
    from .store import FileStore

    rt = Runtime(FileStore(os.environ.get("CPSIM_DATA_DIR", "data")))
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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="cpsim", description="CityPulse Simulator: cities of simulated sensors speaking industrial protocols.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the web app and API")
    v = sub.add_parser("validate", help="check a city file (YAML or JSON)")
    v.add_argument("file")
    r = sub.add_parser("run", help="run a city file headless, until interrupted")
    r.add_argument("file")
    r.add_argument("--seconds", type=float, default=None)
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
        elif args.cmd == "reset-admin":
            auth.reset_admin(open_store())
            print("Admin password cleared. Restart the app; it prints a new setup code.")
    except model.SpecError as exc:
        sys.exit(f"invalid city: {exc}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
