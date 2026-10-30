# CityPulse Simulator (`cpsim`): complete guide

This is the single reference for the CityPulse Simulator. The [README](../README.md) is the feature tour; [SECURITY.md](../SECURITY.md) is the
security policy; this guide ties them together with the ecosystem, installation, configuration, operations and development. Everything here was
checked against the code of this repository (version 0.2.0, `src/cpsim`; later changes are described where they apply) and the ecosystem manifest `ecosystem/ecosystem.yaml`. What could not be
verified is listed in [Known limitations](#16-known-limitations-and-things-not-verified).

Contents

1. [Purpose and audience](#1-purpose-and-audience)
2. [How it fits the ecosystem](#2-how-it-fits-the-ecosystem)
3. [Interaction with each project](#3-interaction-with-each-project)
4. [Architecture](#4-architecture)
5. [Install](#5-install)
6. [Configuration reference](#6-configuration-reference)
7. [First-run walkthrough](#7-first-run-walkthrough)
8. [Using it, feature by feature](#8-using-it-feature-by-feature)
9. [HTTP API reference](#9-http-api-reference)
10. [Roles and permissions](#10-roles-and-permissions)
11. [Security notes](#11-security-notes)
12. [Operations](#12-operations)
13. [Testing and development](#13-testing-and-development)
14. [AIDB and AgentPG integration](#14-aidb-and-agentpg-integration)
15. [Command reference](#15-command-reference)
16. [Known limitations and things not verified](#16-known-limitations-and-things-not-verified)

---

## 1. Purpose and audience

The simulator is the **field side** and the **load generator** of the ecosystem. It exists so that the other apps have believable, continuous,
reproducible traffic to show:

* **Cities** of simulated sensors (27 sensor types, up to 10,000 sensors per city). Each sensor group has a type, a count, a district, a value
  range, a reporting frequency, a protocol and a **burst percentage**. Data travels the way it would in a plant: sensors, field devices
  (Modbus TCP, EtherNet/IP, EtherCAT, PROFINET, PROFIBUS, or `direct`), an edge gateway, then HTTPS to CityPulse.
* **Companies**: simulated employers whose employees and requests (19 request types) go to TrustDesk through its client API.
* **Workloads**: scheduled traffic ("every 2 seconds", "every 5 seconds") for PayBroker, ShopFront shoppers, the LLM gateway, PagerLens, TrustDesk, the identity
  provider and CityPulse.

Who it is for:

| Reader | Use |
|---|---|
| Presenter or sales engineer | Start a city, a payment stream and a failure scenario, then show what the other apps do with them. See [VIDEO-SCRIPT.md](VIDEO-SCRIPT.md). |
| Developer of an ecosystem app | A repeatable source of data and load: same seed, same data. Headless CLI, no Postgres needed. |
| Operator of the fleet | A sandbox with a web app, an API, `/healthz`, `/metrics`, and a self-report to PagerLens. |
| Benchmarker | A paced, drift-free load generator with a hard rate ceiling (`CPSIM_MAX_RATE`). |

Everything it invents is fictional: e-mail addresses on `example.com` or `example.org`, IP addresses in the documentation ranges, test cards in the
reserved `999999` range.

## 2. How it fits the ecosystem

The simulator is a **client of the other apps**, never a server for them: nothing in the fleet calls it, except an observer that scrapes
`/metrics` (PagerLens agent or Prometheus). It is also a **web app of its own** that signs people in through the identity provider.

```mermaid
flowchart LR
  subgraph people[People and automation]
    U[Operator in a browser]
    A[Automation: API key or token]
    P[PagerLens agent or Prometheus]
  end
  subgraph cpsim[cpsim sandbox]
    UI[Web app and API\nFastAPI]
    RT[Runtime\ncities, companies, workloads]
    ST[(Postgres\nAgentPG instance)]
  end
  IDP[identity\nOIDC provider]
  CP[CityPulse\n/api/readings]
  TD[TrustDesk\n/api/v1]
  PL[PagerLens\n/api/ingest]
  GW[llm-gateway\n/v1]
  PB[PayBroker\n/v1]
  SF[ShopFront\n/api/v1]
  U -->|OIDC code + PKCE, roles| IDP
  U --> UI
  A -->|Bearer| UI
  P -->|GET /metrics, scope cpsim.metrics| UI
  UI --> RT
  UI <--> ST
  RT -->|client_credentials: citypulse.ingest| IDP
  RT -->|ingest token or service token| CP
  RT -->|client token or trustdesk.requests.write| TD
  RT -->|ingest token or pagerlens.ingest| PL
  RT -->|client key lg_ or llmgw.use| GW
  RT -->|merchant secret key sk_test_| PB
  RT -->|service token shop / shopfront.api, or guest| SF
  RT -.->|self-report: up, request_rate, error_rate| PL
```

What `ecoctl` does for it (from `ecosystem.yaml`, entry `cpsim`):

* image `ghcr.io/elchinoo/citypulsesimulator`, variable prefix `CPSIM`, one AgentPG database and one sandbox (`<fleet>-cpsim`);
* `generate: [CPSIM_ENCRYPTION_KEY]`: a random platform secret that unlocks the credential store after every restart;
* roles `admin`, `operator`, `viewer` registered on the identity provider; web client id `cpsim`;
* `exposes: [cpsim.read, cpsim.metrics]`: scopes other services may request **from** the simulator;
* `calls`: citypulse `citypulse.ingest`, trustdesk `trustdesk.requests.write`, pagerlens `pagerlens.ingest`, llmgw `llmgw.use` (and shopfront `shopfront.api`, once the proposed ecosystem manifest change is made). This creates a second
  identity client, the `service` client `cpsim-service`, allowed to request only those scopes, and injects `CITYPULSE_URL`, `TRUSTDESK_URL`,
  `PAGERLENS_URL`, `LLMGW_URL` (and `SHOPFRONT_URL`);
* `uses: [paybroker, identity]`: injects `PAYBROKER_URL` and `IDENTITY_URL` without a scope, so the simulator holds all fleet addresses (seven with ShopFront).
  Deploy order puts the callees first, and `ecoctl recreate paybroker` (or identity, or any callee) recreates the simulator as well, so its environment
  carries the new addresses.

## 3. Interaction with each project

Every outbound call is made by the simulator to the other app. No call goes the other way except the metrics scrape.

### Common rules for outbound credentials

For each target the simulator picks a bearer credential like this (`credentials.py`, `tokens.py`, `kinds/__init__.py`):

1. The variable the city, company or workload **names** (`token_env`). Its value is read from the environment, then from the file named by
   `<NAME>_FILE`, then from **Settings > Credentials** (saved, encrypted). The environment wins over a saved value.
2. If that is empty and the service client is configured (`CPSIM_OIDC_ISSUER` plus `CPSIM_SERVICE_CLIENT_SECRET`), a **short-lived token** is minted from
   the identity provider with the `client_credentials` grant, one token source per callee, asking only for that callee's scope. There is no per-city
   switch: the minted token is used exactly when the named variable is empty.
3. Otherwise starting fails with a message naming the variable.

Redirects are never followed, and a credential is only ever sent to the target URL of the item that names it.

### CityPulse (receives readings)

| Aspect | Detail |
|---|---|
| Call | `POST {target}/api/readings`, JSON `{"readings": [...]}`; batches of 100 readings, up to 8 batches per second (`engine.py`). |
| Reading | `sensor`, `type`, `district`, `protocol`, `city`, `value` (`null` for a corrupt value), `ts`, and `lat`/`lon` (8.1.1) and `attrs` (8.1.2) on a sensor's first reading and every 30th after. The `city` is the city's name, shortened to 32 characters plus an 8-hex hash when longer (CityPulse's limit). |
| Credential | `CITYPULSE_INGEST_TOKEN` (or the city's `token_env`); otherwise a service token with scope **`citypulse.ingest`**. |
| Env vars | `CITYPULSE_URL` (fleet address), `CITYPULSE_INGEST_TOKEN`, `CPSIM_OIDC_ISSUER`, `CPSIM_SERVICE_CLIENT_ID/_SECRET`. |
| Errors | 401 or 403: "CityPulse refused the token", readings stay in the retry buffer, wait 30 s. 429, 502, 503, 504: retry, honouring `Retry-After`. Other errors: retry. Backoff doubles from 1 s up to 20 s. Buffer holds 100,000 readings, then the oldest are dropped and counted. |
| Workload kind | `citypulse` is not a separate sender: a city **is** the CityPulse workload (see 8.1). |

### PagerLens (receives telemetry; monitors the simulator)

Two separate flows:

1. **Self-report** (`reporter.py`). Starts at boot when `PAGERLENS_URL` is set and a token exists (`PAGERLENS_TOKEN`, its `_FILE`, or a service token with
   scope **`pagerlens.ingest`**). Every 15 seconds it posts `up`, `request_rate` (readings delivered per second) and `error_rate` (failed delivery
   share) for the service `citypulse-simulator`, which `depends_on` `citypulse-ingest`. It registers the service and a deploy once, and raises the alert
   `SimulatorCannotReachCityPulse` (severity `sev3`) when the error rate stays at 50% or more for 45 seconds. Plain `http` to another host is refused unless `CPSIM_ALLOW_HTTP_TARGETS` is set, the same rule as a workload target (loopback is always allowed, link-local never).
   Reporting failures are printed to stderr and never affect the simulator.
2. **Workload kind `pagerlens`**: a fictional shop (`web`, `checkout`, `catalog`, `payments`, `db`, names get the prefix `sim-`) sends services,
   deploys, metrics, logs and alerts to `/api/ingest/{services,deploys,metrics,logs,alerts}`, with scenarios `healthy`, `bad_deploy`, `error_spike`,
   `latency`, `silence`. Credential: `PAGERLENS_TOKEN` or service token `pagerlens.ingest`.

Additionally PagerLens (agent or Prometheus) may scrape the simulator's `/metrics` with `CPSIM_METRICS_TOKEN` or a service token with scope
**`cpsim.metrics`** (granted by the identity provider because the simulator `exposes` it).

### TrustDesk (receives employees and requests)

Companies (and the `trustdesk` workload kind that repeats them) behave like a client application of TrustDesk (`hr_engine.py`):

| Call | Purpose |
|---|---|
| `GET /api/v1/request-types` | learn which request types the target knows (unknown ones are reported as invalid, never guessed) |
| `GET /api/v1/employees?status=active&limit=200&offset=N` | with `use_existing`, pick employees already there |
| `POST /api/v1/employees` | create the simulated employees |
| `POST /api/v1/requests` | send a request, with an `Idempotency-Key` |
| `POST /api/v1/requests/{ref}/withdraw` | withdraw a share of requests after sending |
| `GET /api/v1/requests/{ref}` | **Refresh TrustDesk statuses** |

Credential: `TRUSTDESK_CLIENT_TOKEN` (a TrustDesk token with the *Client application* scope), or a service token with scope
**`trustdesk.requests.write`**. Env: `TRUSTDESK_URL`. 429 and 503 are retried with back-off. The simulator cannot approve or reject requests (a client token cannot).

### PayBroker (receives payments)

Workload kind `paybroker`. **No identity scope is involved:** PayBroker is called with a merchant key, which is why the manifest lists it under `uses`
(address only). Calls: `POST /v1/vault/tokens` (tokenize a test card, with a publishable key if `publishable_env` is set), `POST /v1/payments/authorize`,
`POST /v1/payments/{id}/capture`, `POST /v1/payments/{id}/refund`, `POST /api/3ds/{id}/complete`. Credential: `PAYBROKER_SECRET_KEY` (a merchant secret key,
`sk_test_...`), optionally `PAYBROKER_PUBLISHABLE_KEY`. Env: `PAYBROKER_URL`. 402 (decline) and 403 (fraud rejection) are expected outcomes, counted by
class, not errors. The decline, 3-D Secure and connector-failure scenarios use the reserved card prefixes `99999901`, `99999902`, `99999903`, which need a
PayBroker that maps them (see README, "The kinds").

### LLM gateway (receives chat and embedding calls)

Workload kind `llmgw`: `POST /v1/chat/completions` and `POST /v1/embeddings` with the aliases you list (`echo-chat` and `echo-embed` on a fresh gateway).
Credential: a client key `lg_...` in `LLMGW_CLIENT_KEY`, else `LLM_API_KEY` (what `ecoctl` injects for an app with `llm: true`), else a service token with scope **`llmgw.use`**.
The gateway (`data_client` in its `api.py`) matches a service token's client id against the **name** of a registered gateway client, so the service-token route needs a client named
exactly `cpsim-service`. `ecoctl` creates a gateway client named after the app (`cpsim`) only for `llm: true` apps, with the aliases `chat-default` and `embed-default`, which the workload
then uses as its default aliases (`LLM_CHAT_MODEL`, `EMBEDDING_MODEL`). `429 budget_exceeded` and `rate_limit_exceeded` are counted as outcome classes of their
own and the loop backs off. Env: `LLMGW_URL`.

### ShopFront (receives simulated shoppers)

Workload kind `shopfront`: one item is one shopper session on `/api/v1` (browse, search, cart, checkout) in the shares of a configurable `mix`; buyers tokenize a PayBroker test card with the
publishable key the shop publishes, and pay through the shop. Credential: a service token in `SHOPFRONT_TOKEN` (scope `shop`), else scope `shopfront.api`, else guests (`params.guest` forces it;
the shop limits guests to 20 checkouts per 10 minutes per address). Env: `SHOPFRONT_URL`. Outcome classes and parameters: README, "The kinds".

### Identity provider (signs people in; issues service tokens; is probed)

| Flow | Detail |
|---|---|
| Browser sign-in | Authorization code with PKCE, state and nonce (`apgkit.auth`). Web client id `CPSIM_OIDC_CLIENT_ID` (default `cpsim`), redirect URI `<public URL>/auth/callback`. Roles come from the verified ID token (`roles` claim); only `admin`, `operator`, `viewer` count; an account with none of them is refused with a message. |
| API sign-in | Bearer access tokens are verified locally against the provider's JWKS (algorithm allow-list, issuer, audience, expiry). Scopes `cpsim.read` and `cpsim.metrics`. |
| Outbound tokens | `client_credentials` with `CPSIM_SERVICE_CLIENT_ID` (default `cpsim-service`) and `CPSIM_SERVICE_CLIENT_SECRET`, see above. |
| Probe | Workload kind `identity`: `client_credentials`, discovery (`/.well-known/openid-configuration`) and JWKS calls. At least 1 s between ticks, at most 20 calls per tick; the secret goes only to a token endpoint on the provider's own origin, tokens are discarded unread. Env: `IDENTITY_URL`. |
| Logout | Sign-out also ends the single sign-on session (`logout_url`). |

### agentpg-kit (library)

The simulator is built on `apgkit` (declared dependency `agentpg-kit>=0.2.5`; the Dockerfile installs the `v0.2.5` tag): `Database` (retrying connections),
`Vault` (envelope encryption), `LocalAuth` / `Authenticator` / `AppAuth` (admin password, API tokens, sessions, JWT verification, rate limiting),
`TokenSource` (service tokens), `SecurityGuard` (CSRF header, body size, security headers), the SQL migration runner, Prometheus metrics with the
family-wide names, and the shared UI (`/kit/*` static files). The kit's static UI components are served under `/kit`.

### ecoctl

`ecoctl` provisions the sandbox and its environment (see section 2 and section 5.3). `ecoctl env cpsim` shows the environment the sandbox is created with
(secrets masked), `ecoctl status` shows its health.

### CityPulse (the app) and AIDB

* **ShopFront:** see above: the simulator drives the shop's storefront API with the `shopfront` workload, and the shop pays through PayBroker.
* **MCP:** the simulator has no MCP server and no MCP tools (searched the source: no reference).
* **AIDB / pgvector:** none (section 14).

## 4. Architecture

```mermaid
flowchart TB
  subgraph proc[cpsim process, one asyncio event loop]
    API[FastAPI app\napi.py, static UI]
    AUTH[Authenticator\ncore.py + apgkit]
    RTM[Runtime\nruntime.py]
    subgraph city[CityRunner, one per running city]
      S[Sensors\ngenerator.py: daily curve, noise, bursts]
      D[Field devices\nprocess images, 32 channels each]
      PS[Protocol servers\nModbus, ENIP, EtherCAT, PROFIBUS,\nPROFINET sender]
      G[Edge gateway\npoll + detect changed channels]
      B[Retry buffer\n100000 readings]
      SK[HttpSink or NullSink]
    end
    HR[HrRun / company schedules\nhr_engine.py]
    WL[WorkloadRunner\nwl_runner.py + schedule.py\ndrift-free ticker]
    KD[Kinds\npaybroker llmgw pagerlens\ntrustdesk identity]
    REP[Reporter\nPagerLens self-report]
    MET[metrics.py\n/metrics]
  end
  PG[(Postgres\ncpsim_city, cpsim_company,\ncpsim_workload, cpsim_credential,\napp_setting, sessions, tokens)]
  API --> AUTH
  API --> RTM
  RTM --> city
  RTM --> HR
  RTM --> WL
  WL --> KD
  S --> D --> PS --> G --> B --> SK
  S -. direct protocol .-> B
  RTM <--> PG
  AUTH <--> PG
  REP --> RTM
  MET --> RTM
```

Key facts, from the code:

* **One process, one event loop.** Cities, company runs, workloads, the reporter and the resume task all run in the web app's loop. Numbers (counters,
  latency, bursts, company results) live **in memory** and restart with the item; definitions live in Postgres.
* **City data path** (`engine.py`): a scheduler (tick 0.25 s) updates field-device images as sensors fall due; per protocol the city hosts one server with
  devices of up to 32 channels; the gateway polls each device every `poll_interval` seconds with the protocol's own client and turns every channel whose
  update counter moved into a reading (PROFINET devices push cyclic frames instead). `direct` sensors skip devices and go straight to the buffer.
  Endpoints bind to `127.0.0.1` unless the city has `expose` set.
* **Workload loop** (`schedule.py`, `wl_runner.py`): tick *n* is due at `start + n x interval` on a monotonic clock; jitter moves a tick around the fixed
  grid; a tick that finds the previous batch running is counted as `skipped`; 429 and 503 trigger back-off (`Retry-After`, or 1 s doubling to 60 s); a refused
  credential stops the workload with the reason. `CPSIM_MAX_RATE` caps items per second for one workload and for all running workloads together.
* **State tables** (`migrations/`): `cpsim_city`, `cpsim_company`, `cpsim_workload` (name plus JSON spec, plus `desired_running`; companies also
  `desired_schedule`), `cpsim_credential` (name, vault ciphertext), and the kit's `app_setting`, sessions, API tokens and OIDC state.
* **No Postgres extensions are required.** The migrations are plain SQL; nothing uses pgvector.
* **Headless mode** (`cpsim run`, `hr-run`, `workloads run`, `cities generate`) uses an in-memory store and needs no database.

## 5. Install

Requirements: Python 3.11 or newer (the image uses 3.14), Postgres for the web app (any recent Postgres; the compose file uses `postgres:17`), Docker
for the compose route. The headless commands need neither a database nor a web server.

### 5.1 Local development with a throwaway Postgres

```bash
cd CityPulseSimulator
cp .env.example .env
# edit .env: set POSTGRES_PASSWORD (python3 -c "import secrets; print(secrets.token_urlsafe(24))")
make up                              # docker compose up -d db: postgres:17 bound to 127.0.0.1:5436, volume pgdata
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"              # needs the kit: see the note below
set -a; . ./.env; set +a             # exports DATABASE_URL and POSTGRES_PASSWORD
cpsim serve                          # applies the migrations, then listens on http://127.0.0.1:8000
```

* The first start prints a **one-time setup code** to the terminal (stderr), between the lines `=== CityPulse Simulator first-run setup ===`.
* The kit: `pyproject.toml` points `uv` at the sibling checkout `../agentpg-kit` (`[tool.uv.sources]`). With `uv sync`, the sibling checkout is used. With
  plain `pip`, install the kit first (`pip install "agentpg-kit @ https://github.com/elchinoo/agentpg-kit/archive/refs/tags/v0.2.5.tar.gz"`), because
  the project requires `agentpg-kit>=0.2.5`.
* `make down` stops the database; the volume `pgdata` keeps the data (`docker compose down -v` would delete it).
* `.env` is git-ignored; `.env.example` shows the variables. Local development only.
* To start from scratch: `docker compose down -v && make up`, or `cpsim reset-admin` to forget only the admin password.

Without Docker, any Postgres works: export `DATABASE_URL=postgresql://user:password@host:port/db` and run `cpsim serve`.

### 5.2 Docker (compose)

There are two compose files:

| File | What it runs | Postgres |
|---|---|---|
| `docker-compose.yml` | Only the `db` service (`postgres:17`, user `cpsim`, database `cpsim`, port `127.0.0.1:5436`, healthcheck `pg_isready`). `POSTGRES_PASSWORD` is mandatory. | this is the database |
| `docker-compose.live.yml` | One headless service per workload kind (profiles `paybroker`, `llmgw`, `pagerlens`, `trustdesk`, `identity`, `citypulse`, `all`), each running `cpsim workloads run /workloads/<kind>.yaml`. Files come from `./examples/live` (or `WORKLOADS_DIR`), mounted read-only. | none |

```bash
cp .env.live.example .env.live     # fill in the URLs and credentials you use; git-ignored
docker compose -f docker-compose.live.yml --env-file .env.live --profile paybroker up --build
make live PROFILE=all              # the same through make; PROFILE defaults to all
```

The web-app image: `docker build -t cpsim .` then run with `-e DATABASE_URL=...`; the default command is `cpsim serve`, bound to `0.0.0.0:8000`
(`HOST` and `PORT` are set in the Dockerfile), runs as a non-root user and has a `HEALTHCHECK` on `/healthz`. A headless container overrides the command
(`docker run --rm --env-file .env.live <image> cpsim workloads run /app/examples/live/paybroker.yaml`); the compose file disables the healthcheck for
those because no web app runs in them. The workflow `.github/workflows/build-image.yml` tests against a `pgvector/pgvector:pg17` service and publishes the image on
pushes to `main`, on `v*` tags and nightly.

### 5.3 Deploy through ecoctl

`ecoctl` (in `ecosystem/`) reads `ecosystem.yaml` and does the rest; the full walkthrough is `ecosystem/docs/DEPLOY.md` (section 10 covers running the simulators).

```bash
cd ecosystem
.venv/bin/ecoctl validate                  # check the manifest
.venv/bin/ecoctl plan                      # every command and API call; changes nothing
.venv/bin/ecoctl apply --only cpsim --execute   # pulls in its callees (citypulse, trustdesk, pagerlens, llmgw) and what it `uses` (paybroker, identity)
.venv/bin/ecoctl env cpsim                 # what the sandbox is created with (secrets masked)
.venv/bin/ecoctl status                    # health of every app
.venv/bin/ecoctl recreate cpsim            # new sandbox, same database; run once after a callee's address changed
```

What the sandbox receives (`ecoctl env cpsim`, from `plan.py`): `CPSIM_ENCRYPTION_KEY` (generated), `CPSIM_OIDC_ISSUER`, `CPSIM_OIDC_CLIENT_ID`
(`cpsim`), `CPSIM_OIDC_CLIENT_SECRET`, `CPSIM_SERVICE_CLIENT_ID` (`cpsim-service`), `CPSIM_SERVICE_CLIENT_SECRET`, and the fleet URLs. `DATABASE_URL` comes from
the linked instance. Never hardcode URLs: they change when an app is recreated, and the simulator follows through fleet references (see 8.5).

Manual AgentPG deployment (without ecoctl), from the README:

```bash
agentpg login
agentpg create --name cpsim-db --size small
agentpg sandbox create cpsim --image ghcr.io/<owner>/citypulsesimulator:latest --instance <instance-id> --port 8000 \
  --env CITYPULSE_INGEST_TOKEN=<ingest token> --env CPSIM_METRICS_TOKEN=<random string>
```

A sandbox's environment can only be set when it is created; to change it, recreate the sandbox (the database, and so every city, company and workload, stays).

## 6. Configuration reference

All variables are read in code unless noted. "Secret" variables also accept `<NAME>_FILE` (path to a file whose first line is the value); some have a
default file path.

### Core

| Variable | Default | Meaning |
|---|---|---|
| `DATABASE_URL`, else `DB_EXTERNAL_DSN`, else `PSQL_CONNECTION_STRING` | none (first non-empty wins) | The Postgres DSN. **Required** for `cpsim serve`, `cpsim migrate`, `reset-admin`, `import-files`. An empty value falls back to libpq's `PG*` variables (linked AgentPG sandboxes provide them). |
| `HOST` | `127.0.0.1` (the image sets `0.0.0.0`) | Address `cpsim serve` binds to. |
| `PORT` | `8000` | Port of `cpsim serve`. The image's healthcheck reads it. |
| `LOG_LEVEL` | `info` | uvicorn log level. |
| `CPSIM_PUBLIC_URL` | derived from the request | The URL the app is reached at; builds the OIDC callback (`<it>/auth/callback`). Set it when behind a proxy that hides the real address. |
| `POSTGRES_PASSWORD` | none | Only used by `docker-compose.yml` and `.env.example` for the local database. |

### Sign-in and tokens

| Variable | Default | Meaning |
|---|---|---|
| `CPSIM_OIDC_ISSUER` | unset (federation off) | The identity provider URL. Setting it turns single sign-on and JWT verification on. |
| `CPSIM_OIDC_CLIENT_ID` | `cpsim` | The web client id (also the expected audience of access tokens). |
| `CPSIM_OIDC_CLIENT_SECRET[_FILE]` | none | The web client's secret. |
| `CPSIM_LOCAL_LOGIN` | true | `false` removes the admin-password login when SSO is on. Truthy values: `1`, `true`, `yes`, `on`. |
| `CPSIM_API_KEY[_FILE]` | none (default file `/etc/cpsim/api_key`) | Static **full-access** key for automation. Also accepted at the sign-in form. |
| `CPSIM_METRICS_TOKEN[_FILE]` | none (default file `/etc/cpsim/metrics_token`) | Static token that can scrape `/metrics` and nothing else. |
| `CPSIM_ALLOW_ANONYMOUS` | false | Demo only: disables authentication entirely. |
| `CPSIM_ENCRYPTION_KEY[_FILE]` | none (default file `/etc/cpsim/encryption_key`) | Platform secret that unlocks the credential store at start (creating the vault on first use). Without it nothing can be saved under Settings > Credentials until an administrator signs in with the password. |

### Outbound (service client and fleet)

| Variable | Default | Meaning |
|---|---|---|
| `CPSIM_SERVICE_CLIENT_ID` | `cpsim-service` | Identity `service` client used to mint outbound tokens and for the identity probe. |
| `CPSIM_SERVICE_CLIENT_SECRET[_FILE]` | none | Its secret. Setting it (together with `CPSIM_OIDC_ISSUER`) turns minted tokens on. |
| `CITYPULSE_URL`, `PAYBROKER_URL`, `TRUSTDESK_URL`, `PAGERLENS_URL`, `LLMGW_URL`, `IDENTITY_URL`, `SHOPFRONT_URL` | unset | The seven fleet addresses. Only these names are valid in a `${NAME}` target (`fleet.py`). `PAGERLENS_URL` also switches on the self-report. |
| `CITYPULSE_INGEST_TOKEN` | none | Default ingest token variable of a city. |
| `TRUSTDESK_CLIENT_TOKEN` | none | Default token variable of a company. |
| `PAYBROKER_SECRET_KEY`, `PAYBROKER_PUBLISHABLE_KEY` | none | PayBroker merchant keys for `paybroker` workloads. |
| `LLMGW_CLIENT_KEY`, `LLM_API_KEY` | none | Gateway client key (`lg_...`); `LLM_API_KEY` is what ecoctl injects for `llm: true` and is tried second. `LLM_CHAT_MODEL` / `EMBEDDING_MODEL` (also injected) become the default aliases. |
| `SHOPFRONT_TOKEN` | none | ShopFront service token (scope `shop`) for `shopfront` workloads; none means guest shoppers. |
| `PAGERLENS_TOKEN[_FILE]` | none (default file `/etc/cpsim/pagerlens_token`) | PagerLens ingest token (self-report and `pagerlens` workloads). |
| *(any name a city, company or workload gives in `token_env` / `publishable_env`)* | | A city or company name must contain `TOKEN`; a workload name must contain `TOKEN`, `KEY` or `SECRET`. A workload name may not start with `CPSIM_`, `DATABASE`, `DB_`, `PSQL`, `PG`, `POSTGRES`, `AGENTPG`, may not be `HOME` or `PATH`, and may not end in `_FILE`. |

### Safety and run behaviour

| Variable | Default | Meaning |
|---|---|---|
| `CPSIM_MAX_RATE` | `200` | Ceiling on items per second, per workload and for all running workloads together. A workload above it is refused when saved; starting one that would push the total above it is refused. (Company runs are limited separately: at most 200 requests per second per company.) |
| `CPSIM_ALLOW_HTTP_TARGETS` | off | `1`, `true` or `yes` allows plain `http` to hosts other than this machine **for workloads**, for local stand-ins reached by name. |
| `CPSIM_TARGET_ALLOWLIST` | Extra hostnames a target may have (comma or space separated; `.domain` matches the domain and its subdomains). The hosts of the ecosystem URLs given by environment (`CITYPULSE_URL`, `TRUSTDESK_URL`, `PAYBROKER_URL`, `SHOPFRONT_URL`, `PAGERLENS_URL`, `LLMGW_URL`, `IDENTITY_URL`) are always allowed; any other host is refused when something starts. |
| `CPSIM_ALLOW_PRIVATE_URLS` | `true` lets the NetGuard accept private and loopback addresses (local stand-ins). Link-local is never accepted. Default: refused. |
| `CPSIM_TOKEN_ENV_ALLOWLIST` | Extra variable names a `token_env` may name, on top of the kinds' default variables. `LLM_API_KEY` and `*_ENCRYPTION_KEY` can never be added. |
| `CPSIM_FORWARDED_ALLOW_IPS` | Peers whose `X-Forwarded-*` headers uvicorn believes. Default `127.0.0.1`; behind AgentPG's single proxy set that proxy's address. Never `*`. |
| `CPSIM_POSITIONS` | on | `0`, `false`, `no` or `off` stops every city sending `lat`/`lon` (8.1.1); the per-city `positions` setting is the finer switch. |
| `CPSIM_ATTRS` | on | `0`, `false`, `no` or `off` stops every city sending sensor `attrs` (8.1.2); the per-city `attrs` setting is the finer switch. |
| `CPSIM_RESUME_ON_START` | on | Default of the "Resume what was running when the app restarts" switch (off for `0`, `false`, `no`, `off`). The switch under Settings overrides it. |
| `CPSIM_RESUME_STAGGER_SECONDS` | `1` | Seconds between two resumed items; `0` to `60`; invalid values fall back to 1. |
| `CPSIM_DATA_DIR` | ignored | Left over from the file store that is gone. Use `cpsim import-files <dir>` once to load old data. |

Docker-compose-only: `WORKLOADS_DIR` (folder of workload files for `docker-compose.live.yml`, default `./examples/live`).

Hard limits in code: request bodies 512 KB; a city at most 10,000 sensors, a group at most 5,000, at most 100 groups; city poll interval 0.5 to 60 s;
group frequency 1 to 3600 s; workload interval 0.2 to 3600 s, batch up to 500, concurrency up to 64; a company up to 2,000 new employees, 5,000 requests per
type and 20,000 per run, rate 0.1 to 200 per second, up to 16 connections.

## 7. First-run walkthrough

1. **Start** the app (section 5). Read the setup code from the log (`=== CityPulse Simulator first-run setup ===`). It lives in memory and changes on every restart until setup is
   complete. With SSO on (`CPSIM_OIDC_ISSUER` set) no setup code is printed; people sign in through the provider.
2. **Open** `http://127.0.0.1:8000`. Enter the setup code and choose an admin password (12 characters or more). You are signed in; the session lasts 12 hours.
3. **Cities > Fixed presets**: type a name (`demo-city`), press **Fixed demo city (always the same)** (174 sensors over all five protocols and `direct`). The city page opens.
4. **Start city.** Without a CityPulse URL the city only generates data. The stats panel appears: readings sent, send rate, field-device endpoints (host:port per protocol), polls ok,
   and the active bursts.
5. Press **BURST** (the whole city, 1, 3 or 5 minutes) and watch *Bursts active* and *Corrupt values* rise.
6. **Workloads**: name `shop-telemetry`, kind *PagerLens telemetry*, **Create**, leave the URL empty (generate only), **Save and start**. The *Outcomes* table shows `generated`.
7. **Settings**: see the **Fleet** card (which of the seven variables are set), **Credentials**, **After a restart**, **API tokens**, **Admin password**.
8. To send somewhere real, set the target URL (or **Use the fleet's CityPulse**) and make sure the token variable is set (or minted, or saved).

Headless with no database at all:

```bash
cpsim validate examples/brussels.yaml && cpsim run examples/brussels.yaml --seconds 30
cpsim workloads run examples/live/pagerlens.yaml --dry-run --seconds 20
```

## 8. Using it, feature by feature

The web app has five tabs: **Cities**, **Companies**, **Workloads**, **Protocols**, **Settings**. The UI hides controls a role cannot use (a viewer sees them disabled);
the server enforces the same rules.

### 8.1 Cities

* **Generate demo cities**: how many (1 to 50), size (`small` about 200 sensors, `medium` about 2,000, `large` about 20,000 clamped to 10,000, or a number from 8 to 10,000), profile
  (`balanced`, `industrial`, `coastal`, `tourist`, `residential`, `mixed`), optional seed and name. Same seed, size and profile give the same city; city *n* of a batch uses seed + *n*.
  Needs `sim:edit`. Never replaces an existing city. Headless: `cpsim cities generate --count 10 --size medium --profile industrial --seed 42 --out cities/`.
* **Fixed presets**: *Fixed demo city*, *Fixed live feed every 2 s* and *every 5 s* (60 sensors, all `direct`, reading interval and poll interval both set to 2 or 5 seconds). These are the
  CityPulse "workload": a city streams to CityPulse on its own schedule (poll interval of the gateway and each group's frequency). In a headless file `kind: citypulse` expands to the same city.
* **City page**: *Start city* / *Stop city*, *Burst the whole city for* 1, 3 or 5 minutes (**BURST**; multiplies every sensor's burst probability by 25), stats panel (refreshes every 2 s while running),
  *Sensor groups* table with **Remove**, **Add a sensor group** (type, count, district, protocol, range, frequency, burst percentage), **City settings**.
* **City settings**: CityPulse URL (or **Use the fleet's CityPulse**), token variable name, random seed, gateway poll interval, *Let other machines reach the protocol endpoints*, *Start this city when the app starts*.
  A running city is locked; stop it first (the API answers 409 otherwise).
* **Behaviour of a sensor** (`generator.py`): daily curve inside the group's range with noise; with probability `burst_pct / 100 x 0.04` per reading a sensor enters a burst episode of 3 to 20 readings
  (dropout 20 to 120 seconds): `spike`, `noise`, `stuck`, `dropout`, `flood` (five times as often) or `corrupt` (sent as `null`). A city seed makes values and bursts reproducible.
* **Districts**: ids of the form `[a-z0-9][a-z0-9_-]{0,23}`; the generator makes up names (`alder-quay`) and gives each district a kind with sensor affinities.
* **Delete city** asks you to type the city's name.

### 8.1.1 Positions on the map

Every sensor has a position (`src/cpsim/geo.py`), sent to CityPulse as `lat` and `lon` (WGS 84 decimal degrees, 6 decimals) so its map, `/api/sensors/nearby`, the GeoJSON endpoints and the geographic part of district explain have data.

* **Real cities: Brussels and Vienna.** A city named `brussels` (19 communes) or `vienna` (23 districts) is placed on its real geography (`src/cpsim/realcities.py`, data in `src/cpsim/geodata/`: byte-for-byte copies of CityPulse's files, refreshed by `scripts/sync_geodata.py`; sources and licences are in `ATTRIBUTION.md` there and in CityPulse's guide 7.8). A district id that is a real one (`ixelles`, `donaustadt`, ...) is that polygon; any other id (`d01` of a hand-made or preset city) stands in for one real district chosen by a hash of the id, the same one every run. A sensor is placed **inside the polygon of its district** by a pure function of the city name and the sensor id: 45 % beside a main road (a point on a road segment inside the district, up to 20 m off the line), the rest anywhere in the district, and every candidate point is kept with a probability that falls from 1 at the city centre to 0.25 at the edge (Gaussian, scale 0.35 of the city's half diagonal), so the centre is denser; never in a lake or river polygon (a point that fails is redrawn, up to 40 times, then the district's most central grid point is used). CityPulse stores the same polygons (`source = 'boundary'`), so `ST_Contains` agrees with the id a sensor reports. **The generator** (`cpsim cities generate`, the *Generate* button, `POST /api/cities/generate`) names new cities after the real ones first (`brussels`, then `vienna`, skipping names that are taken) and only then falls back to invented names; a real city has its real districts (all of them, or a seeded handful of 3 to 7 for a small one), each with a kind that says what the district is like (Ixelles and Etterbeek campus, Anderlecht industrial, the Forêt de Soignes communes green, Innere Stadt historic...), which decides its sensor types. The library function defaults to invented names (`real=False`), so older callers and tests are unchanged.
* **Other cities** (invented names, or cities saved by older versions such as `gotham`, which keeps its New York anchor) work as before: a built-in table anchors known names (case-insensitive, dashes or spaces): `gotham` (New York), `metropolis` (Chicago), london, paris, berlin, madrid, lisbon, rome, tokyo, sydney, new york, chicago, san francisco, boston, toronto, sao paulo, mexico city, mumbai, singapore, cape town, dubai, amsterdam, brussels and vienna. Any other name is placed at a deterministic spot derived from a hash of the name (latitude -50 to 58, longitude -165 to 165: no poles, no antimeridian). Positions are real WGS 84, so `ST_DWithin` distances are true metres. CityPulse's map is plain SVG drawn from GeoJSON (no basemap tiles); such cities get derived outlines.
* **Districts of other cities** take 1.5 km cells on a square spiral around the origin, ordered by kind (read from the generated name: `alder-quay` is a harbour): centres and old towns near the origin, then markets, campuses, housing, parks, rivers, harbours, industry, suburbs, airports, farmland outward; hand-made ids such as `d01` count as housing. A sensor sits at a hash-of-its-id spot inside its district's cell (10 % margin). Nothing depends on the seed or the clock, so a restart or recreate gives the same map; adding a district can move the districts that sort after it.
* **When it is sent.** CityPulse keeps the last position it received (`coalesce`), so a sensor sends it on its first reading and every 30th after that (survives a CityPulse database reset, about 1 % extra payload).
* **Turning it off.** City setting `positions` (default `true`; in a spec file `positions: false`) or, for the whole app, `CPSIM_POSITIONS=off`. CityPulse ignores unknown fields on a reading, so an older version without positions still accepts them; it simply does not store them.
* **Limits.** Only the HTTP path to CityPulse carries coordinates. The simulator has no OTLP sender; the field-protocol hops (Modbus, EtherNet/IP, EtherCAT, PROFINET, PROFIBUS) carry values only, positions are attached by the gateway when it builds the reading.

### 8.1.2 Sensor attributes

Every sensor has device metadata (`src/cpsim/attrs.py`), sent to CityPulse as `attrs` so its sensor filters (`attrs`, `attrs_path`), the JSONB sensor detail and the "vendor or firmware stands out" part of district explain have data.

* **Shape.** `vendor`, `firmware`, `install_date`, `calibration` (`offset`, `scale`, `date`) and `tags` (one to three), under 200 bytes (a test caps it at 400): well inside what CityPulse accepts (two levels, keys `[a-z][a-z0-9_]*`, 1024 bytes). Vendors are made-up names, three or four per sensor type; install dates fall in a fixed five-year window ending 2026-10-01 (no clock, so nothing drifts); calibration offset is within +-0.2 and scale within 1 +- 0.01.
* **Deterministic.** A pure function of the sensor id, type and city (sha256), independent of the seed and the clock: the same sensor keeps its metadata across restarts and recreates.
* **Synthetic sample behaviour: one firmware faults more.** About 8 % of the sensors run the old firmware `1.4.2` (every vendor has it; the newer versions differ per vendor). The generator makes those sensors start fault episodes three times as often, so "firmware 1.4.2 stands out among the problem sensors" is really true in the data CityPulse explains. Real devices have no such rule; it exists to give the metadata something to find. It is a property of the sensor, so it also holds when attrs are not sent, and it raises the city's overall burst rate by about 16 %; other sensors are unchanged.
* **When it is sent.** On a sensor's first reading and every 30th after that, together with the position (CityPulse keeps the last attrs it received and never overwrites them with null), about 6 bytes per reading on average.
* **Turning it off.** City setting `attrs` (default `true`; in a spec file `attrs: false`; validated like `positions`, no UI checkbox) or, for the whole app, `CPSIM_ATTRS=off`. It does not affect positions.

### 8.2 Protocols

The **Protocols** tab explains each protocol and shows a pymodbus snippet to read a running city's Modbus endpoint (port from the city page). Honest summary (README): Modbus TCP is read by a third-party client
(pymodbus) in the tests; EtherNet/IP, EtherCAT, PROFINET and PROFIBUS are implemented as far as the project needs and **not** certified or tested against real PLCs. PROFINET travels over a UDP tunnel and
PROFIBUS over a TCP stream because the raw transports need privileges or hardware. One endpoint per protocol per city; a protocol's address space caps its devices (`DEVICE_LIMITS`: Modbus 247, EtherNet/IP 1000,
EtherCAT 4096, PROFINET 4096, PROFIBUS 125, each device up to 32 sensors).

### 8.3 Companies (TrustDesk)

* **New company**: a name plus *Create empty* or one of four presets: *Quick check* (about 60 requests), *Hundreds of mixed requests* (about 500), *Leave stress*, *Expense audit*.
* **Company page**: TrustDesk URL (or **Use the fleet's TrustDesk**), token variable, new hires (last about 75 days) and tenured staff (1 to 15 years), employee prefix, a count for each of the 19 request types,
  *Problem requests (%)* (default 25), *Withdrawn after sending (%)* (default 3), seed, requests per second, parallel connections.
* **Preview** shows what a run would send (decided from the seed, nothing sent; needs only `sim:read`). **Save and run** starts a run; progress is per type. **Refresh TrustDesk statuses** reads back where requests stand.
  An empty URL means generate only.
* **Schedule**: tick *Run this company again and again* (every 5 to 86400 s, jitter, duration, runs, start on boot), then **Start schedule** / **Stop schedule**. It uses the same drift-free loop and appears in
  `/metrics` as `company/<name>`.
* Results of a run live in memory and are lost on restart. A run ends `done` or `failed` (for example a refused token).
* Headless: `cpsim hr-validate FILE`, `cpsim hr-run FILE [--dry-run]`.

### 8.4 Workloads

Create (name, kind), then on the workload page set the target, the credential variable name, the schedule and the kind's parameters. **Save and start**, **Run once** (one batch now, then stop) and **Stop**.
Presets fill interval, batch and parameters. Counters per workload: ticks, skipped ticks (and how many were backoff), items, outcomes by class, latency p50/p95, last errors, time to the next tick.

| Kind | Target | Credential variable (default) | Interval limits |
|---|---|---|---|
| `paybroker` | PayBroker | `PAYBROKER_SECRET_KEY` (optional `publishable_env`) | 0.2 s, batch up to 500 |
| `llmgw` | LLM gateway | `LLMGW_CLIENT_KEY`, else `LLM_API_KEY`, else service token `llmgw.use` | 0.2 s |
| `shopfront` | ShopFront `/api/v1` | `SHOPFRONT_TOKEN`, else service token `shopfront.api`, else guest | 0.2 s |
| `pagerlens` | PagerLens | `PAGERLENS_TOKEN`, else service token `pagerlens.ingest` | 0.2 s |
| `trustdesk` | via a company's own URL | the company's variable, else `trustdesk.requests.write` | 5 s, batch 1 |
| `identity` | identity provider | the service client (`CPSIM_SERVICE_CLIENT_*`) | 1 s, batch up to 20 |
| `citypulse` | (cities, see 8.1) | the city's variable | file form only |

Parameters of every kind are in the README ("The kinds"). The **Switch scenario** control on a running `pagerlens` workload changes `healthy`, `bad_deploy`, `error_spike`, `latency`, `silence` live
(aimed at `target_service`, default `checkout`); a failure raises one alert after `alert_after` ticks (default 3) so an incident opens. An empty target URL means generate only: items are counted as `generated`.

Schedule fields: `interval`, `jitter_pct` (0 to 50), `batch`, `concurrency`, `duration` / `total` / `ticks` (stop after), `start_on_boot`.

Headless: `cpsim workloads validate FILE`, `cpsim workloads run FILE [--only NAME] [--seconds N] [--dry-run] [--status-every N]`. Exit codes: **0** normal end, Ctrl-C or SIGTERM; **2** misconfiguration
(invalid file, unresolved or empty URL without `--dry-run`, missing credential variable, rate above the ceiling); **3** a target refused the credential. One status line per workload every 5 seconds.

### 8.5 Fleet references

A target may be exactly `${CITYPULSE_URL}`, `${PAYBROKER_URL}`, `${TRUSTDESK_URL}`, `${PAGERLENS_URL}`, `${LLMGW_URL}` or `${IDENTITY_URL}` (nothing before or after, no other name, no defaults, no nesting;
otherwise 422). It is stored as written and resolved from the environment each time something **starts**, so a recreated app is followed without an edit. Unset: start fails with
`<NAME> is not set on this app; set the URL by hand or deploy it with ecoctl`. The resolved address must still pass the URL rules. The UI shows `${CITYPULSE_URL} -> https://...`, has a **Use the fleet's ...**
button next to each Target URL, and Settings has a read-only **Fleet** card (`GET /api/fleet`). Generated cities, the fixed presets and the live-feed presets default to `${CITYPULSE_URL}` when that variable is set.

### 8.6 Resuming after a restart

Starting a city, workload, company run or company schedule stores `desired_running` (companies also `desired_schedule`); **Stop** clears it; the app going down does not. At startup the app first starts items marked
*start on boot*, then resumes every other flagged item one per `CPSIM_RESUME_STAGGER_SECONDS`, in the background (the UI is available meanwhile). Order: cities, workloads, company schedules, company runs.
A failure prints `cpsim: cannot resume ...` and leaves the flag set; a run that ended by itself is not started again. **Settings > After a restart** has the switch (stored in the database, on by default).

### 8.7 Settings

* **Fleet**: which of the seven addresses are set.
* **Credentials for the apps it drives**: paste a credential once per variable name; stored encrypted, never shown again, replace or delete. Needs `settings:manage`.
* **After a restart**: the resume switch.
* **API tokens**: create (name, scope `read`, `metrics`, `full`), shown once, revoke. Prefix `cs_`.
* **Admin password**: set or change (signs every browser out).

### 8.8 MCP tools

None. The simulator exposes no MCP server.

### 8.9 Database insights (Database tab)

The **Database** tab (`GET /api/insights?order=total|mean|calls&limit=N`, permission `settings:manage`, i.e. admin only: statement text shows table names and the shape of the workload) mounts the
kit's `initInsights` panel for the simulator's own AgentPG database: slowest and most called statements (`edb_stat_monitor`, `pg_stat_statements` as a fallback), `edb_pg_tuner` recommendations and
`query_advisor` index hints. Where an extension or a grant is missing (plain Postgres) each part says "Not available on this database." with the reason, and the route still answers `200`.
`/metrics` also exports `db_insights_available{app="cpsim"}`, `db_insights_top_query_mean_seconds{app,queryid}`, `db_insights_statement_calls` and `db_insights_tuner_recommendations` (cached 30 s).
Not verified here: the EDB extensions are not installed on the local test Postgres, so only the degraded path ran (route, metrics, a fake-DOM test of both renderings); the populated path is covered by the kit's tests.

### 8.10 What a read-only database user and AgentPG's restricted MCP can see

`src/cpsim/hardening.py` runs at the end of `migrate.run()` (`cpsim migrate` and the migration step of `cpsim serve`) and uses the kit's `apgkit.masking` conventions on the group role `a1p_readonly`
(members: users from `agentpg db-users create --permission read`; the restricted MCP server of `agentpg mcp serve` runs as such a user). It does nothing where the role does not exist.

| Table | A read-only user gets |
|---|---|
| `cpsim_credential` (Fernet ciphertext of the credentials saved for the apps it drives) | `permission denied` |
| `app_setting` (settings, vault store: wrapped data key, admin password hash), `web_session`, `api_token`, `oidc_login` | `permission denied` |
| `cpsim_city`, `cpsim_company`, `cpsim_workload` | readable. They hold synthetic specs only; a workload names the environment variable that holds its credential (`token_env`), never the credential. There is no personal data, hence no masked view. |

Limits: only the table owner can REVOKE (checked, with a logged warning if it did not take); default privileges give `a1p_readonly` SELECT on every NEW table, so a table added by a later migration is readable
until it is added to `DENIED_TABLES` (a test fails when a column named like a hash, secret, token, cipher or password is readable). The app's own owner connection, `a1p_readwrite` members and the hosted MCP
endpoint reached with `agentpg token` (a read-write session, see `ecosystem/docs/06`) are not restricted.

Masking durability: grants made after startup reopen tables (`agentpg db-users create` and AgentPG's default privileges can give `a1p_readonly` SELECT on a base table again at any time). The app therefore re-asserts the masking every 5 minutes (`masking.reassert_forever`, started in the lifespan, running `hardening.apply` on its own connection), the Prometheus gauge `masking_drift_tables` counts the denied tables that were readable before the last re-assert (alert on `> 0`), `masking_last_ok_timestamp` shows the last success, and `/readyz` reports `masking` (`last_ok`, `drift`, `ok`; report only, it never makes readiness fail). A grant made just after a tick stays open until the next one (at most 5 minutes).

## 9. HTTP API reference

Interface is JSON over HTTP; there is no OpenAPI page (`docs_url`, `redoc_url`, `openapi_url` are disabled). Authenticate with `Authorization: Bearer <API key | cs_ token | service access token>`
(a browser uses a session cookie and must also send the header `X-CPSim-CSRF` on writes).

| Method and path | Permission | Purpose |
|---|---|---|
| `GET /healthz` | public | `{"status":"ok","version","cities_running"}` |
| `GET /readyz` | public | `{"status":"ready","masking":{...}}`, or 503 `waking-database` when Postgres does not answer |
| `GET /metrics` | `metrics:scrape` (cookies refused) | Prometheus text |
| `GET /api/session`, `POST /api/setup`, `POST /api/login`, `POST /api/logout`, `GET /auth/login`, `GET /auth/callback` | public | sign-in state and flows |
| `POST /api/admin-password`, `GET/POST/DELETE /api/tokens`, `GET/PUT/DELETE /api/credentials[/name]`, `GET/PUT /api/run-settings` | `settings:manage` | administration |
| `GET /api/insights?order=&limit=` | `settings:manage` | EDB query statistics, tuner and advisor for the app's own database ([8.9](#89-database-insights-database-tab)) |
| `GET /api/fleet`, `GET /api/catalog`, `GET /api/hr/catalog`, `GET /api/workloads/catalog` | `sim:read` | catalogs |
| `GET /api/cities`, `GET /api/cities/{n}`, `GET /api/cities/{n}/stats` | `sim:read` | |
| `POST /api/cities/generate`, `PUT /api/cities/{n}`, `DELETE /api/cities/{n}` | `sim:edit` | create/replace/delete (a running city: 409) |
| `POST /api/cities/{n}/start`, `/stop`, `/burst` (`{"seconds": 5..1800}`) | `sim:run` | |
| `GET /api/companies[/n]`, `POST /api/companies/{n}/preview`, `GET /api/companies/{n}/status` | `sim:read` | |
| `PUT/DELETE /api/companies/{n}` | `sim:edit` | |
| `POST /api/companies/{n}/run`, `/stop`, `/refresh`, `/schedule/start`, `/schedule/stop` | `sim:run` | |
| `GET /api/workloads[/n]`, `GET /api/workloads/{n}/status` | `sim:read` | |
| `PUT/DELETE /api/workloads/{n}` | `sim:edit` | |
| `POST /api/workloads/{n}/start`, `/stop`, `/run-once`, `/scenario` (`{"scenario": "bad_deploy"}`) | `sim:run` | |

Names of cities, companies and workloads: lowercase letters, digits and dashes, 1 to 40 characters. A GET of a city, company or workload includes a read-only `target` object
(`reference`, `resolved_url`, `resolves`, `problem`); a PUT ignores it, so a GET can be sent back.

## 10. Roles and permissions

Defined in `core.py`. Permissions are what the code checks; roles (people, from the identity provider) and scopes (services, API tokens, the static key) map onto them.

| Permission | Allows |
|---|---|
| `sim:read` | see cities, companies, workloads, catalogs, previews, statistics and run results |
| `sim:run` | start, stop and boost cities; run, stop and refresh companies; start, stop and run workloads once |
| `sim:edit` | create, change and delete cities, companies and workloads |
| `settings:manage` | admin password, API tokens, saved credentials, the resume switch |
| `metrics:scrape` | `GET /metrics` |

| Role | Permissions |
|---|---|
| `admin` | all (`*`) |
| `operator` | `sim:read`, `sim:run`, `sim:edit` (not settings, not metrics) |
| `viewer` | `sim:read` |

| Scope or token | Permissions |
|---|---|
| API token `read` / identity scope `cpsim.read` | `sim:read` |
| API token `metrics` / identity scope `cpsim.metrics` / `CPSIM_METRICS_TOKEN` | `metrics:scrape` only |
| API token `full` / `CPSIM_API_KEY` | all |

A person authenticated by the identity provider with none of these roles can do nothing (and is refused at sign-in). Role changes reach an existing session at its next sign-in.
In the ecosystem, the `ecosystem-admins` access group is granted `admin` on every app (`access.groups` in the manifest); other groups in the manifest do not mention the simulator.

## 11. Security notes

The full policy is [SECURITY.md](../SECURITY.md). The points an operator needs:

* **Fail closed.** Without a credential every protected route is denied. `CPSIM_ALLOW_ANONYMOUS=true` is an explicit demo-only opt-out; never use it on a reachable address.
* **First-run setup code** is printed in the log only; whoever presents it sets the admin password (scrypt hash, 12-character minimum).
* **Sessions**: random cookies, `HttpOnly`, `SameSite=Strict` (local login) or `Lax` (after SSO), `Secure` over HTTPS, stored as SHA-256, 12 hours. Browser writes need a CSRF header.
* **Failed sign-ins** are rate limited: 10 per minute per client address, **in memory, per process**.
* **Secrets are never stored in a city, company or workload**: only the variable **name** (validated). Saved credentials are encrypted with the vault (data key wrapped under `CPSIM_ENCRYPTION_KEY` and the admin
  password); the database alone cannot decrypt them; no endpoint returns them.
* **Outbound requests**: URL rules (https, or http for this machine; workloads also `CPSIM_ALLOW_HTTP_TARGETS`; no credentials, query or fragment; no link-local), no redirects, a credential only to its own target.
  Targets are chosen by the operator, so there is no further SSRF restriction.
* **Residual risk**: a person with `sim:edit` and `sim:run` (operator) can make the simulator send any non-denylisted environment secret of its process to a URL they choose. An administrator can do the same with a
  saved credential. Give the sandbox only narrow, revocable credentials, and the operator role only to people you trust with them. `--env` on AgentPG is not a secret store.
* **Industrial protocol endpoints have no authentication** (the protocols have none). They bind to loopback by default; *Let other machines reach the protocol endpoints* binds all interfaces, per city. They are read-only,
  size-bounded and survive malformed input; never expose them to an untrusted network.
* **Container**: non-root, no secrets baked in, strict Content-Security-Policy (no inline scripts or styles), `X-Frame-Options: DENY`, `nosniff`, `no-referrer`.
* **A leaked ingest token** lets someone push readings to CityPulse (bounded by its validation) until revoked; use one token per simulator.
* Report vulnerabilities through GitHub private vulnerability reporting, not a public issue.

## 12. Operations

### Health and observability

| Endpoint or signal | Use |
|---|---|
| `GET /healthz` | liveness; also the container `HEALTHCHECK` |
| `GET /readyz` | readiness; 503 `waking-database` while Postgres is asleep (AgentPG scales to zero) |
| `GET /metrics` | Prometheus: kit family (`http_requests_total`, `http_request_duration_seconds`, `db_up`, `app_info`, `process_*`), `db_insights_*` (EDB query statistics, [8.9](#89-database-insights-database-tab)) plus `cpsim_*` |
| PagerLens self-report | service `citypulse-simulator`; alert `SimulatorCannotReachCityPulse` |
| Terminal / sandbox log | `cpsim: ...` lines: setup code, `cannot autostart`, `cannot resume`, `reporter: ...`, `cpsim[<city>]: loop crashed` |

`cpsim_*` series: `cpsim_sensors`, `cpsim_buffer_readings`, `cpsim_devices_unreachable` (per city), `cpsim_cities_running`, `cpsim_readings_sent_total`, `cpsim_readings_dropped_total`,
`cpsim_send_failures_total` (per city), `cpsim_polls_total{city,protocol,result}`, `cpsim_hr_runs_running`, `cpsim_hr_requests{company,type,outcome}`, `cpsim_hr_employees{company,outcome}`,
`cpsim_workloads_running`, `cpsim_workload_ticks_total`, `cpsim_workload_skipped_ticks_total`, `cpsim_workload_items_total{workload,kind,outcome}`, `cpsim_workload_latency_seconds{workload,kind,quantile}`.
Workload, tick and company counters restart when the item restarts. Scrape with `Authorization: Bearer <CPSIM_METRICS_TOKEN>` (a browser session cannot scrape).

### Backup and restore

All durable state is in Postgres: `cpsim_city`, `cpsim_company`, `cpsim_workload`, `cpsim_credential` (ciphertext), `app_setting` (admin hash, vault wrapping, resume switch), and the kit's session, token and OIDC tables.
Back up the database (`pg_dump`, or the AgentPG instance's own backup). Restoring it onto another instance restores definitions and the admin password. The credential store is readable only with the same
`CPSIM_ENCRYPTION_KEY` (or the admin password): **keep the key** (ecoctl keeps it in `.ecosystem/state.json`). Counters, run results and running state are not backed up (running state is rebuilt from the `desired_running` flags).
The `cpsim-service` client secret and environment variables are not in the database; they come from the sandbox environment.

### Rotation

| Secret | How |
|---|---|
| Admin password | Settings > Admin password (signs everyone out), or `cpsim reset-admin` then restart and use the new setup code. **`reset-admin` also resets the vault**, so saved credentials could never be read again: the command names them, says they will be deleted, and asks you to type `delete credentials` (`--yes` skips the question; nothing is asked when none is saved). Save them again afterwards. Prefer the change-password route. |
| API token | Settings > API tokens: create a new one, then revoke the old. |
| `CPSIM_API_KEY`, `CPSIM_METRICS_TOKEN`, `CPSIM_ENCRYPTION_KEY`, OIDC and service client secrets, ingest and merchant keys | Environment variables of the sandbox: change them and **recreate the sandbox** (database stays). With ecoctl: `ecoctl apply --rotate` rotates identity client secrets (see DEPLOY.md section 9). Rotating `CPSIM_ENCRYPTION_KEY` alone is not covered by any command in this repository: see Known limitations. |
| A saved credential | Settings > Credentials: paste the new value (replaces), or delete. |

### Vault

The vault wraps one random data key under the admin password and, when `CPSIM_ENCRYPTION_KEY` is set, under that platform secret too. After a restart the store is **locked** unless the platform secret is
present; then Settings shows a note ("the credential store is locked") and signing in with the admin password unlocks it for this session. Items that cannot be decrypted are skipped, not fatal.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `database unavailable after N attempts`, or UI 503 | Postgres asleep or unreachable | Wait and retry (`/readyz` shows `waking-database`); check `DATABASE_URL`; resume the AgentPG sandbox/instance |
| No setup code in the log | Setup already complete, or SSO is on | Sign in; `cpsim reset-admin` then restart to run setup again; with SSO use the identity provider |
| `<NAME> is not set on this app; set the URL by hand or deploy it with ecoctl` | A `${...}` target but the variable is unset | Deploy with ecoctl (`uses`/`calls`), or type the URL, or export the variable |
| `<NAME> (...) cannot be used as a target: ...` | The resolved value breaks the URL rules (plain http, link-local...) | Use https, or for a local stand-in a workload with `CPSIM_ALLOW_HTTP_TARGETS=true` |
| `no credential for X: save ... under Settings > Credentials, or set that environment variable` | The named variable is empty and no service client is configured | Set it, save it, or configure `CPSIM_SERVICE_CLIENT_SECRET` and `CPSIM_OIDC_ISSUER` |
| City page says `CityPulse refused the token (HTTP 401/403)` | Wrong or revoked ingest token, or the minted token lacks `citypulse.ingest` | Fix the token; `ecoctl status` for drift |
| Readings pile up in "Waiting for retry" | CityPulse down or throttling | Check CityPulse; the city retries with back-off up to 20 s; dropped readings are counted |
| Workload state `failed`, banner "Stopped: ..." | The target refused the credential (401 or 403 that is not a business outcome) | Fix the credential and start again |
| Many `skipped` ticks | Target slower than the interval, or back-off after 429/503 | Lengthen the interval, lower `batch`, or check the target |
| `running workloads would ask for N items/s ... CPSIM_MAX_RATE` | Global rate ceiling | Stop another workload or raise `CPSIM_MAX_RATE` |
| PayBroker payments all `fraud_rejected` | Shared cards trip velocity rules, or PayBroker lacks the reserved-prefix mapping | Use a PayBroker that maps `999999` prefixes; keep one unique card per customer (default) |
| LLM workload `auth_error` with a service token | The gateway has no client named like the service client id (`cpsim-service`) | Register that client in the gateway, give the simulator `llm: true` in the manifest (ecoctl creates client `cpsim` and injects `LLM_API_KEY`), or use `LLMGW_CLIENT_KEY` |
| LLM workload `client_error` (`model_not_allowed`) | The gateway client may only use its own aliases | Set `chat_models` / `embedding_models` to the client's aliases (`chat-default`, `embed-default` for an ecoctl client) |
| ShopFront workload mostly `checkout_skipped` | The shop has no PayBroker connection (`payments.enabled` is false) | Connect PayBroker in the shop's settings; shoppers still browse and fill carts |
| ShopFront workload `rate_limited` | Guests are limited to 20 checkouts per 10 minutes per address, searches to 60 a minute | Use a service token (scope `shop`), lower `buy` in the mix or `search_pct` |
| `could not open the protocol endpoints` (500 on start) | OS refused the sockets | Check the process may open loopback ports |
| `the credential store is locked` | No `CPSIM_ENCRYPTION_KEY` and nobody signed in with the password since start | Set the key and recreate the sandbox, or sign in with the admin password |
| `sso_error=your account has no role in CityPulse Simulator` | The person has none of `admin`, `operator`, `viewer` | Grant a role in the identity provider or add them to an access group |
| `sso_error=...` issuer, audience, redirect | Redirect URI or issuer differs from what the provider registered | `ecoctl status` shows drift; re-run `ecoctl apply --execute`; set `CPSIM_PUBLIC_URL` if a proxy hides the address |
| Items did not come back after a restart | Resume switch off, item was stopped, or it failed (`cannot resume` line) | Check Settings > After a restart and the log |
| Changed an env var and nothing happened | Sandbox environment cannot change in place | Recreate the sandbox |
| Free-tier sandbox stopped sending | Auto-pause when idle | Resume the sandbox; items marked *start on boot* or flagged `desired_running` start again |

## 13. Testing and development

```bash
pip install -e ".[dev]"            # pytest, pytest-asyncio, ruff, pymodbus
ruff check src tests               # line length 100; rules E F I B UP
pytest -q                          # database tests are skipped without TEST_DATABASE_URL
make up && make test-db            # everything, on a throwaway database cpsim_test in the compose Postgres
```

* `make test-db` drops and recreates `cpsim_test` and runs the suite with `TEST_DATABASE_URL`. `tests/conftest.py` refuses a `TEST_DATABASE_URL` whose database name does not end in `test` (the tests drop the schema).
  It also clears the environment variables that would change behaviour (API key, OIDC, fleet URLs, tokens).
* `docs/video/record.spec.ts` is a Playwright recorder for `VIDEO-SCRIPT.md` (real Chrome, throwaway Postgres); it is not part of the test suite.
* Browser code is tested without a browser: `tests/test_ui_js.py` parses every module in `src/cpsim/static` and runs the fake-DOM scripts in `tests/ui/*.mjs` (needs `node` and the sibling `../agentpg-kit`).
  There are no real-browser tests in this repository.
* Coverage areas (by test file): API per role (`test_api.py`), cities and generator (`test_citygen*.py`, `test_model_generator.py`, `test_engine.py`), each protocol incl. a pymodbus interoperability check
  (`test_protocols.py`), companies (`test_hr.py`), workload model, schedule maths on a fake clock, runner and kinds (`test_workload_*.py`, `test_schedule.py`), headless CLI as a real process (`test_workloads_cli.py`,
  `test_outbound.py`), credentials and vault (`test_credentials_db.py`), insights route/metrics and the read-only role (`test_hardening.py`, `tests/ui/insights_tab.mjs`), federation (`test_federation_db.py`), schema upgrade (`test_upgrade_db.py`), resume (`test_resume*.py`), fleet references (`test_fleet*.py`).
  Request generators are checked against the targets' own request rules (`tests/targets.py`): copied models, plus the real PayBroker, gateway and PagerLens validators when the sibling repositories are checked out.
* Layout: `src/cpsim/api.py` (routes), `core.py` (auth model), `runtime.py` (owns running items), `engine.py` + `protocols/` (cities), `hr_*.py` (companies), `kinds/` + `wl_*.py` + `schedule.py` (workloads),
  `fleet.py`, `tokens.py`, `credentials.py`, `reporter.py`, `metrics.py`, `store.py`, `migrations/`, `static/` (UI, plain ES modules, no build step).
* Adding a workload kind: a module in `kinds/` with `validate`, a pure `plan`, and an `execute` that classifies the answer; register it in `kinds/__init__.py` (`KINDS`) with its credential variable, scope and UI fields.
* Adding a migration: a new `NNN_name.sql` in `src/cpsim/migrations/` (plain SQL, written to be re-runnable: the existing ones use `IF NOT EXISTS`).

## 14. AIDB and AgentPG integration

**Today**

* **AgentPG**: the simulator is built for it. State is in an AgentPG instance (Postgres) reached through `DATABASE_URL` or the `PG*` variables of a linked sandbox; connections retry with back-off because the database may be
  scaled to zero; `/readyz` reports `waking-database`; the sandbox runs as a non-root container on port 8000; items resume after a paused or recreated sandbox; the sandbox environment carries the credentials and fleet addresses.
  The simulator needs **no extensions** (no `vector`, no `pg_trgm`); the EDB extensions below are optional extras it reads when present.
* **AIDB**: **not used.** There is no AIDB, pgvector, embedding or MCP code in this repository (searched `src`, README, SECURITY, `pyproject.toml`). The simulator does not call a model itself; its `llmgw` workload only
  sends synthetic chat and embedding calls through the gateway to load it.

### EDB and AgentPG capabilities used by this app

| Capability | How the simulator uses it |
|---|---|
| EDB `edb_stat_monitor`, `edb_pg_tuner`, `query_advisor` (`pg_stat_statements` fallback) | **Database** tab and `GET /api/insights`; `db_insights_*` gauges on `/metrics` ([8.9](#89-database-insights-database-tab)). Optional: degrades to "not available" on plain Postgres. |
| AgentPG read-only role `a1p_readonly`, `agentpg db-users`, restricted MCP (`agentpg mcp serve`) | REVOKE on the credential ciphertext, vault, session and token tables ([8.10](#810-what-a-read-only-database-user-and-agentpgs-restricted-mcp-can-see)). Database-enforced, no `data_redaction` extension. |
| AgentPG sandbox, instance, scale-to-zero wake, environment injection | Deployment (above) |
| Not used | AIDB (role grants missing on the platform), pgvector, PostGIS |

**Planned (from `ecosystem/docs/03-aidb-integration.md` section 5.4 and `04-showcase-readiness-plan.md`, status "design", not implemented)**

* The simulator becomes the load and workload generator **for AIDB itself**: pipelines under sustained inserts, agents under concurrent conversations, retrieval latency under a 10,000-sensor city.
* Published benchmarks produced with it: PayBroker authorizations per second and p95, CityPulse readings per second with a 10,000-sensor city, TrustDesk retrieval latency, PagerLens time to first hypothesis, AIDB pipeline lag.
* A fleet smoke test (`ecoctl verify`) that exercises simulator to CityPulse to PagerLens.
* The AIDB spike notes in the ecosystem docs say the role of an instance lacked AIDB grants on the spike instance; none of that touches the simulator.

None of these has code in this repository yet.

## 15. Command reference

| Command | Needs Postgres | Purpose |
|---|---|---|
| `cpsim serve` | yes | apply migrations, run the web app and API |
| `cpsim migrate` | yes | apply the SQL migrations (prints `applied: ...`) |
| `cpsim reset-admin [--yes]` | yes | forget the admin password so setup can run again (restart to get a new code); also deletes the saved credentials, after confirmation |
| `cpsim import-files DIR` | yes | one-shot load of the first release's file store (cities, companies, admin password) |
| `cpsim validate FILE` | no | check a city file |
| `cpsim run FILE [--seconds N]` | no | run a city headless |
| `cpsim hr-validate FILE` / `cpsim hr-run FILE [--dry-run]` | no | check / run a company |
| `cpsim workloads validate FILE` / `cpsim workloads run FILE [...]` | no | check / run workloads |
| `cpsim cities generate [--count --size --profile --seed --name --out --format yaml\|json --target-url --token-env --autostart -v]` | no | generate demo cities as files |

Example files: `examples/brussels.yaml` (a real city), `examples/acme-hundreds.yaml` (a company, about 500 requests), `examples/live/*.yaml` (one workload file per kind, intervals of 2 and 5 seconds).
In a workloads file, `${VAR}` and `${VAR:-default}` are expanded in `target_url` only; an exact fleet reference is kept and resolved at start. Credentials are never read from a file, only from the named variable.

## 16. Known limitations and things not verified

Limitations stated by the code and docs:

* Rate limiting of failed sign-ins is in memory, per process.
* One protocol endpoint per protocol per city; the gateway and devices share a process, so this shows data-path behaviour and failure modes, not network latency.
* Protocols other than Modbus TCP are not conformance tested; no real PLC was used.
* Company runs: requests are plausible, not realistic; they assume common policy values; employees never answer an approver; the simulator cannot approve or reject. Results live in memory.
* Counters live in memory and restart with the item; there is no history.
* The environment of a sandbox cannot change in place.
* Free-tier sandboxes auto-pause when idle, which stops all running items until resumed.
* A `trustdesk` workload needs an existing company; `identity` is deliberately slow.

Found while writing this guide, since fixed (kept here for the record):

* The README's test section named kit tag `v0.2.0`; it now says 0.2.5 or newer, as `pyproject.toml` and the Dockerfile do.
* `pyproject.toml` and `__version__` say 0.2.0 (unchanged).
* The PagerLens self-report ignored `CPSIM_ALLOW_HTTP_TARGETS`; it now applies the same URL rules as a workload target.
* `cpsim reset-admin` left saved credentials as unreadable rows; it now names and deletes them after confirmation.
* The `llmgw` workload with a service token needs a gateway client named exactly like the service client id (`cpsim-service`), which the gateway matches by name; nothing creates it. The workload
  now also accepts `LLM_API_KEY` and the aliases that ecoctl sets for an `llm: true` app, so adding `llm: true` to the `cpsim` manifest entry (proposed, not yet made) is enough.
* There was no ShopFront workload; the `shopfront` kind was added. Its `calls: shopfront: [shopfront.api]` entry in the manifest is proposed, not yet made; until then set `SHOPFRONT_URL` and
  `SHOPFRONT_TOKEN` by hand, or run the shoppers as guests.

Not verified in this review (no live services were contacted, nothing was deployed or run):

* Behaviour against real instances of CityPulse, TrustDesk, PagerLens, PayBroker, the gateway and the identity provider (the ecosystem docs themselves list a full end-to-end PayBroker workload run with a saved merchant key
  as not yet verified).
* The exact ingest request schemas of the other apps; they were taken from the simulator's own senders and its tests, not from the other repositories.
* The suite was run (with a throwaway database) after the changes listed above; the `shopfront` kind is tested against a fake ShopFront and a fake PayBroker only, never a live shop.
* Rotating `CPSIM_ENCRYPTION_KEY` independently of the admin password: the kit supports two wrappings of the data key, but no command here rotates the platform key.
* Existing cities, companies and workloads that hold literal URLs still need switching to fleet references by hand (ecosystem docs).
