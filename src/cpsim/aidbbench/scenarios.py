"""The four scenarios and their setup/cleanup. Everything the benchmark creates is named `cpsim_bench*` and removed afterwards.

Measured from one client process (threads, one connection each), against whatever the target is. Nothing here asserts an AIDB result is
good: it times, counts and reports; a failure is recorded as an error, never hidden.
"""
import contextlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field

import psycopg
from apgkit import aidb, retrieval
from psycopg.rows import dict_row

from . import data, stats
from . import target as target_mod

PREFIX = "cpsim_bench"
TABLE_AIDB, TABLE_APP = f"{PREFIX}_aidb", f"{PREFIX}_app"
PIPELINE = PREFIX
PURPOSE, TOOL = f"{PREFIX}_obs", f"{PREFIX}_recent"
AGENT_GOV, AGENT_OPEN = f"{PREFIX}_agent_gov", f"{PREFIX}_agent_open"
MODEL_CHAT, CREDENTIAL = f"{PREFIX}_chat", f"{PREFIX}_cred"
FAKE_KEY_ENV = "CPSIM_AIDB_FAKE_KEY"
EMBED_MODEL = aidb.LOCAL_EMBEDDING_MODEL
SCENARIOS = ("pipeline", "retrieval", "agents", "governance")
BACKENDS = ("app_fts", "app_vector", "app_hybrid", "aidb", "aidb_hybrid")


@dataclass
class Params:
    scenarios: tuple = SCENARIOS
    sensors: int = 200
    seed: int = 1
    profile: str = "mixed"
    history: int = 1                 # preloaded observations per sensor
    duration: float = 20.0           # seconds of sustained inserts in the pipeline scenario
    insert_rate: float = 50.0        # rows/s offered (0: as fast as the writers can)
    writers: int = 2
    batch: int = 10
    pipeline_modes: tuple = ("live", "background")
    sync_interval: float = 2.0       # background_sync_interval of the pipeline in background mode, seconds
    probe_every: float = 2.0
    probe_timeout: float = 120.0
    drain_timeout: float = 300.0
    readers: int = 2
    retrieval_seconds: float = 10.0
    k: int = 5
    conversations: tuple = (1, 4, 8)
    rounds: int = 3                  # conversations per thread
    turns: int = 2
    gov_conversations: int = 4
    gov_reps: int = 3
    model: str = "fake"              # fake | dummy | endpoint
    model_delay: float = 0.2
    call_timeout: float = 60.0
    callback_host: str = ""
    enable_telemetry: bool = False
    keep: bool = False

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["scenarios"], d["conversations"], d["pipeline_modes"] = list(self.scenarios), list(self.conversations), list(self.pipeline_modes)
        d.pop("callback_host", None)
        return d


@dataclass
class Env:
    conninfo: str
    params: Params
    city: data.City
    corpus: data.Corpus
    notes: list = field(default_factory=list)
    status: object = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    telemetry_enabled_by_us: bool = False
    chat: dict = field(default_factory=dict)
    base_ids: dict = field(default_factory=dict)           # table -> highest id of the preloaded corpus

    def connect(self) -> psycopg.Connection:
        return psycopg.connect(self.conninfo, row_factory=dict_row, application_name="cpsim-aidb-bench", connect_timeout=15)

    def rows(self, n: int):
        with self.lock:
            return self.corpus.rows(n)


def _err(exc: Exception, env: Env | None = None) -> str:
    text = " ".join(str(exc).split())[:200] or exc.__class__.__name__
    return target_mod.scrub(text, env.conninfo if env else "")


