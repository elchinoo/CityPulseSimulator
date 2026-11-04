# `cpsim aidb-bench`: the simulator as the load generator for AIDB

An **optional, operator-run** mode. Nothing in `serve`, `run` or `workloads` imports it, and none of their behaviour changed. It
implements the idea in the ecosystem plan (section 5.4 of `03-aidb-integration.md`) that the simulator becomes the load and workload
generator **for EDB AI Accelerator (AIDB) itself**: it builds a simulated city, turns the sensors' observations into text rows, and drives
an AIDB database with them while it measures.

> **Read this first.** The numbers it produces are **indicative**. A laptop running Docker Desktop is not a benchmark platform (shared
> CPU, a virtual disk, the load generator and the database competing for the same cores). Every report carries that warning, the CPU and
> RAM it ran on, the versions, the parameters and the raw percentiles, so nobody has to guess what a figure means. Nothing here measures
> the quality of answers, recall or ranking. Repeat it on your own target before you rely on a number.

## What it measures

| | Scenario | What is reported |
|---|---|---|
| (a) | **Pipeline throughput and freshness** under sustained inserts into a table with an AIDB pipeline | rows/s offered and achieved; rows/s the pipeline embedded; **queue depth** (source rows minus rows in the knowledge base, from `aidb.pipeline_metrics`, sampled each second) with its trend; time to drain after the inserts stop; **freshness lag**: p50/p95/p99 of the time from a probe row's commit until it is returned by `retrieve_text`. Run once for `Live` and once for `Background` auto-processing (below). |
| (b) | **Retrieval latency** while inserts run, for a city of 8 to 10,000 sensors (default 200) | p50/p95/p99/max and queries/s for five backends built with the kit's `apgkit.retrieval` (`select()`): `app_fts` (full-text), `app_vector` (pgvector), `app_hybrid`, `aidb` (`retrieve_text`) and `aidb_hybrid`; an idle pass and a pass with inserts at the offered rate, same corpus size for every pass. |
| (c) | **Concurrent agent conversations**: N parallel `converse` calls (default 1, 4 and 8 parallel, 2 turns each) | per-call and per-conversation latency percentiles, calls/s, and **errors and timeouts counted, with their kinds**. The model is a scripted OpenAI-compatible server run by the command (`--model fake`), AIDB's built-in `dummy` model, or an operator endpoint (below). |
| (d) | **Governance overhead**: the same conversations with an agent that has **no purpose** and with one confined by a **purpose role** | the two latency distributions side by side, interleaved over several passes, the median-of-passes difference and ratio, the spread between passes (a difference inside the spread is noise), and the number of decisions AIDB recorded. |

### Two pipeline modes, because they behave differently

Measured on AIDB 7.9.0 and in line with EDB's documentation of auto-processing:

* **Live**: triggers embed a row **inside the inserting transaction**. There is no queue by design (queue depth is 0); the cost shows
  in **insert latency** (about 20 ms per row with the local `bge-small` model on the sample machine) and under load in everything else
  that wants the same CPU.
* **Background**: inserts are cheap and a per-pipeline worker drains a backlog every `background_sync_interval` (`--sync-interval`,
  default 2 s). Queue depth and freshness lag are what you watch. Needs `shared_preload_libraries = 'aidb'` on the server.

Things the harness had to learn the hard way, so you do not repeat them: the first call in every new session loads the embedding model
(hundreds of milliseconds, seconds when it has to be downloaded), so every reader session is warmed up before it is timed and each
writer's first batch is not timed; AIDB does **not** process an existing backlog when you switch a pipeline to Live, so the harness drains
before it switches; after a switch to Background the pipeline's worker started up to about 30 s later, so the harness waits for it;
AIDB's own `unprocessed rows` metric stayed stale after a Background to Live switch, so queue depth is computed as source rows minus
knowledge-base rows (the metric is kept next to it in the JSON).

## Running it

You need an **operator-level** DSN of a database that has the AIDB extension available (it creates tables, a pipeline, models, a
purpose role, a SQL tool and two agents, all named `cpsim_bench*`, and removes them again unless you pass `--keep`). A throwaway container
from the ecosystem's `aidb-postgres:17` image is the intended target:

