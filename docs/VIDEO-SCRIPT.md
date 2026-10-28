# Video script: CityPulse Simulator in six minutes

**Title:** CityPulse Simulator: a city of fake sensors and a stream of fake traffic, on demand

**Length:** about 7 minutes 50 seconds in the review cut (narration spoken by Kokoro neural voices, plus deliberate pauses over on-screen waits).

**Audience:** customers and internal teams (sales engineers, developers, anyone who has to demo the EDB AIDB + AgentPG showcase apps). No knowledge of industrial
protocols is assumed; each term is explained when it first appears.

**Goal:** after the video the viewer can start the simulator, create and run a simulated city, burst it, run a scheduled workload, preview a simulated company,
and knows how the simulator plugs into the other apps (CityPulse, PagerLens, TrustDesk, PayBroker, the LLM gateway, identity) and how to watch it.

The reference for everything shown is [GUIDE.md](GUIDE.md).

## Prerequisites and seed data (reproducible on a laptop)

The main path needs **no other app**. A city without a CityPulse URL only generates data, and a workload without a URL only generates items, so the video is
fully reproducible with the simulator and a throwaway Postgres. A short optional scene (7b) shows a live fleet and needs one.

Software: Docker, Python 3.11 or newer, the repository checked out with `../agentpg-kit` next to it (or the kit installed with pip, see GUIDE section 5.1), a browser,
a terminal with a large font. `pymodbus` comes with `pip install -e ".[dev]"`.

Reset to a clean state before every take:

```bash
cd CityPulseSimulator
docker compose down -v                               # deletes the throwaway database volume
cp .env.example .env                                 # once; then put a throwaway value in POSTGRES_PASSWORD
make up                                              # postgres:17 on 127.0.0.1:5436
. .venv/bin/activate && pip install -e ".[dev]"      # once
set -a; . ./.env; set +a
export CPSIM_METRICS_TOKEN=demo-metrics-token        # throwaway; lets scene 8 call /metrics
cpsim serve                                          # leave running in terminal A; the setup code is printed here
```

Seed data: none is imported. Everything is created live, which is part of the demo, and is deterministic:

| Item | Name | How it is created | Why it is reproducible |
|---|---|---|---|
| City | `demo-city` | Cities > Fixed presets > **Fixed demo city (always the same)** | A fixed preset: 174 sensors, seed 42, the same groups every time. |
| Workload | `shop-telemetry` | Workloads > kind *PagerLens telemetry* > **Create**, URL left empty | Generate only, default parameters. |
| Company | `acme` | Companies > **Quick check (about 60 requests)** | A preset; **Preview** is decided from its seed. |

Terminal B (same folder, same virtual environment) is used for the Modbus read in scene 4 and the `curl` in scene 8. Browser: one window at 1920 x 1080, zoom 110%, one
tab at `http://127.0.0.1:8000`. Admin password for the recording: any 12-character throwaway.

Do not show a real token, a real URL of a fleet, or a real password on screen. The setup code and the metrics token in this script are throwaway values.

## Scenes

Times are cumulative and approximate. "Callout" is on-screen text added in editing.