# ---------------------------------------------------------------------------------------------------------------------------------
# setup and cleanup
# ---------------------------------------------------------------------------------------------------------------------------------
def cleanup(env: Env, conn: psycopg.Connection) -> list[str]:
    """Remove every cpsim_bench object. Returns what could not be removed (empty when clean)."""
    left: list[str] = []

    def step(label, fn):
        try:
            fn()
            conn.commit()
        except Exception as exc:                                   # noqa: BLE001 - best effort, reported
            conn.rollback()
            if "does not exist" not in str(exc) and "not found" not in str(exc).lower():
                left.append(f"{label}: {_err(exc, env)}")

    st = aidb.detect(conn)
    if st.available:
        for a in (AGENT_GOV, AGENT_OPEN):
            if conn.execute("SELECT 1 FROM aidb.agents WHERE name = %s", (a,)).fetchone():
                # an agent that has conversations is refused without force (reported as text, not raised); the transcripts stay in aidb.conversations
                step(f"agent {a}", lambda a=a: conn.execute("SELECT aidb.delete_agent(%s, force => true)", (a,)))

        if conn.execute("SELECT 1 FROM aidb.sql_tool_registry WHERE name = %s", (TOOL,)).fetchone():
            step("tool", lambda: conn.execute("SELECT aidb.delete_tool(%s)", (TOOL,)))
        if conn.execute("SELECT 1 FROM aidb.purpose_registry WHERE name = %s AND deleted_at IS NULL", (PURPOSE,)).fetchone():
            step("purpose", lambda: aidb.retire_purpose(conn, PURPOSE))
        if aidb.pipeline_status(conn, PIPELINE):
            step("pipeline", lambda: aidb.drop_pipeline(conn, PIPELINE))
        if conn.execute("SELECT 1 FROM aidb.models WHERE name = %s", (MODEL_CHAT,)).fetchone():
            step("model", lambda: conn.execute("SELECT aidb.delete_model(%s)", (MODEL_CHAT,)))
        if conn.execute("SELECT 1 FROM aidb.credentials WHERE name = %s", (CREDENTIAL,)).fetchone():
            step("credential", lambda: conn.execute("SELECT aidb.delete_credential(%s)", (CREDENTIAL,)))
    for t in (TABLE_AIDB, TABLE_APP):
        step(f"table {t}", lambda t=t: conn.execute(psycopg.sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(psycopg.sql.Identifier(t))))
    role = aidb.purpose_role_name(PURPOSE)
    if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
        step("role", lambda: (conn.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(psycopg.sql.Identifier(role))),
                              conn.execute(psycopg.sql.SQL("DROP ROLE {}").format(psycopg.sql.Identifier(role)))))
    checks = [("table", "SELECT tablename AS n FROM pg_tables WHERE tablename LIKE 'cpsim\\_bench%%'"),
              ("role", "SELECT rolname AS n FROM pg_roles WHERE rolname LIKE 'aidb\\_p\\_cpsim\\_bench%%'")]
    if st.available:
        checks += [("agent", "SELECT name AS n FROM aidb.agents WHERE name LIKE 'cpsim\\_bench%%'"),
                   ("tool", "SELECT name AS n FROM aidb.sql_tool_registry WHERE name LIKE 'cpsim\\_bench%%'"),
                   ("model", "SELECT name AS n FROM aidb.models WHERE name LIKE 'cpsim\\_bench%%'"),
                   ("credential", "SELECT name AS n FROM aidb.credentials WHERE name LIKE 'cpsim\\_bench%%'"),
                   ("pipeline", "SELECT name AS n FROM aidb.pipelines WHERE name LIKE 'cpsim\\_bench%%'")]
    for kind, q in checks:
        for r in conn.execute(q).fetchall():
            if f"{kind} {r['n']}: still present after delete_agent" not in left:
                left.append(f"{kind} {r['n']} still present")
    conn.commit()
    if env.telemetry_enabled_by_us:
        step("telemetry setting", lambda: conn.execute(psycopg.sql.SQL("ALTER DATABASE {} RESET aidb.otel_client").format(
            psycopg.sql.Identifier(conn.execute("SELECT current_database() AS d").fetchone()["d"]))))
    return left


def setup(env: Env) -> dict:
    """Extension, tables, indexes, preloaded corpus, pipeline backfill. Returns timings for the report."""
    p = env.params
    out: dict = {}
    with env.connect() as conn:
        try:
            conn.execute("CREATE EXTENSION IF NOT EXISTS aidb CASCADE")
            conn.commit()
        except psycopg.Error as exc:
            conn.rollback()
            raise RuntimeError(f"AIDB is not available on the target: {_err(exc, env)}") from exc
        env.status = aidb.require(conn, min_version="7.7")
        leftovers = cleanup(env, conn)
        if leftovers:
            raise RuntimeError("could not clear an earlier run: " + "; ".join(leftovers))
        if p.enable_telemetry:
            aidb.enable_telemetry(conn)
            env.telemetry_enabled_by_us = True
        for t in (TABLE_AIDB, TABLE_APP):
            conn.execute(f"CREATE TABLE {t} (id bigserial PRIMARY KEY, sensor_id text NOT NULL, district text NOT NULL, kind text NOT NULL, "
                         "body text NOT NULL, embedding vector(384), created_at timestamptz NOT NULL DEFAULT now())")
        conn.execute(f"CREATE INDEX ON {TABLE_APP} USING gin (to_tsvector('english', body))")
        conn.execute(f"CREATE INDEX ON {TABLE_APP} USING hnsw (embedding vector_cosine_ops)")
        conn.commit()
        n = len(env.city.sensors) * max(1, p.history)
        rows = env.rows(n)
        t0 = time.perf_counter()
        for t in (TABLE_APP, TABLE_AIDB):
            with conn.cursor() as cur:
                cur.executemany(f"INSERT INTO {t} (sensor_id, district, kind, body) VALUES (%s, %s, %s, %s)",
                                [(r.sensor_id, r.district, r.kind, r.body) for r in rows])
            conn.commit()
        for t in (TABLE_AIDB, TABLE_APP):
            env.base_ids[t] = conn.execute(f"SELECT max(id) AS m FROM {t}").fetchone()["m"]
        out["preload"] = {"rows_per_table": n, "insert_seconds": round(time.perf_counter() - t0, 3)}
        if {"retrieval"} & set(p.scenarios):
            t0 = time.perf_counter()
            ids = [r["id"] for r in conn.execute(f"SELECT id FROM {TABLE_APP} ORDER BY id").fetchall()]
            for i in range(0, len(ids), 500):
                lo, hi = ids[i], ids[min(i + 499, len(ids) - 1)]
                conn.execute(f"UPDATE {TABLE_APP} SET embedding = aidb.encode_text(%s, body)::vector WHERE id BETWEEN %s AND %s", (EMBED_MODEL, lo, hi))
                conn.commit()
            secs = time.perf_counter() - t0
            out["app_embedding_backfill"] = {"rows": n, "seconds": round(secs, 3), "rows_per_s": round(n / secs, 2) if secs else None}
        if {"pipeline", "retrieval"} & set(p.scenarios):
            t0 = time.perf_counter()
            info = aidb.ensure_pipeline(conn, PIPELINE, source=TABLE_AIDB, key_column="id", text_column="body", model=EMBED_MODEL)
            secs = time.perf_counter() - t0
            out["aidb_pipeline_backfill"] = {"rows": n, "seconds": round(secs, 3), "rows_per_s": round(n / secs, 2) if secs else None, "kb": info.kb}
    return out