```bash
docker run -d --name cpsim-aidb-bench -p 127.0.0.1:55461:5432 -e POSTGRES_PASSWORD=... aidb-postgres:17 \
  postgres -c shared_preload_libraries=aidb -c aidb.allow_insecure_egress=on
docker exec ... psql -U postgres -c "CREATE DATABASE cpsim_bench"          # the name must end in bench or test

export CPSIM_TARGET_ALLOWLIST=127.0.0.1 CPSIM_ALLOW_PRIVATE_URLS=true      # see the safety rules below
export CPSIM_AIDB_DSN_FILE=/path/to/file-holding-the-dsn                   # or CPSIM_AIDB_DSN, or be asked (hidden prompt)
cpsim aidb-bench --check                                                    # validates the target, changes nothing
cpsim aidb-bench --sensors 200 --history 5 --duration 30 --retrieval-seconds 15 \
  --hardware-note "Docker Desktop, 8 CPUs 16 GB, MacBook" --enable-telemetry
```

Note the `postgres -c shared_preload_libraries=aidb` in the container command: the image's default command sets it, and **giving the
image only `-c ...` flags replaces that command and silently drops the setting**, which disables Background mode (the harness says so and
records the mode as failed rather than hanging). `aidb.allow_insecure_egress=on` is only needed for the scripted model server (AIDB
refuses plain http otherwise) and must never be on in production. The first run in a new container downloads the 64 MB `bge-small`
model from Hugging Face.

Useful options (`cpsim aidb-bench --help` lists all): `--scenarios pipeline,retrieval,agents,governance`, `--sensors N` (8 to 10,000),
`--history` (preloaded observations per sensor), `--insert-rate` (rows/s offered, `0` = as fast as possible), `--pipeline-mode
both|live|background`, `--readers`, `-k`, `--conversations 1,4,8`, `--turns`, `--rounds`, `--gov-reps`, `--model fake|dummy|endpoint`,
`--model-delay`, `--call-timeout`, `--enable-telemetry`, `--out DIR`, `--keep`.

### Agents and the model

