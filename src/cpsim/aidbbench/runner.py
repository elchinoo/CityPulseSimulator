"""Orchestration of `cpsim aidb-bench`: target checks, setup, the selected scenarios, cleanup, the report."""
import contextlib
import sys

from . import data, report, scenarios, target
from .scenarios import Env, Params

EXIT_OK, EXIT_FAILED, EXIT_MISCONFIGURED, EXIT_REFUSED = 0, 1, 2, 3


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def check(dsn: str, allow_any_database: bool = False) -> int:
    """`--check`: validate the target (allow-list, NetGuard, database name), connect, say whether AIDB is usable. Changes nothing."""
    import psycopg
    from apgkit import aidb
    from psycopg.rows import dict_row

    try:
        t = target.resolve(dsn, allow_any_database)
    except target.TargetRefused as exc:
        log(f"cpsim: target refused: {exc}")
        return EXIT_REFUSED
    try:
        with psycopg.connect(t.conninfo, row_factory=dict_row, connect_timeout=15) as c:
            st = aidb.detect(c)
            print(f"target accepted (allow-listed, pinned address, database {t.database!r}); "
                  f"AIDB {'available, version ' + st.version if st.available else 'NOT available: ' + st.reason}")
    except psycopg.Error as exc:
        log("cpsim: could not connect: " + target.scrub(" ".join(str(exc).split())[:200], dsn))
        return EXIT_FAILED
    return EXIT_OK


def run(dsn: str, params: Params, out_dir: str, hardware_note: str = "", allow_any_database: bool = False) -> int:
    try:
        t = target.resolve(dsn, allow_any_database)
    except target.TargetRefused as exc:
        log(f"cpsim: target refused: {exc}")
        return EXIT_REFUSED
    city = data.build_city(params.sensors, params.seed, params.profile)
    env = Env(t.conninfo, params, city, data.Corpus(city, params.seed))
    started = report.now_utc()
    skipped: dict = {}
    results: dict = {}
    setup_info: dict = {}
    code = EXIT_OK
    try:
        log(f"setting up (city of {len(city.sensors)} sensors)...")
        setup_info = scenarios.setup(env)
    except Exception as exc:                                    # noqa: BLE001
        log("cpsim: setup failed: " + scenarios._err(exc, env))
        with contextlib.suppress(Exception), env.connect() as c:
            scenarios.cleanup(env, c)
        return EXIT_FAILED
    try:
        envinfo = scenarios.environment(env)
        wanted = set(params.scenarios)
        agent_info: dict = {}
        if wanted & {"agents", "governance"}:
            try:
                agent_info = scenarios.setup_agents(env)
            except Exception as exc:                            # noqa: BLE001
                for s in ("agents", "governance"):
                    if s in wanted:
                        skipped[s] = "agent setup failed: " + scenarios._err(exc, env)
                wanted -= {"agents", "governance"}
        for name, fn in (("pipeline", scenarios.run_pipeline), ("retrieval", scenarios.run_retrieval),
                         ("agents", scenarios.run_agents), ("governance", scenarios.run_governance)):
            if name not in wanted:
                continue
            log(f"running {name}...")
            try:
                results[name] = fn(env)
            except Exception as exc:                            # noqa: BLE001
                skipped[name] = "failed: " + scenarios._err(exc, env)
                code = EXIT_FAILED
        notes = list(env.notes)
        if agent_info:
            if agent_info.get("downgraded"):
                notes.append(f"scripted model not used: {agent_info['downgraded']}; the agent scenarios ran with AIDB's built-in dummy model "
                             "(no tool use, no model hop)")
            notes.append(f"agent model: {agent_info.get('effective_mode')}" + (f", {agent_info.get('model_delay_s')} s per answer" if agent_info.get("model_delay_s") is not None else ""))
        if "fake" in env.chat:
            env.chat["fake"].close()
        doc = {"schema": report.SCHEMA, "started_at": started, "finished_at": report.now_utc(), "indicative": True, "disclaimer": report.DISCLAIMER,
               "hardware": {"client": report.client_hardware(), "server_host_best_effort": envinfo.pop("server_host_best_effort", {}),
                            "note": hardware_note or None},
               "versions": {**report.versions(), **envinfo},
               "parameters": params.as_dict(),
               "city": {"name": city.name, "seed": params.seed, "sensors": len(city.sensors), "districts": len(city.districts), "types": len(city.kinds),
                        "preloaded_rows": setup_info.get("preload", {}).get("rows_per_table")},
               "setup": setup_info, "scenarios": results, "skipped": skipped, "notes": notes}
    finally:
        left: list = []
        if not params.keep:
            with contextlib.suppress(Exception), env.connect() as c:
                left = scenarios.cleanup(env, c)
        if left:
            log("cpsim: cleanup left: " + "; ".join(left))
    if left:
        doc["notes"].append("cleanup left objects behind: " + "; ".join(left))
    j, m = report.write(doc, out_dir, secrets=(dsn,))
    log(f"report: {j}\nreport: {m}")
    return code