KB = f"public.pipeline_{PIPELINE}"


def set_mode(env: Env, mode: str) -> None:
    """Switch the pipeline between Live (triggers embed inside the writing transaction) and Background (a worker drains a backlog every
    `sync_interval`). The backlog is drained BEFORE the switch (AIDB does not process an existing backlog when switching to Live, measured
    on 7.9.0), and for Background the call waits until the pipeline's worker is running (measured: it starts up to about 30 s after
    the switch, and rows inserted before that sit in the queue). Part of the benchmark's own setup, not of any measured window."""
    p = env.params
    with env.connect() as conn:
        current = conn.execute("SELECT auto_processing::text AS m FROM aidb.pipelines WHERE name = %s", (PIPELINE,)).fetchone()["m"]
        conn.commit()
        if mode == "background":
            preload = conn.execute("SHOW shared_preload_libraries").fetchone()["shared_preload_libraries"]
            if "aidb" not in preload:
                raise RuntimeError("Background mode needs shared_preload_libraries to include aidb on the server (it does not here); note that "
                                   "starting the aidb-postgres image with extra `-c` flags and no `postgres` command drops the image's setting")
    if current.lower() != mode:
        d = wait_drained(env, p.drain_timeout)
        if d["timed_out"]:
            raise RuntimeError(f"pipeline did not drain before switching to {mode} ({d['lag_left']} rows waiting)")
    with env.connect() as conn:
        if mode == "live":
            conn.execute("SELECT aidb.update_pipeline(name => %s, auto_processing => 'Live')", (PIPELINE,))
        else:
            conn.execute("SELECT aidb.update_pipeline(name => %s, auto_processing => 'Background', background_sync_interval => %s::interval)",
                         (PIPELINE, f"{p.sync_interval:g} seconds"))
        conn.commit()
        if mode == "background" and current.lower() != mode:
            end = time.perf_counter() + 120
            while time.perf_counter() < end:
                n = conn.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE backend_type LIKE %s", (f"aidb bgworker {PIPELINE}(%",)).fetchone()["n"]
                conn.commit()
                if n:
                    break
                time.sleep(1)
            else:
                env.notes.append("Background mode: the pipeline's worker was not seen within 120 s of switching (or this role cannot see it); "
                                 "results for that mode may include worker start-up")
    d = wait_drained(env, p.drain_timeout)
    if d["timed_out"]:
        raise RuntimeError(f"pipeline did not drain after switching to {mode}")


def reset_to_base(env: Env) -> None:
    """Both tables back to the preloaded corpus (rows added by earlier passes deleted, statistics refreshed), so every backend and mode is
    measured against the same amount of data. The AIDB table is cleaned in Live mode, where deletes reach the knowledge base inside the
    transaction (checked on 7.9.0: the knowledge base shrinks with the source)."""
    set_mode(env, "live")
    with env.connect() as conn:
        for t in (TABLE_APP, TABLE_AIDB):
            conn.execute(f"DELETE FROM {t} WHERE id > %s", (env.base_ids[t],))
            conn.commit()
        conn.autocommit = True
        for t in (TABLE_APP, TABLE_AIDB):
            conn.execute(f"VACUUM (ANALYZE) {t}")
    d = wait_drained(env, env.params.drain_timeout)
    if d["timed_out"]:
        raise RuntimeError("pipeline did not settle after resetting the tables")


# ---------------------------------------------------------------------------------------------------------------------------------
# load generators
# ---------------------------------------------------------------------------------------------------------------------------------
class Writer:
    """`writers` threads inserting batches of observations into `table` at `rate` rows/s in all (0: unthrottled) until `stop` is set."""

    def __init__(self, env: Env, table: str, rate: float):
        self.env, self.table, self.rate = env, table, rate
        self.rec = stats.Recorder()
        self.rows = 0
        self.behind = 0
        self._lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.t0 = self.t1 = 0.0

    def _run(self, stop: threading.Event) -> None:
        p = self.env.params
        every = p.batch * p.writers / self.rate if self.rate > 0 else 0.0
        sql = f"INSERT INTO {self.table} (sensor_id, district, kind, body) VALUES (%s, %s, %s, %s)"
        try:
            conn = self.env.connect()
        except psycopg.Error as exc:
            self.rec.fail("connect: " + _err(exc, self.env))
            return
        nxt = time.perf_counter()
        first = True
        with conn:
            while not stop.is_set():
                batch = self.env.rows(p.batch)
                t = time.perf_counter()
                try:
                    with conn.cursor() as cur:
                        cur.executemany(sql, [(r.sensor_id, r.district, r.kind, r.body) for r in batch])
                    conn.commit()
                    if not first:                       # the first batch of a new session loads the embedding model (Live mode): not a steady-state sample
                        self.rec.ok(time.perf_counter() - t)
                    first = False
                    with self._lock:
                        self.rows += len(batch)
                except psycopg.Error as exc:
                    conn.rollback()
                    self.rec.fail(_err(exc, self.env))
                if every:
                    nxt += every
                    wait = nxt - time.perf_counter()
                    if wait > 0:
                        stop.wait(wait)
                    else:
                        with self._lock:
                            self.behind += 1
                        nxt = time.perf_counter()

    def start(self, stop: threading.Event) -> None:
        self.t0 = time.perf_counter()
        self.threads = [threading.Thread(target=self._run, args=(stop,), daemon=True) for _ in range(self.env.params.writers)]
        for t in self.threads:
            t.start()

    def finish(self) -> dict:
        for t in self.threads:
            t.join(timeout=60)
        self.t1 = time.perf_counter()
        secs = max(self.t1 - self.t0, 1e-9)
        return {"target_rows_per_s": self.rate or None, "achieved_rows_per_s": round(self.rows / secs, 2), "rows": self.rows,
                "seconds": round(secs, 2), "batches_behind_schedule": self.behind, "batch_size": self.env.params.batch,
                "writers": self.env.params.writers, "batch_commit_latency": self.rec.summary(),
                "note": "each writer's first batch is inserted but not timed (new session)"}


class Sampler:
    """Once a second, from `aidb.pipeline_metrics` (the kit's pipeline_status): rows waiting = source rows minus rows in the knowledge base (the
    queue depth), embedded and source counts, status. AIDB's own "unprocessed rows" metric is recorded next to it: it was measured to stay
    stale after a switch from Background to Live, so it is not used for the queue depth."""

    def __init__(self, env: Env, every: float = 1.0):
        self.env, self.every = env, every
        self.points: list[tuple[float, int, int, int]] = []       # (t, backlog, embedded, source)
        self.metric: list[float] = []                              # AIDB's own "unprocessed rows" metric, kept for comparison
        self.status: set[str] = set()
        self.errors = stats.Recorder()
        self.record_errors = 0
        self._t: threading.Thread | None = None

    def _run(self, stop: threading.Event) -> None:
        try:
            conn = self.env.connect()
        except psycopg.Error as exc:
            self.errors.fail("connect: " + _err(exc, self.env))
            return
        with conn:
            while not stop.is_set():
                try:
                    s = aidb.pipeline_status(conn, PIPELINE)[0]
                    conn.commit()
                    emb, src = int(s["embedded_rows"] or 0), int(s["source_rows"] or 0)
                    self.points.append((time.perf_counter(), max(0, src - emb), emb, src))
                    self.metric.append(float(s["lag"]))
                    self.status.add(str(s["status"]))
                    self.record_errors = int(s["record_errors"]) + int(s["blocking_errors"])
                except Exception as exc:                       # noqa: BLE001
                    conn.rollback()
                    self.errors.fail(_err(exc, self.env))
                stop.wait(self.every)

    def start(self, stop: threading.Event) -> None:
        self._t = threading.Thread(target=self._run, args=(stop,), daemon=True)
        self._t.start()

    def join(self) -> None:
        if self._t:
            self._t.join(timeout=30)

    def summary(self) -> dict:
        lags = [float(p[1]) for p in self.points]
        t0 = self.points[0][0] if self.points else 0.0
        trend = stats.slope([(p[0] - t0, float(p[1])) for p in self.points])
        out = {"queue_depth_rows": stats.describe(lags), "aidb_unprocessed_rows_metric": stats.describe(self.metric), "queue_depth_trend_rows_per_s": None if trend is None else round(trend, 3),
               "samples": len(self.points), "pipeline_status_seen": sorted(self.status), "pipeline_error_rows": self.record_errors,
               "sampler_errors": self.errors.summary()["errors"]}
        if len(self.points) >= 2:
            dt = self.points[-1][0] - self.points[0][0]
            de = self.points[-1][2] - self.points[0][2]
            out["embedded_rows_per_s"] = round(de / dt, 2) if dt > 0 else None
        return out


def wait_drained(env: Env, timeout: float) -> dict:
    """Seconds until the pipeline queue is empty (None, timed_out=True when it is not within `timeout`)."""
    t0 = time.perf_counter()
    with env.connect() as conn:
        while True:
            s = aidb.pipeline_status(conn, PIPELINE)[0]
            conn.commit()
            left = max(0, int(s["source_rows"] or 0) - int(s["embedded_rows"] or 0))
            if left == 0:
                return {"seconds": round(time.perf_counter() - t0, 2), "timed_out": False, "lag_left": 0}
            if time.perf_counter() - t0 > timeout:
                return {"seconds": None, "timed_out": True, "lag_left": left}
            time.sleep(0.5)


# ---------------------------------------------------------------------------------------------------------------------------------
# (a) pipeline throughput and freshness
# ---------------------------------------------------------------------------------------------------------------------------------
def _pipeline_once(env: Env, mode: str) -> dict:
    p = env.params
    stop, probe_stop = threading.Event(), threading.Event()
    writer, sampler = Writer(env, TABLE_AIDB, p.insert_rate), Sampler(env)
    fresh = stats.Recorder()
    probes = {"sent": 0, "retrievable": 0, "timed_out": 0}

    def probe() -> None:
        retr = retrieval.AidbRetrieval(KB)
        with env.connect() as conn:
            with contextlib.suppress(Exception):          # warm this session (loads the embedding model); not a sample
                retr.retrieve(conn, "warm-up", 1)
                conn.commit()
            while not probe_stop.is_set():
                text = data.marker_text(uuid.uuid4().hex[:10])
                try:
                    key = conn.execute(f"INSERT INTO {TABLE_AIDB} (sensor_id, district, kind, body) VALUES ('probe', 'probe', 'probe', %s) RETURNING id",
                                       (text,)).fetchone()["id"]
                    conn.commit()
                except psycopg.Error as exc:
                    conn.rollback()
                    fresh.fail(_err(exc, env))
                    probe_stop.wait(p.probe_every)
                    continue
                t0 = time.perf_counter()
                probes["sent"] += 1
                found = False
                while time.perf_counter() - t0 < p.probe_timeout and not probe_stop.is_set():
                    try:
                        hits = retr.retrieve(conn, text, 5)
                        conn.commit()
                        if any(h.key == str(key) for h in hits):
                            found = True
                            break
                    except Exception as exc:                  # noqa: BLE001
                        conn.rollback()
                        fresh.fail(_err(exc, env))
                    probe_stop.wait(0.2)
                if found:
                    probes["retrievable"] += 1
                    fresh.ok(time.perf_counter() - t0)
                elif not probe_stop.is_set():
                    probes["timed_out"] += 1
                probe_stop.wait(p.probe_every)

    pt = threading.Thread(target=probe, daemon=True)
    sampler.start(stop)
    writer.start(stop)
    pt.start()
    time.sleep(p.duration)
    stop.set()
    ins = writer.finish()
    drain = wait_drained(env, p.drain_timeout)
    probe_stop.set()
    pt.join(timeout=p.probe_timeout + 30)
    sampler.join()
    sm = sampler.summary()
    sm["drain_after_inserts_stop"] = drain
    return {"inserts": ins, "freshness": {**probes, "latency": fresh.summary(),
                                          "resolution_note": "polled every 0.2 s with a retrieval call: values are upper bounds, resolution about 0.2 s plus one retrieval"},
            "pipeline": sm}


def run_pipeline(env: Env) -> dict:
    """Sustained inserts into the AIDB-backed table, once per pipeline mode. Live: the insert transaction carries the embedding work (queue depth
    is 0 by design, the cost shows in insert latency). Background: inserts are fast and a worker drains a backlog (queue depth, freshness lag)."""
    res: dict = {"modes": {}, "background_sync_interval_s": env.params.sync_interval}
    for mode in env.params.pipeline_modes:
        try:
            reset_to_base(env)
            set_mode(env, mode)
            res["modes"][mode] = _pipeline_once(env, mode)
        except Exception as exc:                              # noqa: BLE001
            res["modes"][mode] = {"error": _err(exc, env)}
    set_mode(env, "live")
    return res


# ---------------------------------------------------------------------------------------------------------------------------------
# (b) retrieval latency
# ---------------------------------------------------------------------------------------------------------------------------------
def _retriever(env: Env, backend: str, embedder):
    app = retrieval.AppRetrieval(TABLE_APP, key_column="id", text_column="body", embedding_column="embedding",
                                 embedder=embedder if backend in ("app_vector", "app_hybrid") else None)
    kb = retrieval.AidbRetrieval(KB)
    want = "aidb" if backend.startswith("aidb") else "app"
    os.environ["CPSIM_BENCH_RETRIEVAL_BACKEND"] = want
    r = retrieval.select("CPSIM_BENCH", app=app, aidb=kb, connect=env.connect)
    if r.name != want:
        raise RuntimeError(f"retrieval.select fell back to {r.name!r} (wanted {want!r}); see the log line RETRIEVAL BACKEND FALLBACK")
    return r


def _pass(env: Env, backend: str, seconds: float, loaded: bool) -> dict:
    p = env.params
    emb_rec = stats.Recorder()
    local = threading.local()
    conns: list[psycopg.Connection] = []
    clock = threading.Lock()

    def embedder(text: str):
        c = getattr(local, "c", None)
        if c is None:
            c = local.c = env.connect()
            with clock:
                conns.append(c)
        t = time.perf_counter()
        v = c.execute("SELECT aidb.encode_text_query(%s, %s) AS v", (EMBED_MODEL, text)).fetchone()["v"]
        c.commit()
        emb_rec.ok(time.perf_counter() - t)
        return v

    retr = _retriever(env, backend, embedder)
    hybrid = backend.endswith("hybrid")
    queries = env.corpus.queries(64)
    rec = stats.Recorder()
    stop = threading.Event()
    table = TABLE_AIDB if backend.startswith("aidb") else TABLE_APP
    with env.connect() as c0:
        rows_before = c0.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]
    cold: list[float] = []
    ready = threading.Barrier(p.readers + 1)

    def reader(i: int) -> None:
        # every session loads the local embedding model on its first call (hundreds of ms to seconds): warm each reader's own session first,
        # unmeasured, then start together. The first reader's first call is reported as `first_call_s`.
        try:
            conn = env.connect()
        except psycopg.Error as exc:
            rec.fail("connect: " + _err(exc, env))
            ready.wait()
            return
        with conn:
            for q in queries[:3]:
                t = time.perf_counter()
                try:
                    retr.retrieve(conn, q, p.k, hybrid=hybrid)
                    conn.commit()
                except Exception as exc:                     # noqa: BLE001
                    conn.rollback()
                    rec.fail("warm-up: " + _err(exc, env))
                if i == 0 and not cold:
                    cold.append(time.perf_counter() - t)
            ready.wait()
            n = i
            while not stop.is_set():
                q = queries[n % len(queries)]
                n += p.readers
                t = time.perf_counter()
                try:
                    hits = retr.retrieve(conn, q, p.k, hybrid=hybrid)
                    dt = time.perf_counter() - t
                    conn.commit()
                    rec.ok(dt)
                    if not hits:
                        rec.fail("no hits")
                except Exception as exc:                     # noqa: BLE001
                    conn.rollback()
                    rec.fail(_err(exc, env))

    writer = Writer(env, table, p.insert_rate) if loaded else None
    sampler = Sampler(env) if loaded and backend.startswith("aidb") else None
    sstop = threading.Event()
    threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(p.readers)]
    for t in threads:
        t.start()
    ready.wait(timeout=300)
    emb_rec.samples.clear()
    rec.samples.clear()
    if sampler:
        sampler.start(sstop)
    t0 = time.perf_counter()
    if writer:
        writer.start(stop)
    time.sleep(seconds)
    stop.set()
    for t in threads:
        t.join(timeout=60)
    elapsed = time.perf_counter() - t0
    out = {"table_rows_at_start": rows_before, "seconds": round(elapsed, 2), "readers": p.readers, "queries_per_s": round(len(rec.samples) / elapsed, 2),
           "latency": rec.summary(), "first_call_s": round(cold[0], 3) if cold else None}
    if backend in ("app_vector", "app_hybrid"):
        out["query_embedding_latency"] = emb_rec.summary()
    if writer:
        out["inserts"] = writer.finish()
    if sampler:
        sstop.set()
        sampler.join()
        out["pipeline"] = sampler.summary()
    for c in conns:
        with contextlib.suppress(Exception):
            c.close()
    return out