* `--model fake` (default): a scripted OpenAI-compatible server started by the command, listening on `0.0.0.0` (override with
  `CPSIM_AIDB_FAKE_BIND`) and reached by the database at `--callback-host` / `CPSIM_AIDB_CALLBACK_HOST` (default `192.168.65.254`, Docker
  Desktop's address for the host). It answers every request after `--model-delay` seconds (default 0.2 s): first a tool call to the SQL tool
  that reads the latest observations of a district, then, once the tool result is in the prompt, a canned text. It is derived from the kit's
  `tests/aidbfake.py` but threaded and stateless so parallel conversations are really parallel. **The agent numbers therefore measure
  AIDB's agent loop, the tool's SQL and the model hop with a fixed delay, not any real model.** If the server's
  `aidb.allow_insecure_egress` is off or the callback address is not a private IP literal, the run falls back to `dummy` and says so.
* `--model dummy`: AIDB's built-in dummy model ("Hello world!"): no tool use, no model hop.
* `--model endpoint`: a real OpenAI-compatible endpoint given by `CPSIM_AIDB_CHAT_URL` (https, or a private address with the egress
  setting), `CPSIM_AIDB_CHAT_MODEL`, and the key in the variable named by `CPSIM_AIDB_CHAT_KEY_ENV` (default `CPSIM_AIDB_CHAT_KEY`; the
  name is checked like any `token_env`). The endpoint host must be in `CPSIM_TARGET_ALLOWLIST`. **This path was not exercised against a
  real provider when this page was written** (no provider key was available); the credential goes into an AIDB credential through the
  kit, never into the report.

### Safety rules (the same pattern as every other outbound target)

* The DSN comes from `CPSIM_AIDB_DSN` (or the file named by `CPSIM_AIDB_DSN_FILE`) or a hidden prompt. It is **never printed, never in the
  report, never stored**; error text is scrubbed of it and the report writer refuses to write a document that contains a connection URL
  or the DSN.
* The target must be **allow-listed**: its host has to appear in `CPSIM_TARGET_ALLOWLIST` (`.example.org` matches the domain). This
  list is read as it is: the hosts of the ecosystem URLs (`CITYPULSE_URL`, ...) do **not** count, because they are not database hosts,
  and an empty list refuses everything (exit code 3, nothing is contacted).
* The kit's NetGuard resolves the host **once** and the connection is pinned to that address (`hostaddr`); private and loopback addresses
  need `CPSIM_ALLOW_PRIVATE_URLS=true`, link-local (cloud metadata) is always refused. Unix sockets, multi-host DSNs and a DSN that already
  sets `hostaddr` are refused.
* The database name must end in `bench` or `test` (it creates and drops objects), unless `--allow-any-database`.
* Exit codes follow the other commands: 2 for invalid parameters or no target, 3 for a refused target, 1 when setup or a scenario failed.

What a run leaves behind: the objects above are removed (the harness then checks the catalogs and says what is still there). AIDB
keeps the **transcripts of the benchmark's conversations** (`aidb.conversations`) even when an agent is deleted with `force => true`, and
telemetry spans if recording was on; that is one more reason the target is a throwaway database that you drop afterwards.

The bench is an operator tool: it does **not** use the app's row MACs or sealed boot (it creates throwaway objects and removes them), and
it needs the database owner or a superuser for `CREATE EXTENSION`, `CREATE ROLE` and, with `--enable-telemetry`, `ALTER DATABASE ... SET
aidb.otel_client` (undone at the end). Without `--enable-telemetry` the governance comparison runs with AIDB's default (`noop`)
telemetry, so decisions are not recorded and the overhead is lower than with recording on; the report states which one was used.

## The report

Each run writes `aidb-bench-<UTC time>.json` and `.md` into `--out` (default `./aidb-bench-reports`):

* `hardware.client` (OS, CPUs, RAM, Python), `hardware.server_host_best_effort` (CPUs, RAM and cgroup limits as the **server's** OS
  reports them, read with `pg_read_file` when the role may), `hardware.note` (your `--hardware-note`);
* `versions` (cpsim, agentpg-kit, psycopg, PostgreSQL, AIDB, pgvector, pgfs, server settings), `parameters` (everything that shaped the
  run), `city` (sensors, districts, seed, preloaded rows), `setup` (preload and backfill timings);
* `scenarios.<name>` with the **raw summary of every latency distribution**: `n`, `min`, `mean`, `p50`, `p95`, `p99`, `max` in
  milliseconds, plus error counts and kinds. With fewer than 100 samples a p99 is not a meaningful estimate; `n` is always there so you
  can tell;
* `skipped` (a scenario that failed, with the reason) and `notes` (for example "scripted model not used"). The host name of the target is
  deliberately not recorded.

The harness itself is tested without AIDB (statistics, allow-list refusals, report shape, the scripted model server, CLI refusals:
`tests/test_aidb_bench.py`, part of the normal suite). One end-to-end test is marked `aidb` and skips without `AIDB_TEST_URL` (a
superuser DSN of the `aidb-postgres:17` image).

## Limits, stated plainly

* One client process (threads, a connection each); one run; no repetitions of the whole run and no confidence intervals. The
  governance scenario repeats and interleaves its passes precisely because a single pair of passes cannot separate overhead from noise.
* Freshness is measured by polling (every 0.2 s, one retrieval per poll): values are upper bounds with roughly that resolution. Probes are
  one at a time, so few of them fit in a short run (`n` is reported).
* `app_vector` and `app_hybrid` embed each query with AIDB's local model (`aidb.encode_text_query`), the only offline embedder available,
  so those backends include a model call that a real app would take from its own provider. The embedding is also listed on its own. Rows
  inserted during a pass are not embedded for the app backends.
* Retrieval quality is not measured (the kit's tests already compare the answers of the backends on small data).
* The city's text is synthetic and short; real documents, chunking and other models change every number.
* Governance overhead is measured for agents with a purpose role (the SQL tool runs as that role). The cost of purpose decisions with
  `--enable-telemetry` on is included; the cost of other governance features is not measured.
* Not verified: an AgentPG-hosted AIDB (the platform's login role did not hold the AIDB grants when this was written), a real chat
  model, GPU embedding, and any size above what the sample machine could finish in reasonable time (the generator and the corpus cap
  are tested at 10,000 sensors; the database side was run at the sizes in the sample report).

## Sample report (small city, Docker Desktop on a laptop)

The full report of one real run is kept next to this page: [`aidb-bench-sample/aidb-bench-20261104T195610Z.md`](aidb-bench-sample/aidb-bench-20261104T195610Z.md)
and its [JSON](aidb-bench-sample/aidb-bench-20261104T195610Z.json) (raw percentiles, parameters, versions). Setup: a throwaway
`aidb-postgres:17` container (PostgreSQL 17.11, AIDB 7.9.0, pgvector 0.8.7, default settings: `shared_buffers=128MB`,
`max_worker_processes=8`) on **Docker Desktop on a MacBook**: the host has 14 cores and 48 GB, the Docker VM sees **8 CPUs and 15.4 GB**,
the client and the database share it, nothing else was running. City of 200 sensors (generated, seed 1), 5 preloaded observations per
sensor (1,000 rows per table), 50 rows/s offered by 2 writers, local `bge-small-en-v1.5-f16` embeddings (384 dimensions, CPU),
scripted model with 0.2 s per answer, telemetry recording on. About 6.5 minutes for the whole run. **One run; indicative; do not quote
these as AIDB's performance.** The command was:

```bash
cpsim aidb-bench --sensors 200 --history 5 --duration 30 --retrieval-seconds 15 --conversations 1,4,8 --rounds 3 --turns 2 \
  --gov-conversations 4 --gov-reps 3 --enable-telemetry --probe-every 1 --hardware-note "Docker Desktop on a MacBook ..."
```

What it measured on that machine (milliseconds unless stated; the report has every number, `n` and the error counts, which were 0
everywhere except where shown):

| | Live pipeline | Background pipeline (2 s sync interval) |
|---|---|---|
| inserts achieved of 50 rows/s offered | 47.7 rows/s (11 batches behind schedule) | 50.0 rows/s |
| insert batch of 10 rows, commit latency p50 / p95 / p99 | 174 / 424 / 994 | 4.4 / 7.0 / 8.5 |
| rows embedded per second during the run | 48.0 | 47.9 |
| queue depth (rows waiting) max / mean | 0 / 0 (by design) | 141 / 102 |
| freshness lag, commit to retrievable, p50 / p95 / p99 (n = 12) | 40 / 605 / 895 | 1,819 / 2,199 / 2,298 |
| time to drain after the inserts stopped | 0.04 s | 3.7 s |

Retrieval latency, k = 5, 2 reader threads, 15 s per pass, tables of 1,000 rows (p50 / p95 / p99, queries per second):

| backend | idle | with inserts (50 rows/s offered) |
|---|---|---|
| `app_fts` (full-text) | 28 / 43 / 46 (73/s) | 38 / 62 / 68 (52/s) |
| `app_vector` (pgvector) | 12 / 17 / 29 (156/s) | 12 / 18 / 30 (154/s) |
| `aidb` (`retrieve_text`) | 10 / 15 / 18 (172/s) | Live: **107 / 1,358 / 1,476 (6/s)**, and only 5.2 rows/s of the 50 offered were inserted; Background: 10 / 14 / 16 (181/s) |
| `aidb_hybrid` | 39 / 49 / 75 (46/s) | Live: 148 / 1,438 / 1,547 (6/s); Background: 45 / 61 / 81 (43/s) |
| `app_hybrid` | 41 / 60 / 83 (49/s) | 52 / 76 / 91 (38/s) |

Concurrent agents (scripted model, governed agent, 2 turns per conversation, 3 conversations per thread): 1, 4 and 8 parallel gave
2.9, 11.3 and 22.3 calls/s, p50 331 / 349 / 354 ms and p99 469 / 486 / 493 ms (each call includes two 0.2 s model answers), **0 errors
and 0 timeouts in 78 calls**, and the model server saw up to 8 requests in flight. That shows the agent loop and the database side scale
across 8 parallel conversations when the model is instant and fixed; it says nothing about a real model.

Governance overhead (4 parallel, 3 interleaved passes of 12 calls each): median p50 463.3 ms with a purpose role against 465.0 ms
without (difference -1.7 ms by the median of passes), spread between passes 17 ms (open) and 5 ms (governed): **the purpose role's cost
is inside the noise** at this scale, with decisions recorded (36 recorded).

How to read it, carefully: the two things that stand out are visible in the report itself. **Live mode puts the embedding in the
inserting transaction**: 10-row batches took about 170 ms, and when retrieval ran at the same time the readers' tail (p95 above one
second) and the writers' throughput (5 of 50 rows/s) both suffered, on this CPU-only setup; **Background mode** kept inserts at
single-digit milliseconds and retrieval unaffected, and paid for it with a freshness lag of about two seconds (the sync interval) and a
backlog that cleared four seconds after the inserts stopped. A GPU, a remote embedding endpoint, a larger server or another sync interval
would change all of this, which is what the command is for. The `app_fts` figure is larger than at smaller table sizes because the kit's
keyword query computes the text-search vector per row; the GIN index the harness creates may not serve that query (not checked).
