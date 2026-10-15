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
pip install -e ".[dev]"
cpsim serve                       # http://127.0.0.1:8000 ; the one-time setup code is printed in the terminal
```

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
| `DATABASE_URL` / `DB_EXTERNAL_DSN` / `PSQL_CONNECTION_STRING` | If set, cities and the admin password hash live in Postgres (they survive restarts, the AgentPG way). Otherwise a JSON file in `CPSIM_DATA_DIR` (default `data/`). |
| `CITYPULSE_INGEST_TOKEN` (or the variable a city names in `token_env`) | The ingest token for that city's CityPulse. Also read from `<NAME>_FILE`. **Never stored in a city.** |
| `CPSIM_METRICS_TOKEN` | Lets `/metrics` be scraped with a token that can read nothing else. |
| `CPSIM_API_KEY` | Optional static full-access key for automation. |
| `PAGERLENS_URL`, `PAGERLENS_TOKEN` | Report to PagerLens (an ingest token from PagerLens). https required unless localhost. |
| `CPSIM_ALLOW_ANONYMOUS` | Demo only: disables authentication entirely. |

## Monitored by PagerLens

Either run `pagerlens-agent` against `/healthz` and `/metrics` (create a scrape token: `CPSIM_METRICS_TOKEN`), or set
`PAGERLENS_URL`/`PAGERLENS_TOKEN` and the simulator pushes `up`, `request_rate` (readings delivered per second) and
`error_rate` (failed deliveries) for the service `citypulse-simulator`, which *depends on* `citypulse-ingest`, and raises
`SimulatorCannotReachCityPulse` when delivery keeps failing. `/metrics` also has `cpsim_*` series per city and protocol.

## Deploy on AgentPG

```bash
agentpg login
agentpg create --name cpsim-db --size small                 # an instance for the simulator (optional but recommended)
agentpg sandbox create cpsim \
  --image ghcr.io/<owner>/citypulsesimulator:latest \
  --instance <instance-id> --port 8000 \
  --env CITYPULSE_INGEST_TOKEN=<ingest token> --env CPSIM_METRICS_TOKEN=<random string>
```

The image must be public on GHCR. `--env` is not a secret store: anyone with access to the sandbox's definition can read
it, so use a dedicated, narrow `ingest` token that you can revoke. Without a linked database, cities live in the
container and disappear when it is recreated. Free-tier sandboxes auto-pause when idle, which stops the simulator; a
resumed sandbox restarts cities that have *Start this city when the app starts* ticked. The full walkthrough for all four
projects is in the CityPulse tutorial.

## Tests

```bash
pip install -e ".[dev]" && ruff check src tests && pytest -q
```

Covers every protocol stack (including malformed input, dead links, wrong addresses and a pymodbus interoperability
check), the generator (burst probability rises with the percentage; seeds reproduce; every episode behaves as described),
validation of cities, a whole city across all five protocols into a sink, back-pressure and retry, and the web API.

## Honest limits

* One protocol endpoint per protocol per city, hosting up to 32 sensors per device; a city is capped at 10,000 sensors.
* The gateway and devices share a process, so this shows data-path behaviour and failure modes, not network latency.
* A protocol's address space caps its devices (PROFIBUS DP: 125 slaves, so 4,000 sensors).
* Rate limiting of failed sign-ins is in memory, per process. Sessions are in memory and end on restart.

Licensed under Apache-2.0. See [SECURITY.md](SECURITY.md).