def run_retrieval(env: Env) -> dict:
    """Per backend: an idle pass, then a pass with inserts running. AIDB backends get one loaded pass per pipeline mode."""
    p = env.params
    res: dict = {"k": p.k, "queries": "64 generated questions, cycled", "backends": {}}
    for backend in BACKENDS:
        try:
            reset_to_base(env)
            r = {"idle": _pass(env, backend, p.retrieval_seconds, False)}
            if backend.startswith("aidb"):
                for mode in p.pipeline_modes:
                    reset_to_base(env)
                    set_mode(env, mode)
                    r[f"loaded_{mode}"] = _pass(env, backend, p.retrieval_seconds, True)
                    r[f"loaded_{mode}"]["drain_after_pass"] = wait_drained(env, p.drain_timeout)
                set_mode(env, "live")
            else:
                reset_to_base(env)
                r["loaded"] = _pass(env, backend, p.retrieval_seconds, True)
            res["backends"][backend] = r
        except Exception as exc:                              # noqa: BLE001
            res["backends"][backend] = {"error": _err(exc, env)}
    return res


# ---------------------------------------------------------------------------------------------------------------------------------
# (c) agents and (d) governance overhead
# ---------------------------------------------------------------------------------------------------------------------------------
def setup_agents(env: Env) -> dict:
    """Chat model (scripted server, AIDB's dummy model or an operator endpoint), SQL tool, purpose, and the governed and ungoverned agents."""
    from .fakemodel import FakeModel

    p = env.params
    info: dict = {"mode": p.model}
    with env.connect() as conn:
        mode = p.model
        if mode == "fake":
            host = p.callback_host or os.environ.get("CPSIM_AIDB_CALLBACK_HOST", "192.168.65.254")
            egress = conn.execute("SHOW aidb.allow_insecure_egress").fetchone()["aidb.allow_insecure_egress"]
            if not target_mod.callback_host_ok(host):
                mode, info["downgraded"] = "dummy", f"callback host {host!r} is not a private or loopback IP literal"
            elif egress != "on":
                mode, info["downgraded"] = "dummy", "the server's aidb.allow_insecure_egress is off, so AIDB cannot call the scripted server over plain http"
        if mode == "endpoint":
            url, mname = os.environ.get("CPSIM_AIDB_CHAT_URL", ""), os.environ.get("CPSIM_AIDB_CHAT_MODEL", "")
            if not url or not mname:
                raise RuntimeError("--model endpoint needs CPSIM_AIDB_CHAT_URL and CPSIM_AIDB_CHAT_MODEL (and the key in the variable named by "
                                   "CPSIM_AIDB_CHAT_KEY_ENV, default CPSIM_AIDB_CHAT_KEY)")
            from .. import outbound
            key_env = outbound.check_token_env(os.environ.get("CPSIM_AIDB_CHAT_KEY_ENV", "CPSIM_AIDB_CHAT_KEY"), "CPSIM_AIDB_CHAT_KEY_ENV")
            base = target_mod.endpoint_target(url)
            aidb.ensure_gateway_model(conn, MODEL_CHAT, kind="chat", model=mname, base_url=base, credential=CREDENTIAL, secret_env=key_env,
                                      validate=False)
            model_name = MODEL_CHAT
            info["endpoint"] = "operator-provided OpenAI-compatible endpoint (host not recorded)"
        elif mode == "fake":
            fake = FakeModel(delay=p.model_delay, tool=TOOL, tool_args={"district": env.city.districts[0]},
                             host=os.environ.get("CPSIM_AIDB_FAKE_BIND", "0.0.0.0"))  # noqa: S104
            env.chat["fake"] = fake
            os.environ[FAKE_KEY_ENV] = "cpsim-bench-fake-key"      # not a secret: the scripted server ignores it
            aidb.ensure_gateway_model(conn, MODEL_CHAT, kind="chat", model="scripted", base_url=f"http://{p.callback_host or os.environ.get('CPSIM_AIDB_CALLBACK_HOST', '192.168.65.254')}:{fake.port}/v1",
                                      credential=CREDENTIAL, secret_env=FAKE_KEY_ENV, validate=False)
            model_name = MODEL_CHAT
            info["model_delay_s"] = p.model_delay
        else:
            model_name = "dummy"
        info["effective_mode"] = mode
        env.chat["mode"] = mode
        aidb.ensure_sql_tool(conn, TOOL, description="The latest observations of the sensors of one district",
                             sql_statement=f"SELECT id, body FROM {TABLE_AIDB} WHERE district = ${{district}} ORDER BY id DESC LIMIT 3",
                             params={"district": ("TEXT", "District id")})
        aidb.ensure_purpose(conn, PURPOSE, read=[TABLE_AIDB], description="cpsim aidb-bench")
        for name, purpose in ((AGENT_OPEN, None), (AGENT_GOV, PURPOSE)):
            aidb.ensure_agent(conn, name, instructions="Answer questions about city sensor observations. Use the tool to read them.",
                              model=model_name, tools=[TOOL], purpose=purpose, timeout=int(max(10, env.params.call_timeout)))
        info["telemetry_otel_client"] = aidb.detect(conn).otel_client
    env.chat["otel"] = info["telemetry_otel_client"]
    return info


