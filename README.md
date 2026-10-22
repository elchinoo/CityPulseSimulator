# CityPulse Simulator

The field side of [CityPulse](https://github.com/elchinoo/CityPulse): build **cities** of simulated sensors, give
each group a type, a count, a value range, a reporting frequency, a protocol and a **burst percentage**, and watch the
data reach CityPulse the way it would from a real plant: through **industrial protocols** and an **edge gateway**.

```
 sensors ──▶ field devices ──Modbus TCP · EtherNet/IP · EtherCAT · PROFINET · PROFIBUS──▶ edge gateway ──HTTPS──▶ CityPulse
 (random,    (process images,        real clients and servers on loopback                 (poll, detect changed    (ingest API,
  bursty)     one per 32 sensors)                                                          channels, retry buffer)  status engine)
```

* **27 sensor types**, the same catalogue CityPulse knows (rain, light, humidity, temperature, traffic cameras and lights,
  radars, air quality, CO₂, noise, water level/flow/pressure, power meters, street lights, gas leaks, waste bins, bridge
  vibration, seismic, pedestrians, bike-share, EV chargers, parking, wind, pressure, UV, soil moisture). Any type can use
  any protocol.
* **Cities you create** in the web app, or as a YAML file you run headless. Up to 10,000 sensors per city.
* **Random bursts** driven by a percentage per group (below), plus a *burst the whole city* button.
* **Monitored by PagerLens**: `/healthz`, `/metrics` (scope-limited token) and an optional self-report.

## Demo cities that differ: the generator

A fixed preset gives the very same city every time. To get cities that differ, **generate** them: each city draws its own number of
districts (3 to 12 of the twelve in the catalogue), the sensor types that suit each district, group sizes, protocols, frequencies,
burst percentages and sometimes a narrower value range. **Same seed, size and profile give the same city; another seed gives another.**

* **Districts have a kind** (harbour, historic, centre, campus, riverside, airport, residential, industrial, green, suburb, market,
  rural) with an affinity for each of the 27 types: a harbour favours water level, wind and bridge vibration; an industrial park air
  quality, noise, power meters and gas leaks; a residential district noise, parking and waste bins; a centre traffic, parking and air
  quality. Other types stay possible but rare.
* **Size:** `small` is about 200 sensors, `medium` about 2,000, `large` about 20,000 clamped to the limit of 10,000 (or give a number
  from 8 to 10,000). Each city varies around that, and group sizes are heavy-headed: a few big groups, many small ones. Every
  protocol appears; each city leans on its own protocols, and a protocol is never given more sensors than its address space holds.
* **Profile:** `balanced`, `industrial`, `coastal`, `tourist`, `residential` (each favours its kinds of district and sensors) or
  `mixed` (the default: random themes per city).
* **Names** are made up (`port-alder`, `silverbrook`, `linden-falls`), unique, and never collide with an existing city. Generated
  cities have no URL unless you give one, name the usual `CITYPULSE_INGEST_TOKEN` variable, and can be marked *start on boot*.

**Web app:** Cities, *Generate demo cities* (how many, size, profile, optional seed and name) shows a table of what was created; the
old presets are kept below it, labelled *fixed*.

**API** (permission `sim:edit`; never replaces an existing city; 1 to 50 cities per call):

```bash
curl -X POST $URL/api/cities/generate -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"count": 10, "size": "medium", "profile": "coastal", "seed": 42}'
# optional: name (name-1, name-2... for several), target_url, token_env, autostart
# -> {"created": 10, "sensors": 19873, "cities": [{"name", "seed", "sensors", "groups", "types", "protocols",
#      "districts": [{"id", "name", "kind", "sensors", "groups", "types": [...]}]}, ...]}
```

City *n* of a batch uses seed + *n*, so the batch is reproducible. **Headless** (no Postgres, no network):

```bash
cpsim cities generate --count 10 --size medium --profile industrial --seed 42 --out cities/   # one YAML file per city
cpsim cities generate --count 3 --format json --out cities/ -v      # JSON; -v lists each city's districts and types
cpsim run cities/<name>.yaml --seconds 30                            # the files are ordinary city files
```

Without `--out` it only prints the summary. Options: `--name`, `--target-url`, `--token-env`, `--autostart`.

## Companies: simulated employees for TrustDesk

The same app also simulates an **employer** for [TrustDesk](https://github.com/elchinoo/TrustDesk). A *company* says how many
employees to create and **how many requests of each type to send**, and the simulator behaves like a client application
(TrustDesk's `/api/v1`, with a token that has the *Client application* scope).

- **Employees:** *new hires* (started in the last ~75 days, to test probation rules) and *tenured* staff (1 to 15 years, to build
  leave and sick history), in nine departments with managers above the others. With *use existing* it also picks employees that
  already exist in TrustDesk. All names and e-mail addresses are invented, and every address is on `example.com`.
- **Requests:** a count for each of the 19 request types: vacation, sick, parental, bereavement and unpaid leave; business trips;
  travel, customer, meal, medical, training, equipment, home-office and other expenses; remote work, overtime, training and
  equipment requests; and free-form "other". Up to 5,000 per type and 20,000 per run.
- **Problem share:** a percentage of the requests is built to break a typical policy (too little notice, too long, over a limit,
  no receipt, overlapping an earlier request of the same employee, duplicates, gift cards, work abroad...). The rest are written to
  comply. TrustDesk then decides what each one really is under *its* policies.
- **Reproducible:** the same seed gives the same people and requests (employee numbers carry the seed, so two seeds never clash).
  Requests carry an `Idempotency-Key`, so running again never duplicates them.
- **Behaviour:** requests per second (at most), parallel connections, a share withdrawn after sending, retry with back-off on
  `429` and `503`, and a clear failure on a refused token. Progress is shown per type; **Refresh TrustDesk statuses** reads back
  where the requests stand now.

In the web app open **Companies**, pick a preset (quick check, hundreds of mixed requests, leave stress, expense audit) or set the
count of each type yourself, **Preview** to see what would be sent, then **Save and run**. The target URL may be empty
to generate without sending. Headless:

```bash
export TRUSTDESK_CLIENT_TOKEN=<TrustDesk token with the Client application scope>
cpsim hr-validate examples/acme-hundreds.yaml
cpsim hr-run examples/acme-hundreds.yaml --dry-run     # generate only
cpsim hr-run examples/acme-hundreds.yaml               # set target_url in the file first
```

The company stores only the *name* of the token variable (it must contain `TOKEN`; the token is never saved), and the token is
sent only to the company's URL. The generated requests are checked against TrustDesk's own validation in the tests when both
repositories are side by side.

## Workloads: scheduled traffic for every service of the ecosystem

Apps like CityPulse, PayBroker or PagerLens need *constant* data to look alive. A **workload** pushes simulated traffic to one of
them on a schedule you set, for example **every 2 seconds** or **every 5 seconds**. It is stored in Postgres like a city or a company
(only the *name* of the environment variable that holds its credential, never the credential), and it runs three ways:

| Where | How | Postgres |
|---|---|---|
| **On AgentPG** (or anywhere `cpsim serve` runs) | **Workloads** tab or `/api/workloads` (roles below) | required, as today |
| **Locally in Docker, pointing at the LIVE apps** | `docker compose -f docker-compose.live.yml ... up` | **none** |
| **Headless on a host** | `pip install .` then `cpsim workloads run examples/live/paybroker.yaml` | **none** |

### The schedule

| Field | Meaning |
|---|---|
| `interval` | Seconds between ticks, **0.2 to 3600** (fractions allowed): `2`, `5`, `0.5`. |
| `jitter_pct` | 0 to 50. Each tick fires up to this share of the interval early or late around a *fixed* grid, so jitter never accumulates. |
| `batch` | Items per tick, **1 to 500**. |
| `concurrency` | Parallel requests inside one batch (1 to 64). |
| `duration` / `total` / `ticks` | Stop after this many seconds / items / scheduled ticks. Empty means forever. |
| `start_on_boot` | Start again when the app starts (a resumed AgentPG sandbox, a restarted container). |

The loop uses a monotonic clock and is **drift-free**: tick *n* is due at `start + n x interval`, whatever happened before. It **never
overlaps itself**: a tick that finds the previous batch still running is counted as `skipped` (not queued), and slots lost to a starved
event loop are skipped, never replayed in a burst. When the target answers `429` or `503` the workload **backs off** (`Retry-After`, or 1 s
doubling up to 60 s) and the ticks in that window count as skipped; one clean batch ends the backoff. A refused credential
(401, or a 403 that is not a business outcome) stops the workload with the reason. **`CPSIM_MAX_RATE`** (default 200 items per second) is
a ceiling for one workload and for all running workloads together. Per workload you see: ticks, skipped ticks (and how many were
backoff), items sent, outcomes by class, latency p50/p95, the last errors, and the time of the next tick. **Start**, **Stop** and
**Run once** (one batch right now) are in the UI and the API. Numbers live in memory (they restart with the workload); definitions live in the database.

An empty `target_url` means *generate only*: items are produced and counted as `generated`, nothing is sent. The headless command refuses
that unless you pass `--dry-run`, so a forgotten URL variable cannot pass for a working run.

### The kinds

Every identity the simulator invents is fictional: e-mail addresses on `example.com`/`example.org`, IP addresses in the documentation
ranges (`198.51.100.0/24`, `203.0.113.0/24`, `192.0.2.0/24`), test cards only. Items counted per kind are noted below.

| Kind | Target | Credential (default variable) | What a tick sends |
|---|---|---|---|
| `paybroker` | PayBroker `/v1` | merchant **secret** key (`PAYBROKER_SECRET_KEY`); optional publishable key variable (`publishable_env`) | `batch` payments in the `mix`, plus follow-ups |
| `llmgw` | LLM gateway `/v1/chat/completions`, `/v1/embeddings` | client key `lg_...` (`LLMGW_CLIENT_KEY`), else a service token with scope `llmgw.use` | `batch` chat or embedding calls |
| `pagerlens` | PagerLens `/api/ingest/*` | ingest token (`PAGERLENS_TOKEN`), else a service token with scope `pagerlens.ingest` | metrics and logs of a fictional shop, deploys, alerts, with failure injection |
| `trustdesk` | TrustDesk client API, through a company | the company's own variable (`TRUSTDESK_CLIENT_TOKEN`), else scope `trustdesk.requests.write` | one run of a company per tick |
| `identity` | the identity provider | the simulator's service client (`CPSIM_SERVICE_CLIENT_ID` / `_SECRET`) | token, discovery and JWKS calls (a load/health probe) |
| `citypulse` | CityPulse | cities (see below) | a reading from every sensor each interval |

**`paybroker`** (`params`): `mix` is a percentage per scenario (`regular` 74, `decline` 8, `three_ds` 5, `connector_failure` 5,
`card_testing` 5, `account_takeover` 3; shares are weights and are scaled to 100), `customers` (pool size; empty means sized to the rate),
`capture_pct`, `refund_pct`, `complete_3ds_pct` (follow-ups on earlier payments, see below), `capture_method` (`automatic`, `manual` or the
merchant's default), `seed`. **One unique test card per customer**, Luhn-valid in the reserved `999999` range: a card shared by customers trips
PayBroker's velocity rules and everything is rejected. Regular customers pay with their own card, with a typical basket and an idempotency
key; the decline, 3-D Secure and connector-failure scenarios use a fresh unique card per payment in the reserved prefixes `99999901`
(declined by the issuer), `99999902` (3-D Secure) and `99999903` (first connector fails, the cascade recovers). **This needs a PayBroker that
maps those prefixes** (its README, "test cards"; an older one treats them as ordinary random-behaviour cards): the public shared numbers
reach hundreds of payments an hour and PayBroker's fraud model rejects them for card velocity before they can show their behaviour. Card
testing is a burst of tiny payments from one device with a fresh card each time; account takeover is a known customer on a new device in
another country with a big pasted order; a few regular customers are brand-new accounts (`account_age_hours`). **Follow-ups**
(`capture`, `refund`, completing the 3-D Secure challenge through `/api/3ds/{id}/complete`) are done on later ticks, at most `batch` per tick
and counted as items, so the real rate can reach twice `batch / interval`. Outcomes: `ok`, `declined` (402), `fraud_rejected` (403),
`requires_action`, `capture_ok`, `refund_ok`, `complete_3ds_ok`, `followup_refused`, and the error classes. **402 and 403 are expected results,
not errors.**

**`llmgw`**: `chat_models`, `embedding_models` (the gateway's aliases; `echo-chat` and `echo-embed` exist on a fresh gateway), `chat_pct`,
`max_tokens_min`/`max_tokens_max`, `embed_inputs`, `temperature`, `seed`. Prompts rotate over generic text about fictional topics. `429
budget_exceeded` and `rate_limit_exceeded` are counted as outcome classes of their own (expected), and the loop backs off. The gateway needs a
registered client named like the service client id when a service token is used.

**`pagerlens`**: a fictional shop (`web` -> `checkout`, `catalog`; `checkout` -> `payments`, `db`; ...) named with `prefix` (default `sim-`).
Each tick sends one metrics call and one logs call (`batch` log events) per reporting service; the services, their dependencies and a baseline
deploy are registered once. `scenario`: `healthy`, `bad_deploy` (a deploy, then errors and latency on `target_service`; its dependents see a few
errors), `error_spike`, `latency`, `silence` (the target stops reporting). A failure raises one alert (`alert`, after `alert_after` ticks) so
an incident opens. Switch it live: Workloads tab > **Switch scenario**, or `POST /api/workloads/{name}/scenario`. Items are HTTP calls.

**`trustdesk`**: `company` names a company (Companies tab, or the `companies:` list of a file). The company engine is reused, not duplicated: each tick starts
one run and waits for it (a tick that finds it still running is skipped). A fixed company seed advances by one per run, because TrustDesk
deduplicates by idempotency key; no seed means every run is random. `interval` is 5 s or more. The simpler way to get recurring requests is the **schedule of
a company itself** (next section).

**`identity`**: `mix` of `token` (client_credentials), `discovery` and `jwks` calls, and an optional `scope`. **Modest by design**: at least 1 s between ticks
and at most 20 calls per tick, because every token call is a real, audited sign-in of a service client. The client secret is sent only to a token endpoint on the
provider's own origin, the access tokens are discarded unread.

**`citypulse`**: a city *is* the CityPulse workload, and it already streams on its own schedule: a city's `poll_interval` (how often the gateway polls the
field devices) and each group's `frequency` (seconds between readings of one sensor). In the app, **Cities > Live feed every 2 s / 5 s** creates a city of 60 `direct`
sensors with both set to that interval. In a headless file, `kind: citypulse` expands to the same city (`schedule.interval`, `params.sensors`, `params.seed`).

### Recurring requests for TrustDesk: a company on a schedule

A company may carry an optional `schedule`: `interval` (5 to 86400 s between run starts), `jitter_pct`, `duration`, `runs`, `start_on_boot`. In the company's page tick
*Run this company again and again* and press **Start schedule**; the latest run stays visible under Companies. It uses the same drift-free loop and counters as a workload
(it appears in `/metrics` as `company/<name>`).

### Running them

**On AgentPG** the simulator is already a sandbox. Open **Workloads**, create one, press **Save and start**. The credentials are read from the *sandbox's environment*, so the
variables must exist when the sandbox is created (`--env PAYBROKER_SECRET_KEY=... --env LLMGW_CLIENT_KEY=... --env PAGERLENS_TOKEN=... --env TRUSTDESK_CLIENT_TOKEN=...
--env CITYPULSE_INGEST_TOKEN=...`, and `CPSIM_SERVICE_CLIENT_SECRET` plus `CPSIM_OIDC_ISSUER` for service tokens and the identity probe). **A sandbox's environment can only be set at
creation: to change it, recreate the sandbox** (the database and its workloads stay). Use dedicated, narrow, revocable credentials: `--env` is not a secret store. Workloads
with *Start this workload when the app starts* resume after a pause or restart.

**Locally in Docker against the live apps** needs no Postgres:

```bash
cp .env.live.example .env.live        # the live URLs (PAYBROKER_URL, LLMGW_URL, PAGERLENS_URL, TRUSTDESK_URL, CITYPULSE_URL, IDENTITY_URL) and the credentials; git-ignored
docker compose -f docker-compose.live.yml --env-file .env.live --profile paybroker up --build     # one profile per kind, or: --profile all
# or just the image:
docker run --rm --env-file .env.live ghcr.io/<owner>/citypulsesimulator cpsim workloads run /app/examples/live/paybroker.yaml
```

The container runs the headless CLI, prints one status line every 5 seconds and stops on Ctrl-C or `docker stop`. The server image keeps working as before (`cpsim serve` is still the default
command). Edit the files in `examples/live/` (mounted read-only; `WORKLOADS_DIR` points at your own folder). Credentials are passed by name from the environment and are never baked into
an image or written to a file.

**Headless on a host**: `pip install .`, export the variables, then

```bash
cpsim workloads validate examples/live/paybroker.yaml
cpsim workloads run examples/live/paybroker.yaml [--only NAME] [--seconds N] [--dry-run] [--status-every 5]
```

A file has a `workloads:` list (and optionally `companies:` for `trustdesk`); `${VAR}` and `${VAR:-default}` are expanded in `target_url` only. A status line per workload looks like

```
[   10s] shop-payments paybroker every 2s x3 running: ticks=6 skipped=0 items=29 ok=17 capture_ok=8 declined=3 p50=80ms p95=126ms next=1.1s
```

Exit codes: **0** after a normal end, Ctrl-C or SIGTERM; **2** for a fatal misconfiguration (invalid file, empty URL without `--dry-run`, a missing credential variable, a rate above the ceiling);
**3** when a target refused the credential. When a target is not on this machine, one line says so: `notice: shop-payments sends simulated traffic to https://... (not localhost)`. Keys and
tokens are never printed or logged. `cpsim run` (cities) and `cpsim hr-run` (companies) need no Postgres either.

```yaml
workloads:
  - name: shop-payments
    kind: paybroker
    target_url: ${PAYBROKER_URL}
    token_env: PAYBROKER_SECRET_KEY     # the NAME of the variable, never the key
    params: {capture_pct: 50, refund_pct: 10, complete_3ds_pct: 80}
    schedule: {interval: 2, batch: 3, jitter_pct: 10}
```

One example per kind is in `examples/live/` (`paybroker`, `llmgw`, `pagerlens`, `trustdesk`, `identity`, `citypulse`), with intervals of 2 and 5 seconds.

### Metrics

`cpsim_workload_ticks_total{workload,kind}`, `cpsim_workload_skipped_ticks_total{workload,kind}`, `cpsim_workload_items_total{workload,kind,outcome}`,
`cpsim_workload_latency_seconds{workload,kind,quantile}` (p50 and p95 over the last 2,000 calls) and `cpsim_workloads_running`. Labels are bounded: workload names
are chosen by the operator and few, and outcome classes are a small fixed set per kind. The counters restart when a workload restarts.

## The protocols, honestly

| Protocol | Transport here | What is implemented |
|---|---|---|
| Modbus TCP | TCP | MBAP framing, function 0x03/0x04, exceptions. **Read by a third-party client (pymodbus) in the tests.** |
| EtherNet/IP | TCP | Encapsulation, session registration, CIP Get_Attribute_Single on an Assembly object. Explicit messaging only. |
| EtherCAT | UDP (0x88A4) | Frame and datagram layout, logical read command, working counter. |
| PROFINET | UDP tunnel | RT_CLASS_1 cyclic frames pushed by the devices (frame id, IO data, IOPS, cycle counter, data status). No DCP or acyclic services. |
| PROFIBUS DP | TCP stream | SD1/SD2/SC telegrams with FCS and end delimiter; the master polls slave addresses. Function codes simplified. |

The raw transports of PROFINET (layer-2 Ethernet frames) and PROFIBUS (RS-485) need privileges or hardware, so those
two are carried over UDP/TCP, like a bridge would. The telegram layouts follow the public specifications as far as this
project needs; **they are not certified for conformance and have not been tested against real PLCs.** Only Modbus TCP
is verified against an independent implementation.

Why terminate protocols at an edge gateway? That is how plants work, and it means CityPulse needs no raw Modbus or
PROFINET port: it keeps one HTTPS API, which is also all a platform such as AgentPG exposes.

## How a sensor behaves

Normal values follow a daily curve inside the group's range (`lo`..`hi`, defaulting to the type's usual range) with noise.
The **burst percentage** (0 to 100) sets how readily a sensor enters a random *burst episode*: each reading starts one
with probability `burst_pct / 100 × 0.04` (so 100 means about one episode per 25 readings, 0 never). Episodes last 3 to 20
readings (a dropout lasts 20 to 120 seconds) and are one of:

| Episode | Effect (and what CityPulse sees) |
|---|---|
| spike | a value far outside the range, often past the type's physical limits → *fault* |
| noise | much larger measurement noise |
| stuck | the sensor repeats one value → *stuck* |
| dropout | the sensor goes silent → *stale*, then *offline* |
| flood | it reports five times as often |
| corrupt | the value is not a number → sent as `null`, rejected by CityPulse |

Give a city a **seed** and the whole sequence (values and bursts) is reproducible. *Burst the whole city* multiplies every
sensor's probability by 25 for a few minutes.

## Quick start (local)

```bash
cp .env.example .env              # set POSTGRES_PASSWORD; DATABASE_URL points at the compose Postgres
make up                           # postgres:17 on 127.0.0.1:5436
pip install -e ".[dev]"
set -a; . ./.env; set +a
cpsim serve                       # applies the migrations, then http://127.0.0.1:8000 ; the setup code is printed in the terminal
```

**Postgres is required** for the web app (`DATABASE_URL`; the headless commands `cpsim run`, `hr-run` and `workloads run` need none): cities, companies, workloads, the admin password hash, sessions, API tokens and sign-in state all
live there. Earlier versions could run without a database (a JSON file under `CPSIM_DATA_DIR` and in-memory sessions); that mode is
gone, and `CPSIM_DATA_DIR` is ignored. **Upgrading from a file-based install:** existing file data is not migrated automatically.
Run `cpsim import-files <the old CPSIM_DATA_DIR>` once to load its cities, companies and admin password (specs that no longer
validate are skipped and counted); or simply re-create cities and companies from their YAML/JSON. An install that already used
Postgres upgrades in place: `cpsim serve` (or `cpsim migrate`) keeps its cities, companies and admin password.

Open the app, enter the setup code, choose a password, click **Create a demo city** (174 sensors over all five
protocols), set **CityPulse URL**, export the token and press **Start city**:

```bash
export CITYPULSE_INGEST_TOKEN=<an ingest token created in CityPulse Settings>   # before `cpsim serve`
```

Headless, from a file: `cpsim validate examples/metropolis.yaml` then `cpsim run examples/metropolis.yaml`.
Without a CityPulse URL a city only generates data (useful to point your own Modbus/PROFIBUS tooling at its devices; the
city page shows each endpoint). Endpoints bind to loopback unless you tick *Let other machines reach the protocol endpoints*.

## Configuration

| Variable | Meaning |
|---|---|
| `DATABASE_URL` / `DB_EXTERNAL_DSN` / `PSQL_CONNECTION_STRING` | **Required.** The Postgres database (the AgentPG way) holding cities, companies and the credentials below. |
| `CITYPULSE_INGEST_TOKEN` (or the variable a city names in `token_env`) | The ingest token for that city's CityPulse. Also read from `<NAME>_FILE`, or saved under Settings > Credentials. **Never stored in the city itself.** |
| `TRUSTDESK_CLIENT_TOKEN` (or the variable a company names in `token_env`) | A TrustDesk token with the *Client application* scope, for Companies. Also read from `<NAME>_FILE`, or saved under Settings > Credentials. **Never stored in the company itself.** |
| `CPSIM_METRICS_TOKEN` | Lets `/metrics` be scraped with a token that can read nothing else. |
| `CPSIM_API_KEY` | Optional static full-access key for automation. API tokens (read, metrics or full) can also be created under Settings. |
| `PAGERLENS_URL`, `PAGERLENS_TOKEN` | Report to PagerLens (an ingest token from PagerLens). https required unless localhost. |
| `PAYBROKER_SECRET_KEY`, `LLMGW_CLIENT_KEY`, `PAGERLENS_TOKEN` (or the variable a workload names in `token_env`) | Credentials for workloads. Also read from `<NAME>_FILE`, or saved under Settings > Credentials. **Never stored in the workload itself.** A name must contain `TOKEN`, `KEY` or `SECRET` and may not be one of the simulator's own (`CPSIM_*`) or the database's. |
| `CPSIM_ENCRYPTION_KEY[_FILE]` | Platform secret that unlocks the credential store at start, so credentials saved under Settings > Credentials survive a restart. Without it nothing can be saved. |
| `CPSIM_MAX_RATE` | Ceiling on items per second, per workload and for all running workloads together (default 200). |
| `CPSIM_ALLOW_HTTP_TARGETS` | `true` allows plain `http` to hosts other than this machine, for local stand-ins reached by name (default off: `https` or localhost only). |
| `CPSIM_ALLOW_ANONYMOUS` | Demo only: disables authentication entirely. |

## Single sign-on and roles (the ecosystem identity provider)

CityPulse Simulator runs **standalone** (the setup code and an admin password, as above) or **federated** with the ecosystem's
[identity provider](https://github.com/elchinoo/identity). Register it there as a `web` app with client id `cpsim`, redirect URI
`https://<simulator>/auth/callback` and the roles below, then set:

| Variable | Meaning |
|---|---|
| `CPSIM_OIDC_ISSUER` | The identity provider's URL. Federation is off when unset. |
| `CPSIM_OIDC_CLIENT_ID` | Default `cpsim`. |
| `CPSIM_OIDC_CLIENT_SECRET[_FILE]` | The app's secret from the provider. |
| `CPSIM_PUBLIC_URL` | The URL the simulator is reached at (used to build the callback). |
| `CPSIM_LOCAL_LOGIN` | `false` removes the admin-password login when SSO is on (default keeps it as a break-glass). |

| Role | May |
|---|---|
| `admin` | everything, including the admin password and API tokens |
| `operator` | see everything; create, change and delete cities, companies and workloads; start, stop and boost cities; run, stop and refresh companies; start, stop and run workloads once. Not tokens, password or `/metrics` |
| `viewer` | see cities, companies, workloads, previews, statistics and run results; change nothing |

Services can use `client_credentials`: scope `cpsim.read` lets a service read (as a viewer does), `cpsim.metrics` lets it scrape
`/metrics`. The static `CPSIM_API_KEY` stays full access and `CPSIM_METRICS_TOKEN` stays scrape-only. A browser session never
scrapes `/metrics`. The UI hides what a role cannot do (a viewer sees controls disabled); the server enforces it.

**The simulator as a client of other apps.** Where a city or company names a token variable that is empty
(`CITYPULSE_INGEST_TOKEN`, `TRUSTDESK_CLIENT_TOKEN`) and for PagerLens reporting (`PAGERLENS_TOKEN`), the simulator can mint a
short-lived token from the identity provider instead. A static token always wins when set. It needs a **second registration**, a
`service` client (the web client above may not use `client_credentials`):

| Variable | Meaning |
|---|---|
| `CPSIM_OIDC_ISSUER` | The same issuer as above. |
| `CPSIM_SERVICE_CLIENT_ID` | Default `cpsim-service`. |
| `CPSIM_SERVICE_CLIENT_SECRET[_FILE]` | Its secret. Setting it turns the feature on. |

Register `cpsim-service` as a `service` client allowed to call the apps `citypulse`, `trustdesk`, `pagerlens` and `llmgw`. The simulator keeps
one token source per callee and asks each only for its own scope: `citypulse.ingest`, `trustdesk.requests.write`, `pagerlens.ingest`, `llmgw.use` (the gateway also needs a
registered client named `cpsim-service`). PayBroker takes merchant keys, not identity tokens.
There is no per-city or per-company switch: the minted token is used exactly when the named variable is empty.

## Monitored by PagerLens

Either run `pagerlens-agent` against `/healthz` and `/metrics` (create a scrape token: `CPSIM_METRICS_TOKEN`), or set
`PAGERLENS_URL`/`PAGERLENS_TOKEN` (or the service client above) and the simulator pushes `up`, `request_rate` (readings delivered per second) and
`error_rate` (failed deliveries) for the service `citypulse-simulator`, which *depends on* `citypulse-ingest`, and raises
`SimulatorCannotReachCityPulse` when delivery keeps failing. `/metrics` also has `cpsim_*` series per city and protocol.

## Deploy on AgentPG

```bash
agentpg login
agentpg create --name cpsim-db --size small                 # an instance for the simulator (required: Postgres holds all state)
agentpg sandbox create cpsim \
  --image ghcr.io/<owner>/citypulsesimulator:latest \
  --instance <instance-id> --port 8000 \
  --env CITYPULSE_INGEST_TOKEN=<ingest token> --env CPSIM_METRICS_TOKEN=<random string>   # DATABASE_URL comes from the linked instance
```

Workloads need their credentials in the same `--env` list (see Workloads). The image must be public on GHCR. `--env` is not a secret store: anyone with access to the sandbox's definition can read
it, so use a dedicated, narrow `ingest` token that you can revoke. Free-tier sandboxes auto-pause when idle, which stops the simulator; a
resumed sandbox restarts cities that have *Start this city when the app starts* ticked. The full walkthrough for all four
projects is in the CityPulse tutorial.

## Tests

```bash
pip install -e ".[dev]" && ruff check src tests && pytest -q          # database tests are skipped without TEST_DATABASE_URL
make up && make test-db                                                  # all of them, on a throwaway database in the compose Postgres
```

(The kit, `agentpg-kit`, is installed from the sibling checkout with `uv`, or `pip install "agentpg-kit @ https://github.com/elchinoo/agentpg-kit/archive/refs/tags/v0.2.0.tar.gz"`.)

Workloads are covered by schedule maths on a fake clock (drift, jitter bounds, skipped ticks, duration and total stops, backoff), every generator checked against the target's own request rules
(PayBroker's models and the gateway's validator are loaded from the sibling checkouts when present), outcome classification, mock-transport senders, secrets never stored or echoed, the web API per role and scope,
the schema upgrade, and the headless CLI as a real process with no Postgres. Also covers every protocol stack (including malformed input, dead links, wrong addresses and a pymodbus interoperability
check), the generator (burst probability rises with the percentage; seeds reproduce; every episode behaves as described),
validation of cities, a whole city across all five protocols into a sink, back-pressure and retry, and the web API.
The demo-city generator is tested for determinism, variability (districts, types, sizes, protocols), limits, unique names, the API per role, the CLI as a real process without Postgres, and the browser form with a fake DOM (needs `node`).

## Honest limits

- **Company runs:** the requests are plausible, not realistic. They assume common policy values (25 vacation days, 30 days' notice
  for long leave, receipts from 25 USD...); a different policy set will judge them differently. Employees never answer when an
  approver asks for information, and the simulator cannot approve or reject (a client token cannot). Results of a run live in memory
  and are lost on restart.

* One protocol endpoint per protocol per city, hosting up to 32 sensors per device; a city is capped at 10,000 sensors.
* The gateway and devices share a process, so this shows data-path behaviour and failure modes, not network latency.
* A protocol's address space caps its devices (PROFIBUS DP: 125 slaves, so 4,000 sensors).
* Rate limiting of failed sign-ins is in memory, per process. Sessions live in Postgres and survive a restart.

Licensed under Apache-2.0. See [SECURITY.md](SECURITY.md).

## Credentials saved in the app

A workload, city or company names a credential by variable name (`PAYBROKER_SECRET_KEY`). The value is read from the environment when
it is set there, otherwise from **Settings > Credentials**, where an administrator pastes it once. It is encrypted with the vault
(data key wrapped under `CPSIM_ENCRYPTION_KEY` and the admin password), stored as ciphertext in `cpsim_credential`, decrypted only in
process memory, never returned by the API or shown again, and sent only to the target URL of the workload that names it. Apps that trust
the identity provider (CityPulse, PagerLens, TrustDesk, the gateway) need no saved credential: the simulator signs in as its own service
client. Saving, replacing and deleting need `settings:manage` (administrators only). Headless and Docker runs have no database and use
environment variables or files as before.