| # | Time | On-screen action | Narration | Callouts |
|---|---|---|---|---|
| 1 | 0:00 to 0:24 | Title card, then the README top diagram (`README.md`, the ASCII flow: sensors, field devices, edge gateway, CityPulse). Slow zoom along the arrows. | "This is the CityPulse Simulator. It builds a whole smart city out of nothing. It invents thousands of sensors, and sends their readings the way a real plant would. It can also push payments, model calls and employee requests at the other apps of our showcase. In the next six minutes, you will start it, run a city, and break that city on purpose. Then we will see how it connects to everything else." | Title: CityPulse Simulator. Arrows labelled: sensors, field devices, edge gateway, CityPulse. |
| 2 | 0:24 to 0:54 | Terminal A: show `cpsim serve` output with the line `=== CityPulse Simulator first-run setup ===` (the code is in the same line, after "enter this one-time code to create the admin password:", followed by uvicorn's INFO lines). Switch to the browser at `http://127.0.0.1:8000`. Type the code into **Setup code**, a throwaway password twice, click **Create admin password**. The Cities tab opens. | "The simulator is one small web app on top of Postgres. When it starts for the first time, it prints a one-time setup code. Here it is, in the terminal. Nobody can use the app without that code. So a new install is never open by accident. In the browser, we type the code, and choose an admin password. Create it, and we are in. In the real fleet, people sign in through the shared identity provider instead. The roles are admin, operator and viewer." | Terminal: one-time setup code. Browser: Postgres is the only dependency. Lower third: roles admin, operator, viewer. |
| 3 | 0:54 to 1:56 | Cities tab. Type `demo-city` in **City name**, click **Fixed demo city (always the same)**. The city page opens. Scroll the **Sensor groups** table slowly: type, count, district, protocol, range, every, burst. Point at the Protocol column (modbus tcp, ethernet ip, ethercat, profinet, profibus, direct) and the Burst column. | "Let's build a city. In this app, a city is simply a list of sensor groups. We name it demo city, and pick the fixed preset, which is always the same. Each group says what kind of sensor, how many, and in which district. It also says how often they report, and how they talk. That last part is the protocol: the language the device speaks to the gateway. Modbus, EtherNet IP and EtherCAT are languages of factories and utilities. So are Profinet and Profibus. Direct skips them. The last column is the burst percentage. It says how likely a sensor is to misbehave. This preset has one hundred seventy four sensors, and it is the same every time. If you want cities that differ, the city generator makes ten at a time. The same seed always gives the same city. Cities can have up to ten thousand sensors. That is how you load test the whole chain." | Highlight Protocol column. Highlight Burst column. Caption: 174 sensors, seed 42. |
| 4 | 1:56 to 3:14 | Click **Start city**. The stats cards appear: **Delivery to CityPulse** (Readings sent, Send rate, Status ok) and **Field-device endpoints** (protocol, `127.0.0.1:port`, devices, polls ok). Terminal B: `python -c "from pymodbus.client import ModbusTcpClient; c=ModbusTcpClient('127.0.0.1', port=<modbus port from the page>); print(c.read_input_registers(0, count=6, device_id=1).registers)"` Show the printed list of numbers. **Wait about 15 s after Start city first:** sensors report every 10 to 15 s and until the first one has, the registers are all `[0, 0, 0, 0, 0, 0]`. The six registers are two channels, each a float32 value in two registers followed by an update counter (for example `[16814, 7551, 2, 16816, 20609, 1]`), so do not read them out as sensor values. Back in the browser, point at Readings sent counting up. | "Click Start city, and the city comes alive. Each protocol gets a real server on this machine. A gateway polls them, and turns the changes into readings. Just as an edge box would in a plant. Sensors report every ten to fifteen seconds. Until the first one has, the Modbus registers stay at zero. So let's give it a moment, and watch the readings sent counter. [pause 9] There it goes. Now for the proof. Here is an ordinary third party Modbus client, asking that server for its registers. And it answers with numbers. Six registers make two channels. Each has a value, and an update counter. Because no CityPulse address is set, the readings are counted, but not sent anywhere. With an address, the same readings go to CityPulse over HTTPS, in batches. There is a retry buffer, if CityPulse is slow. If it pushes back, the city waits and tries again. And if the token is refused, the page says so in plain words." | Callout on endpoints table: real servers on loopback. Callout on terminal: third-party Modbus client. Callout: no URL means generate only. |
| 5 | 3:14 to 4:00 | On the city page, in the run card choose **Burst the whole city for** 1 minute, click **BURST**. Watch **Behaviour**: *Bursts active* fills (for example spike, noise, stuck, dropout), *Corrupt values* and *Silent updates* rise. Optional: cut to the README table of burst episodes. | "Next, a city that misbehaves. Real cities have bad days. A burst is a random episode, where a sensor misbehaves. We turn the whole city up for one minute, and press burst. [pause 4] Now look at the behaviour card. Bursts active fills up. Corrupt values and silent updates start to rise. The episodes are a wild spike, extra noise, a stuck value, or a sensor that goes silent. Others flood, or send garbage. Each group's burst percentage sets how often it happens. This button turns the whole city up at once. So later, the monitoring apps have something real to notice. And if you give the city a seed, even the chaos repeats exactly." | Callout: spike, noise, stuck, dropout, flood, corrupt. Callout: same seed, same bursts. |
| 6 | 4:00 to 5:11 | Click **Stop city**. Open the **Workloads** tab. Name `shop-telemetry`, kind *PagerLens telemetry*, **Create**. On the page click the preset *Healthy baseline every 5 s*, then **Save and start**. Show the blue **Generate only** banner and the KPIs (ticks, items sent, latency) and the Outcomes table showing `generated`. Select **bad deploy** in the scenario list, click **Switch scenario**. | "Cities cover the sensors. Workloads cover everything else. A workload sends simulated traffic to one app, on a schedule you choose. First we stop the city, and open the workloads tab. We create a workload called shop telemetry, of kind PagerLens telemetry. It plays a small online shop, reporting to PagerLens, our incident tool. We pick the healthy baseline, every five seconds, then save and start. [pause 5] The blue banner says generate only. With no address, it only generates, so this is safe to try. Now the interesting part. We pick the bad deploy scenario, and switch. With a real PagerLens set, the shop now fails on purpose. Errors and slowness, so an incident opens on the other side. There are also workloads for PayBroker payments, ShopFront shoppers and the LLM gateway. And for TrustDesk, and the identity provider. A schedule is paced against a steady clock. So a slow target makes ticks skip, instead of piling up. And a hard rate ceiling keeps the simulator from flooding anything by mistake." | Callout: scheduled traffic. Callout: kinds: PayBroker, ShopFront, LLM gateway, PagerLens, TrustDesk, identity. Callout on switch: failure on purpose. |
| 7 | 5:11 to 5:52 | Click **Stop**. Open **Companies**, name `acme`, click **Quick check (about 60 requests)**. On the company page click **Preview**: show the table of types, planned counts, problem counts and an example. Point at *Problem requests (%)* 25. | "Now, a different kind of traffic: a simulated company. A company is a simulated employer for TrustDesk. It invents employees, new hires and long-serving staff. And it sends requests of each type: vacations, sick leave and expenses. We stop the workload, name the company acme, and choose the quick check. Then we press preview. Preview shows exactly what would be sent. It is decided from a seed, and it sends nothing. A quarter of the requests are built to break a typical policy. Too little notice, or no receipt, for example. So TrustDesk has something to judge. Every name is invented, on example dot com." | Callout: all names are invented, on example dot com. Callout: preview sends nothing. |
| 8 | 5:52 to 6:49 | **Settings** tab. Scroll the page in its real order: **Admin password**, **After a restart** switch, **Fleet** card (seven variables, all *not set* locally), **Credentials for the apps it drives** (names only, a box per name, no values), **API tokens**. The `grep` shows `cpsim_workloads_running 0.0` because the workload was stopped in scene 7 (start it again before this scene if you want `1.0`). Terminal B: `curl -s -H "Authorization: Bearer demo-metrics-token" http://127.0.0.1:8000/metrics \| grep cpsim_workloads_running` and `curl -s http://127.0.0.1:8000/healthz`. | "Last, the settings tab. In the fleet, you never type app addresses in. ecoctl gives the simulator the seven app addresses. A target is looked up each time something starts. So a recreated app is followed automatically. Here on a laptop, they all say not set. Credentials are saved encrypted, and never shown again. Nothing secret is ever stored inside a city or a workload. The app also remembers what was running, so a restart brings everything back. For monitoring, it answers health checks, and Prometheus metrics. Let's ask it, from the terminal. [pause 1] The metrics show zero workloads running, because we stopped it. And the health check says okay. It also reports its own health to PagerLens. Three roles control who can do what. Viewers only look. Operators run and edit simulations. Administrators also manage credentials and tokens." | Callout: ecoctl injects the fleet addresses. Callout: secrets are never stored in a city or workload. Callout: /healthz, /metrics. |
| 9 | 6:49 to 7:26 | Back to the README diagram or the GUIDE mermaid diagram (section 2). Then the end card with the repository path `CityPulseSimulator/docs/GUIDE.md` and the command `cpsim serve`. | "Let's wrap up what the CityPulse Simulator does. It builds cities of sensors that speak real industrial protocols. It bursts them on demand. It runs scheduled traffic for the other apps. And it sends simulated employee requests to TrustDesk. All of it comes from one app, with roles, a vault and metrics. It needs only Postgres. And the same files run headless, with no database at all. Start with the guide, in the docs folder. Run it locally in five minutes. Then point it at the fleet, and watch the other apps come alive. Thanks for watching." | End card: docs/GUIDE.md. Command: `cpsim serve`. Call to action: run it, then point it at the fleet. |

### Optional scene 7b (not counted in the six minutes): the live fleet

Only for a recording against a deployed fleet (the URLs are whatever `ecoctl` gave that fleet; never type them from memory, and never show them in a public cut). In the
city settings click **Use the fleet's CityPulse**, save, start the city, and show readings arriving in CityPulse; start a PagerLens workload with *Bad deploy of checkout* and
show the incident in PagerLens. Narration: "On the fleet, one click puts the CityPulse reference in. Start the city and the readings show up in CityPulse. Start the bad deploy,
and PagerLens opens an incident and starts investigating." The simulator needs the identity service client or the matching tokens for these calls (GUIDE section 3).

## Narration word count

The narration is rewritten for listening in `docs/video/narration.txt` (short sentences, deliberate `[pause]` marks over the Modbus wait, the burst and the workload start) and mirrored into the table above by `docs/video/build.py`; scene 7b is not part of the review cut.
If a take runs long, shorten scene 5 (skip the README table) and scene 8 (skip the `healthz` call).

## Recording notes

* Record the screen at 1920 x 1080; hide bookmarks and extensions; terminal font 18 pt or larger; light or dark theme, but the same in all shots.
* Record the narration separately and cut to it; the pauses in scenes 4 and 6 are for readings and counters to move.
* Numbers (readings sent, send rate, burst names) vary a little by take; do not read them out. The 174 sensors and the 25% problem share are fixed.
* The spoken variable name in scene 8 is the seven fleet addresses; show the Fleet card on screen instead of reading them.
* Everything on screen is fictional data. Do not paste any real credential into the Credentials box; if you show the box, leave it empty.
* Before the take, clear the browser's saved passwords for this origin and sign out once to show the clean state.

### The recorder: `docs/video/record.spec.ts`

`docs/video/record.spec.ts` performs every on-screen action of scenes 1 to 9 in real Google Chrome (Playwright channel `chrome`, no browser download), with video on at
1920 x 1080, and writes one `.webm` to `docs/video/out/` (git-ignored). It is a rehearsal and capture aid, not the final video: no narration, and the callouts are drawn as a caption bar
and an amber highlight ring on the elements the script points at, with a visible mouse pointer, typing at human speed, CSS zoom 110% and scene boundaries padded to the script's times.

```bash
cd docs/video && npm install                  # @playwright/test 1.64.1; real Chrome must be installed; the video encoder needs `npx playwright install ffmpeg` once, or run `python3 build.py`, which needs no download (see docs/video/README.md)
PG_BIN=/path/to/postgres/bin npx playwright test        # PG_BIN: folder with initdb and postgres (default: `pg_config --bindir`); RECORD_PACE=0.3 rehearses faster, HEADED=1 shows the window
```

It starts everything itself and removes it afterwards: a throwaway Postgres (UTF8, `127.0.0.1` only, no unix socket, database `cpsim_video_test`) and `cpsim serve` from the repository's `.venv`
(`CPSIM_BIN` to use another) on a free port, with `CPSIM_METRICS_TOKEN=demo-metrics-token`. No other app is contacted. The terminal scenes (2, 4, 8) are drawn as a terminal overlay in the same page with
the real output of the real commands (the server's setup banner, the pymodbus read against the city's Modbus port, `curl` of `/metrics` and `/healthz`), so the take is one continuous video.

Differences found by running the script's earlier Playwright outline, now corrected here and in the spec:

* The setup code matches `[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}` (confirmed); it is printed inside a sentence, not on a line of its own.
* The app's strict Content-Security-Policy blocks injected styles, so the recorder's browser context runs with `bypassCSP` (only for the recording; the app is unchanged).
* The Modbus registers are zero until the first sensor has reported (see scene 4), so the recorder polls the read until it returns data.
* Every selector and button text in the first outline was correct (`#setup-code`, `#setup-pw`, `#setup-pw2`, `#nc-name`, `#storm-secs`, `#nwl-name`, `#nwl-kind`, `#wl-scenario`, `#nco-name`, `#fleet-table`, `#resume-on-start`).
* The toasts `Burst started`, `Scenario: bad deploy`, the banner `Generate only: no target URL, so nothing is sent.` and the heading `Preview: ...` appear exactly as written.