def converse_load(env: Env, agent: str, threads: int, rounds: int, turns: int) -> dict:
    """`threads` parallel conversations, each `rounds` conversations of `turns` calls. Every call is timed; errors and timeouts are counted."""
    p = env.params
    rec, conv = stats.Recorder(), stats.Recorder()
    outcomes = {"ok": 0, "error": 0, "timeout": 0}
    olock = threading.Lock()
    queries = env.corpus.queries(threads * rounds * turns + 8)
    fake = env.chat.get("fake")
    before = fake.stats() if fake else None

    def worker(i: int) -> None:
        try:
            conn = env.connect()
        except psycopg.Error as exc:
            rec.fail("connect: " + _err(exc, env))
            return
        with conn:
            for r in range(rounds):
                cid, t_conv, failed = None, time.perf_counter(), False
                for t in range(turns):
                    q = queries[(i * rounds + r) * turns + t]
                    t0 = time.perf_counter()
                    reply = aidb.converse(conn, agent, q, conversation_id=cid, timeout=p.call_timeout)
                    dt = time.perf_counter() - t0
                    with olock:
                        if reply.timed_out:
                            outcomes["timeout"] += 1
                        elif reply.ok:
                            outcomes["ok"] += 1
                        else:
                            outcomes["error"] += 1
                    if reply.ok:
                        rec.ok(dt)
                        cid = reply.conversation_id
                    else:
                        rec.fail("timeout" if reply.timed_out else (reply.error or "no message")[:100])
                        failed = True
                        break
                if not failed:
                    conv.ok(time.perf_counter() - t_conv)

    ts = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(threads)]
    t0 = time.perf_counter()
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    wall = time.perf_counter() - t0
    out = {"threads": threads, "conversations_per_thread": rounds, "turns_per_conversation": turns, "wall_seconds": round(wall, 2),
           "calls_ok_per_s": round(outcomes["ok"] / wall, 2) if wall else None, "outcomes": outcomes,
           "call_latency": rec.summary(), "conversation_latency": conv.summary()}
    if fake:
        after = fake.stats()
        out["model_server"] = {"requests": after["requests"] - before["requests"], "max_in_flight": after["max_in_flight"],
                               "delay_s": after["delay_s"]}
    return out


def run_agents(env: Env) -> dict:
    p = env.params
    res = {"agent": "governed (purpose)", "model": env.chat.get("mode"), "levels": {}}
    converse_load(env, AGENT_GOV, 1, 1, 1)                      # warm-up, not reported
    for n in p.conversations:
        res["levels"][str(n)] = converse_load(env, AGENT_GOV, n, p.rounds, p.turns)
    return res


def _decision_count(conn) -> int | None:
    try:
        n = conn.execute("SELECT count(*) AS n FROM aidb_otel.traces WHERE decision_status IS NOT NULL AND source = 'internal'").fetchone()["n"]
        conn.commit()
        return int(n)
    except psycopg.Error:
        conn.rollback()
        return None


def run_governance(env: Env) -> dict:
    """The same conversations with the ungoverned agent (no purpose) and the governed one (purpose role), interleaved so drift hits both."""
    p = env.params
    converse_load(env, AGENT_OPEN, 1, 1, 1)
    converse_load(env, AGENT_GOV, 1, 1, 1)
    passes: dict = {"open": [], "governed": []}
    with env.connect() as c:
        d0 = _decision_count(c)
    for _ in range(p.gov_reps):
        for label, agent in (("open", AGENT_OPEN), ("governed", AGENT_GOV)):
            passes[label].append(converse_load(env, agent, p.gov_conversations, p.rounds, 1))
    with env.connect() as c:
        d1 = _decision_count(c)
    rec = {}
    for label, runs in passes.items():
        samples = [r for r in runs]
        rec[label] = {"passes": [{"call_latency": r["call_latency"], "outcomes": r["outcomes"], "calls_ok_per_s": r["calls_ok_per_s"]} for r in samples],
                      "p50_ms_per_pass": [r["call_latency"]["p50_ms"] for r in samples],
                      "p95_ms_per_pass": [r["call_latency"]["p95_ms"] for r in samples]}

    def med(label, key):
        vals = [v for v in rec[label][key] if v is not None]
        return stats.percentile(sorted(vals), 50) if vals else None

    o50, g50, o95, g95 = med("open", "p50_ms_per_pass"), med("governed", "p50_ms_per_pass"), med("open", "p95_ms_per_pass"), med("governed", "p95_ms_per_pass")
    spread = lambda label: (None if not rec[label]["p50_ms_per_pass"] or None in rec[label]["p50_ms_per_pass"]  # noqa: E731
                            else round(max(rec[label]["p50_ms_per_pass"]) - min(rec[label]["p50_ms_per_pass"]), 3))
    return {"conversations": p.gov_conversations, "calls_per_pass": p.gov_conversations * p.rounds, "repetitions": p.gov_reps, "model": env.chat.get("mode"),
            "telemetry_otel_client": env.chat.get("otel"), **rec,
            "overhead": {"p50_ms": None if None in (o50, g50) else round(g50 - o50, 3), "p50_ratio": None if not o50 or g50 is None else round(g50 / o50, 3),
                         "p95_ms": None if None in (o95, g95) else round(g95 - o95, 3), "p95_ratio": None if not o95 or g95 is None else round(g95 / o95, 3),
                         "p50_spread_between_passes_ms": {"open": spread("open"), "governed": spread("governed")},
                         "note": "median over the repetitions of each pass's percentile; compare the difference with the spread between passes: a difference "
                                 "inside the spread is noise"},
            "decisions_recorded": None if None in (d0, d1) else d1 - d0}


# ---------------------------------------------------------------------------------------------------------------------------------
def environment(env: Env) -> dict:
    """Versions and server settings (no host names, no DSN)."""
    out: dict = {}
    with env.connect() as c:
        def one(q, default=None):
            try:
                v = c.execute(q).fetchone()
                c.commit()
                return next(iter(v.values())) if v else default
            except psycopg.Error:
                c.rollback()
                return default

        out["postgres"] = one("SHOW server_version")
        out["aidb"] = one("SELECT extversion FROM pg_extension WHERE extname = 'aidb'")
        out["pgvector"] = one("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        out["pgfs"] = one("SELECT extversion FROM pg_extension WHERE extname = 'pgfs'")
        out["settings"] = {k: one(f"SHOW {k}") for k in ("shared_buffers", "max_connections", "work_mem", "effective_cache_size", "max_worker_processes",
                                                        "max_parallel_workers", "aidb.allow_insecure_egress")}
        host: dict = {}
        txt = one("SELECT pg_read_file('/proc/cpuinfo')")
        if txt:
            host["cpus_seen_by_server_os"] = txt.count("\nprocessor") + (1 if txt.startswith("processor") else 0)
        mem = one("SELECT pg_read_file('/proc/meminfo')")
        if mem:
            for line in mem.splitlines():
                if line.startswith("MemTotal:"):
                    host["ram_bytes_seen_by_server_os"] = int(line.split()[1]) * 1024
        cg = one("SELECT pg_read_file('/sys/fs/cgroup/memory.max')")
        if cg:
            host["cgroup_memory_max"] = cg.strip()
        cpu = one("SELECT pg_read_file('/sys/fs/cgroup/cpu.max')")
        if cpu:
            host["cgroup_cpu_max"] = cpu.strip()
        out["server_host_best_effort"] = host
    return out
