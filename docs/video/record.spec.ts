import { test, expect, Page, Locator } from '@playwright/test';
import { execFileSync, spawn, ChildProcess } from 'node:child_process';
import * as fs from 'node:fs';
import * as net from 'node:net';
import * as os from 'node:os';
import * as path from 'node:path';

/**
 * Performs the on-screen actions of docs/VIDEO-SCRIPT.md, scene by scene, and records them (one 1920x1080 video, see playwright.config.ts).
 * It is not the final video: no narration, and the callouts are the short captions of the script's "Callouts" column, drawn as an overlay.
 *
 * Everything is local and throwaway: a Postgres cluster of its own (UTF8, 127.0.0.1 only, no unix socket, database name ending in `test`)
 * and `cpsim serve` on a free port. Nothing is sent to any other app: the city and the workload have no target URL.
 *
 * The two terminal scenes (2, 4 and 8) are drawn as a terminal overlay in the same page, with the REAL output of the commands the
 * script runs (the server's setup banner, the pymodbus read, curl), so that the video is one continuous take.
 *
 * Settings (environment): RECORD_PACE (default 1; 0.3 for a quick rehearsal), PG_BIN (folder with initdb and postgres; default: pg_config --bindir),
 * CPSIM_BIN (default ../../.venv/bin/cpsim), HEADED=1 to watch.
 *
 * Narration cut (driven by docs/video/build.py): SCENE_INFO=<durations.json> makes every scene follow the measured audio (each action starts when the
 * narration sentence it illustrates begins: `at(k)` = the k-th sentence of the scene, counted from 0 across paragraphs), SCENE_DURATIONS=<json> gives the
 * minimum length of each scene (audio + 0.7 s), TIMINGS_OUT=<file> receives the scene start times. CPSIM_PORT_RANGE=lo-hi (default 41600-41699) keeps every
 * port the run opens inside that range. Without SCENE_INFO the recorder runs as a quick rehearsal (RECORD_PACE).
 */
const REPO = path.resolve(__dirname, '..', '..');
const PACE = Number(process.env.RECORD_PACE ?? '1');
const METRICS_TOKEN = 'demo-metrics-token'; // throwaway, as in the script
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const hold = (ms: number) => sleep(ms * PACE);

// ---------------------------------------------------------------------------------------------------------------- local services
const [PORT_LO, PORT_HI] = (process.env.CPSIM_PORT_RANGE ?? '41600-41699').split('-').map(Number);
const takenPorts = new Set<number>();
async function freePort(): Promise<number> {
  for (let p = PORT_LO; p <= PORT_HI; p++) {
    if (takenPorts.has(p)) continue;
    const ok = await new Promise<boolean>((res) => {
      const s = net.createServer();
      s.once('error', () => res(false));
      s.listen(p, '127.0.0.1', () => s.close(() => res(true)));
    });
    if (ok) {
      takenPorts.add(p);
      return p;
    }
  }
  throw new Error(`no free port in ${PORT_LO}-${PORT_HI}`);
}

function pgBin(): string {
  if (process.env.PG_BIN) return process.env.PG_BIN;
  return execFileSync('pg_config', ['--bindir'], { encoding: 'utf8' }).trim();
}

async function waitFor<T>(what: string, fn: () => Promise<T | false | undefined>, ms = 60_000): Promise<T> {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    try {
      const v = await fn();
      if (v) return v;
    } catch {
      /* retry */
    }
    await sleep(250);
  }
  throw new Error(`timed out waiting for ${what}`);
}

interface Services {
  url: string;
  port: number;
  logs(): string;
  stop(): Promise<void>;
}

async function startServices(): Promise<Services> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'cpsim-video-'));
  const bin = pgBin();
  const pgEnv = { PATH: `${bin}:/usr/bin:/bin`, LANG: 'C', LC_ALL: 'C' };
  const data = path.join(dir, 'pgdata');
  execFileSync(path.join(bin, 'initdb'), ['-D', data, '-E', 'UTF8', '--locale=C', '-A', 'trust', '-U', 'postgres'], { env: pgEnv, stdio: 'pipe' });
  const pgPort = await freePort();
  const pgLog = fs.openSync(path.join(dir, 'postgres.log'), 'a');
  const pg: ChildProcess = spawn(
    path.join(bin, 'postgres'),
    ['-D', data, '-c', 'listen_addresses=127.0.0.1', '-c', `port=${pgPort}`, '-c', 'unix_socket_directories=', '-c', 'fsync=off'],
    { env: pgEnv, stdio: ['ignore', pgLog, pgLog] },
  );
  const psql = (db: string, sql: string) =>
    execFileSync(path.join(bin, 'psql'), ['-h', '127.0.0.1', '-p', String(pgPort), '-U', 'postgres', '-d', db, '-v', 'ON_ERROR_STOP=1', '-c', sql], { env: pgEnv, stdio: 'pipe' });
  await waitFor('postgres', async () => {
    try {
      execFileSync(path.join(bin, 'pg_isready'), ['-h', '127.0.0.1', '-p', String(pgPort)], { env: pgEnv, stdio: 'pipe' });
      return true;
    } catch {
      return false;
    }
  });
  const dbName = 'cpsim_video_test'; // a throwaway database: the name ends in `test`
  psql('postgres', `CREATE DATABASE ${dbName} ENCODING 'UTF8' TEMPLATE template0`);

  const port = await freePort();
  const cpsimBin = process.env.CPSIM_BIN ?? path.join(REPO, '.venv', 'bin', 'cpsim');
  if (!fs.existsSync(cpsimBin)) throw new Error(`${cpsimBin} not found: create the virtualenv first (pip install -e ".[dev]") or set CPSIM_BIN`);
  let buf = '';
  const app = spawn(cpsimBin, ['serve'], {
    cwd: dir,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    env: {
      PATH: '/usr/bin:/bin',
      HOME: dir,
      TMPDIR: dir,
      LANG: 'en_US.UTF-8',
      PYTHONUNBUFFERED: '1',
      NO_COLOR: '1',
      HOST: '127.0.0.1',
      PORT: String(port),
      DATABASE_URL: `postgresql://postgres@127.0.0.1:${pgPort}/${dbName}`,
      CPSIM_METRICS_TOKEN: METRICS_TOKEN,
    },
  });
  app.stdout!.on('data', (d) => (buf += d));
  app.stderr!.on('data', (d) => (buf += d));
  const url = `http://127.0.0.1:${port}`;
  await waitFor('cpsim /readyz', async () => (await fetch(url + '/readyz')).ok, 90_000);
  return {
    url,
    port,
    logs: () => buf,
    async stop() {
      try {
        process.kill(-app.pid!, 'SIGTERM');
      } catch {}
      await sleep(1000);
      try {
        process.kill(-app.pid!, 'SIGKILL');
      } catch {}
      try {
        pg.kill('SIGINT');
      } catch {}
      await sleep(1500);
      try {
        pg.kill('SIGKILL');
      } catch {}
      fs.rmSync(dir, { recursive: true, force: true });
    },
  };
}

// ---------------------------------------------------------------------------------------------------------------- overlays
/** A visible mouse pointer, the caption bar ("callout"), the highlight ring and the terminal overlay; re-installed on every page load. */
const OVERLAY = `
(() => {
  if (window.__pw) return;
  window.__pw = true;
  const init = () => {
  const css = document.createElement('style');
  css.textContent = \`
    #pw-cursor{position:fixed;z-index:2147483647;width:22px;height:22px;margin:-4px 0 0 -4px;border-radius:50%;background:rgba(230,40,40,.85);
      border:2px solid #fff;box-shadow:0 0 0 2px rgba(0,0,0,.35);pointer-events:none;transition:transform .08s}
    #pw-caption{position:fixed;z-index:2147483646;left:50%;bottom:44px;transform:translateX(-50%);max-width:70%;padding:14px 28px;border-radius:12px;
      background:rgba(15,23,42,.94);color:#fff;font:600 26px/1.3 system-ui,-apple-system,sans-serif;box-shadow:0 8px 30px rgba(0,0,0,.4);
      opacity:0;transition:opacity .35s;pointer-events:none;text-align:center}
    #pw-caption.on{opacity:1}
    .pw-hl{outline:5px solid #ffb000 !important;outline-offset:4px;border-radius:6px;animation:pw-pulse 1.1s ease-in-out infinite}
    @keyframes pw-pulse{50%{outline-color:#ff5a1f;outline-offset:8px}}
    #pw-term{position:fixed;z-index:2147483645;inset:0;background:#0b1220;color:#d6e2f5;padding:56px 72px;font:26px/1.45 ui-monospace,Menlo,monospace;
      white-space:pre-wrap;overflow:hidden}
    #pw-term .bar{color:#7f93b5;font-size:20px;margin-bottom:28px;letter-spacing:.04em}
    #pw-term .p{color:#7fd88f}
    #pw-card{position:fixed;z-index:2147483645;inset:0;background:linear-gradient(135deg,#0f172a,#1e3a5f);color:#fff;display:flex;flex-direction:column;
      align-items:center;justify-content:center;gap:28px;font-family:system-ui,-apple-system,sans-serif;text-align:center}
    #pw-card h1{font-size:76px;margin:0}
    #pw-card p{font-size:32px;margin:0;color:#c3d4ee}
    #pw-card pre{font:17px/1.4 ui-monospace,Menlo,monospace;color:#9fe3ff;text-align:left;background:rgba(0,0,0,.28);padding:22px 30px;border-radius:12px;margin:0;white-space:pre}
  \`;
  document.documentElement.appendChild(css);
  const mk = (id) => { const e = document.createElement('div'); e.id = id; document.documentElement.appendChild(e); return e; };
  const cur = mk('pw-cursor'), cap = mk('pw-caption');
  cur.style.left = cur.style.top = '-50px';
  addEventListener('mousemove', (e) => { cur.style.left = e.clientX + 'px'; cur.style.top = e.clientY + 'px'; }, true);
  addEventListener('mousedown', () => { cur.style.transform = 'scale(.7)'; }, true);
  addEventListener('mouseup', () => { cur.style.transform = ''; }, true);
  window.__caption = (t) => { if (t) { cap.textContent = t; cap.classList.add('on'); } else cap.classList.remove('on'); };
  window.__term = (title, html) => {
    let t = document.getElementById('pw-term');
    if (!t) { t = document.createElement('div'); t.id = 'pw-term'; document.documentElement.appendChild(t); }
    t.innerHTML = '<div class="bar">' + title + '</div>' + html;
  };
  window.__termOff = () => { const t = document.getElementById('pw-term'); if (t) t.remove(); };
  window.__card = (html) => {
    let c = document.getElementById('pw-card');
    if (!c) { c = document.createElement('div'); c.id = 'pw-card'; document.documentElement.appendChild(c); }
    c.innerHTML = html;
  };
  window.__cardOff = () => { const c = document.getElementById('pw-card'); if (c) c.remove(); };
  document.documentElement.style.zoom = '1.1'; // the script's "zoom 110%"
  };
  // init scripts run before the root element exists
  if (document.documentElement) init();
  else new MutationObserver((_, o) => { if (document.documentElement) { o.disconnect(); init(); } }).observe(document, { childList: true });
})();
`;

const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

async function caption(page: Page, text: string, ms = 3500) {
  await page.evaluate((t) => (window as any).__caption(t), text);
  await hold(ms);
  await page.evaluate(() => (window as any).__caption(''));
  await hold(300);
}

/** Highlight an element for a while (the script's "highlight ..." callouts). */
async function highlight(loc: Locator, ms = 3000) {
  await loc.scrollIntoViewIfNeeded();
  await loc.evaluate((e) => e.classList.add('pw-hl'));
  await hold(ms);
  await loc.evaluate((e) => e.classList.remove('pw-hl'));
}

/** Move the pointer to the element, pause, click: a person's click. */
async function click(page: Page, loc: Locator, pause = 700) {
  await loc.scrollIntoViewIfNeeded();
  await loc.hover();
  await hold(pause);
  await loc.click();
  await hold(400);
}

async function typeSlow(loc: Locator, text: string, perKey = 90) {
  await loc.hover();
  await loc.click();
  await loc.pressSequentially(text, { delay: perKey * Math.max(PACE, 0.2) });
  await hold(350);
}

let termHtml = '';
/** Draw the terminal overlay and type the commands (output appears after each); `append` keeps what an earlier call drew. */
async function terminal(page: Page, title: string, blocks: { cmd?: string; out?: string }[], ms: number, append = false) {
  if (!append) termHtml = '';
  const draw = (h: string) => page.evaluate(([t, x]) => (window as any).__term(t, x), [title, h]);
  await draw(termHtml);
  for (const b of blocks) {
    if (b.cmd) {
      let typed = '';
      for (const ch of b.cmd) {
        typed += ch;
        await draw(termHtml + '<span class="p">$ </span>' + esc(typed) + '█');
        await sleep(Math.max(12, 28 * PACE));
      }
      termHtml += '<span class="p">$ </span>' + esc(b.cmd) + '\n';
    }
    if (b.out) termHtml += esc(b.out) + '\n\n';
    await draw(termHtml);
    await hold(400);
  }
  await hold(ms);
}

const termOff = (page: Page) => page.evaluate(() => (window as any).__termOff());

/** The one-liner of scene 4 and the curl calls of scene 8, run for real against the local app. */
function run(cmd: string, args: string[], env: Record<string, string> = {}): string {
  return execFileSync(cmd, args, { encoding: 'utf8', env: { PATH: '/usr/bin:/bin', ...env }, timeout: 30_000 }).trim();
}


// ---------------------------------------------------------------------------------------------------------------- narration timing
const readJson = (f?: string) => (f ? JSON.parse(fs.readFileSync(f, 'utf8')) : null);
const INFO: Record<string, { audio: number; offsets: number[] }> | null = readJson(process.env.SCENE_INFO);
const MIN: Record<string, number> | null = readJson(process.env.SCENE_DURATIONS);
const LEAD = 0.25; // an on-screen change a little before the sentence it illustrates
const marks: Record<number, number> = {};
let curScene = 0;
let curT0 = 0;
/** Start a scene: closes the previous one (held until audio + 0.7 s have passed) and records the start time. */
async function scene(n: number) {
  await endScene();
  curScene = n;
  curT0 = Date.now();
  marks[n] = curT0;
}
async function endScene() {
  if (!curScene || !MIN) return;
  const need = MIN[String(curScene)] * 1000 - (Date.now() - curT0);
  if (need > 0) await sleep(need);
  else console.log(`scene ${curScene}: actions overran the minimum by ${(-need / 1000).toFixed(1)} s`);
}
/** Absolute time (ms) when sentence k of the current scene begins (a rehearsal: k seconds into the scene). */
function when(k: number): number {
  if (!INFO) return curT0 + k * 1500 * PACE;
  const off = INFO[String(curScene)].offsets[k];
  if (off === undefined) throw new Error(`scene ${curScene} has no narration sentence ${k}`);
  return curT0 + (off - LEAD) * 1000;
}
/** Wait until the narration sentence k of the current scene begins. */
async function at(k: number) {
  const wait = when(k) - Date.now();
  if (wait > 0) await sleep(wait);
  else if (wait < -1500) console.log(`scene ${curScene}: sentence ${k} reached ${(-wait / 1000).toFixed(1)} s late`);
}
const capOn = (page: Page, text: string) => page.evaluate((t) => (window as any).__caption(t), text);
const capOff = (page: Page) => page.evaluate(() => (window as any).__caption(''));
/** A caption from now until sentence k begins. */
async function capUntil(page: Page, text: string, k: number) {
  await capOn(page, text);
  await at(k);
  await capOff(page);
}
/** Outline every match of the locator until sentence k begins. The page re-renders its stats cards about once a second, so the
 *  outline is re-applied every 250 ms. Never fails the take: a missing element is only logged. */
async function hlUntil(loc: Locator, k: number) {
  const mark = () => loc.evaluateAll((es) => es.forEach((e) => e.classList.add('pw-hl'))).catch(() => {});
  try {
    await loc.first().scrollIntoViewIfNeeded({ timeout: 5000 });
  } catch (e) {
    console.log('highlight skipped:', String(e).split('\n')[0]);
  }
  while (Date.now() < when(k)) {
    await mark();
    await sleep(250);
  }
  await loc.evaluateAll((es) => es.forEach((e) => e.classList.remove('pw-hl'))).catch(() => {});
}
/** Scroll the page gently until sentence k begins (movement for a long explanation). */
async function drift(page: Page, k: number, step = 28) {
  let total = 0;
  while (Date.now() < when(k) && total < 900) {
    await page.mouse.wheel(0, step);
    total += step;
    await sleep(300);
  }
  await at(k);
}
const card = (page: Page, html: string) => page.evaluate((h) => (window as any).__card(h), html);
const escHtml = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');

// ---------------------------------------------------------------------------------------------------------------- the recording
test('VIDEO-SCRIPT.md scenes 1 to 9', async ({ page }) => {
  const svc = await startServices();
  try {
    await page.addInitScript(OVERLAY);
    const password = `Throwaway-${Math.random().toString(36).slice(2, 10)}-9`; // 12+ characters, never reused, typed into a masked field
    const readme = fs.readFileSync(path.join(REPO, 'README.md'), 'utf8');
    const flow = (readme.match(/```\n( sensors[\s\S]*?)```/) ?? [, ''])[1] as string;
    await page.goto(svc.url + '/');
    await expect(page.locator('#setup-code')).toBeVisible();

    // ---- scene 1: title card, then the README diagram (slow zoom) -------------------------------------------------------
    await scene(1);
    await card(page, '<h1>CityPulse Simulator</h1><p>A city of fake sensors and a stream of fake traffic, on demand</p>');
    await at(2);
    await card(page, '<p>sensors, field devices, edge gateway, CityPulse</p><pre id="pw-flow" style="font-size:17px;transition:transform 16s linear;transform-origin:50% 50%">' + escHtml(flow) + '</pre>');
    await page.evaluate(() => requestAnimationFrame(() => requestAnimationFrame(() => ((document.getElementById('pw-flow') as HTMLElement).style.transform = 'scale(1.14)'))));
    await capUntil(page, 'Sensors → field devices → edge gateway → CityPulse', 3);
    await capUntil(page, 'Also: payments, model calls, employee requests', 4);
    await capUntil(page, 'CityPulse Simulator', 5);

    // ---- scene 2: the setup code, then the setup form ---------------------------------------------------------------------
    await scene(2);
    await page.evaluate(() => (window as any).__cardOff());
    const logs = svc.logs();
    const banner = logs.slice(Math.max(0, logs.indexOf('=== CityPulse Simulator first-run setup')));
    const code = (banner.match(/\b([A-Z0-9]{4}(?:-[A-Z0-9]{4}){2})\b/) ?? [])[1] ?? (banner.match(/^[ \t]{2,}([A-Za-z0-9]{3,8}(?:-[A-Za-z0-9]{3,8}){1,4})[ \t]*$/m) ?? [])[1];
    expect(code, 'the setup code printed by cpsim serve').toBeTruthy();
    await terminal(page, 'Terminal A  —  cpsim serve', [{ cmd: 'cpsim serve', out: banner.split('\n').slice(0, 8).join('\n').trim() }], 0);
    await capUntil(page, 'One-time setup code', 3);
    await termOff(page);
    await capUntil(page, 'Postgres is the only dependency', 5);
    await typeSlow(page.locator('#setup-code'), code!);
    await typeSlow(page.locator('#setup-pw'), password, 60);
    await typeSlow(page.locator('#setup-pw2'), password, 60);
    await click(page, page.getByRole('button', { name: 'Create admin password' }));
    await expect(page.getByRole('link', { name: 'Cities' })).toBeVisible();
    await at(7);
    await capUntil(page, 'In the fleet: the shared identity provider', 8);
    await capOn(page, 'Roles: admin, operator, viewer');

    // ---- scene 3: the fixed demo city ----------------------------------------------------------------------------------------
    await scene(3);
    await capOff(page);
    await at(2);
    await typeSlow(page.locator('#nc-name'), 'demo-city');
    await click(page, page.getByRole('button', { name: 'Fixed demo city (always the same)' }));
    await expect(page.getByRole('heading', { name: 'demo-city' })).toBeVisible();
    const groups = page.locator('table').filter({ hasText: 'Protocol' }).first();
    await expect(groups).toBeVisible();
    await groups.scrollIntoViewIfNeeded();
    await at(3);
    await groups.scrollIntoViewIfNeeded();
    await drift(page, 5, 10);
    const cell = (t: string) => groups.locator('td', { hasText: new RegExp(`^${t}$`) });
    await hlUntil(groups.locator('th', { hasText: 'Protocol' }), 6);
    await hlUntil(groups.locator('th', { hasText: 'Protocol' }).or(cell('modbus tcp')).or(cell('ethernet ip')).or(cell('ethercat')), 7);
    await hlUntil(cell('profinet').or(cell('profibus')), 8);
    await hlUntil(cell('direct'), 9);
    await hlUntil(groups.locator('th', { hasText: /Burst/ }), 11);
    await capOn(page, '174 sensors, seed 42');
    await hlUntil(page.getByRole('heading', { name: /Sensor groups \(174 sensors\)/ }), 12);
    await capOff(page);
    await at(12);
    await click(page, page.getByRole('link', { name: /all cities/ }));
    await capOn(page, 'Same seed, same city');
    await hlUntil(page.getByText('Generate demo cities', { exact: true }), 14);
    await capOff(page);
    await click(page, page.getByRole('link', { name: 'demo-city' }).or(page.getByText('demo-city', { exact: true })).first());
    await expect(page.getByRole('heading', { name: 'demo-city' })).toBeVisible();
    await capOn(page, 'Up to 10,000 sensors');
    await at(15);
    await page.mouse.wheel(0, 300);
    await sleep(1000);
    await page.mouse.wheel(0, -2000);

    // ---- scene 4: start the city, read Modbus with a third-party client ------------------------------------------------------
    await scene(4);
    await at(0);
    await click(page, page.getByRole('button', { name: 'Start city' }));
    await expect(page.getByText('Delivery to CityPulse')).toBeVisible({ timeout: 30_000 });
    const modbusRow = page.locator('tr', { has: page.locator('td', { hasText: /^modbus tcp$/i }) }).first();
    await expect(modbusRow).toBeVisible({ timeout: 30_000 });
    const endpoint = ((await modbusRow.locator('code').first().innerText()) ?? '').trim(); // 127.0.0.1:<port>
    const modbusPort = endpoint.split(':').pop()!;
    const py = path.join(REPO, '.venv', 'bin', 'python');
    const oneLiner = `from pymodbus.client import ModbusTcpClient; c=ModbusTcpClient('127.0.0.1', port=${modbusPort}); print(c.read_input_registers(0, count=6, device_id=1).registers)`;
    // Sensors fall due every 10 to 15 s: until the first one has reported, the registers are all zero. Registers come in triples
    // (a float32 value in two registers, then an update counter), so six registers are two channels. Poll in the background.
    let registers = '';
    const ready = (async () => {
      const end = Date.now() + 90_000;
      while (Date.now() < end) {
        try {
          registers = run(py, ['-c', oneLiner]);
          if (/^\[(\d+, ){5}\d+\]$/.test(registers) && registers !== '[0, 0, 0, 0, 0, 0]') return true;
        } catch {
          /* not up yet */
        }
        await sleep(1500);
      }
      return false;
    })();
    await at(1);
    await capOn(page, 'Real servers on loopback');
    await hlUntil(modbusRow, 2);
    await capOff(page);
    await page.mouse.wheel(0, -2000);
    await at(5);
    await capUntil(page, 'Registers stay at zero until the first report', 6);
    await hlUntil(page.getByText('Readings sent').first(), 7);
    expect(await ready, 'the Modbus registers never became non-zero').toBeTruthy();
    await at(9);
    await terminal(page, 'Terminal B', [{ cmd: `python -c "${oneLiner}"`, out: registers }], 0);
    await capUntil(page, 'A third-party Modbus client reads the city', 11);
    await at(13);
    await termOff(page);
    await capUntil(page, 'No URL means generate only', 14);
    await page.mouse.wheel(0, -2000);
    await capUntil(page, 'With a URL: HTTPS, batches, a retry buffer', 17);
    await drift(page, 17, 20);

    // ---- scene 5: burst the whole city -----------------------------------------------------------------------------------------
    await scene(5);
    await capOff(page);
    await at(1);
    await page.locator('#storm-secs').scrollIntoViewIfNeeded();
    await at(2);
    await hlUntil(page.locator('#storm-secs'), 3);
    await page.locator('#storm-secs').selectOption({ label: '1 minute' });
    await hold(500);
    await click(page, page.getByRole('button', { name: 'BURST' }));
    await expect(page.getByText('Burst started')).toBeVisible();
    await capOn(page, 'Burst: spike, noise, stuck, dropout, flood, corrupt');
    await expect
      .poll(async () => /Bursts active\s*\n?\s*(?!none)\S/i.test(await page.locator('body').innerText()), { timeout: 60_000, intervals: [1000] })
      .toBeTruthy();
    await at(4);
    await capOff(page);
    await hlUntil(page.getByText('Bursts active').first(), 7);
    await capUntil(page, 'Corrupt values, silent updates, spikes, stuck values', 9);
    await capUntil(page, 'Burst % per group sets how often', 12);
    await capOn(page, 'Same seed, same bursts');

    // ---- scene 6: stop the city, a scheduled workload ----------------------------------------------------------------------------
    await scene(6);
    await capOff(page);
    await at(2);
    await at(3);
    await click(page, page.getByRole('button', { name: 'Stop city' }));
    await click(page, page.getByRole('link', { name: 'Workloads' }));
    await at(4);
    await typeSlow(page.locator('#nwl-name'), 'shop-telemetry');
    await page.locator('#nwl-kind').selectOption({ label: 'PagerLens telemetry' });
    await hold(500);
    await click(page, page.getByRole('button', { name: 'Create' }));
    await expect(page.getByRole('heading', { name: 'shop-telemetry' })).toBeVisible();
    await at(6);
    await click(page, page.getByRole('button', { name: 'Healthy baseline every 5 s' }));
    await click(page, page.getByRole('button', { name: 'Save and start' }));
    const banner6 = page.getByText('Generate only: no target URL, so nothing is sent.');
    await expect(banner6).toBeVisible();
    await at(7);
    await capOn(page, 'Scheduled traffic');
    await hlUntil(banner6, 9);
    await capOff(page);
    await expect.poll(async () => /generated/.test(await page.locator('body').innerText()), { timeout: 30_000 }).toBeTruthy();
    await at(10);
    await page.locator('#wl-scenario').selectOption({ label: 'bad deploy' });
    await hold(600);
    await click(page, page.getByRole('button', { name: 'Switch scenario' }));
    await expect(page.getByText('Scenario: bad deploy')).toBeVisible();
    await capUntil(page, 'A failure on purpose', 13);
    await capOn(page, 'Kinds: PayBroker, ShopFront, LLM gateway, PagerLens, TrustDesk, identity');
    await hlUntil(page.getByText('ticks', { exact: true }).first(), 15);
    await capOn(page, 'Paced on a steady clock, with a hard rate ceiling');
    await hlUntil(page.getByText('skipped ticks', { exact: true }).first(), 17);

    // ---- scene 7: a company, preview ---------------------------------------------------------------------------------------------
    await scene(7);
    await capOff(page);
    await at(1);
    await click(page, page.getByRole('button', { name: 'Stop', exact: true }));
    await click(page, page.getByRole('link', { name: 'Companies' }));
    await at(2);
    await typeSlow(page.locator('#nco-name'), 'acme');
    await at(4);
    await click(page, page.getByRole('button', { name: 'Quick check (about 60 requests)' }));
    await expect(page.getByRole('heading', { name: 'acme' })).toBeVisible();
    await at(5);
    await click(page, page.getByRole('button', { name: 'Preview' }));
    await expect(page.getByRole('heading', { name: /^Preview: / })).toBeVisible();
    await capOn(page, 'Preview sends nothing');
    await hlUntil(page.getByRole('heading', { name: /^Preview: / }), 7);
    await hlUntil(page.getByRole('columnheader', { name: /^PLANNED$/i }).or(page.getByRole('columnheader', { name: /^PROBLEM$/i })), 8);
    await capOff(page);
    await hlUntil(page.getByRole('columnheader', { name: /^PROBLEM$/i }), 10);
    await hlUntil(page.getByText('Problem requests (%)').first(), 11);
    await capOn(page, 'All names are invented, on example.com');
    await at(11);

    // ---- scene 8: settings, metrics, health --------------------------------------------------------------------------------------
    await scene(8);
    await capOff(page);
    await at(0);
    await click(page, page.getByRole('link', { name: 'Settings' }));
    await expect(page.locator('#fleet-table')).toBeVisible();
    await at(1);
    await capOn(page, 'ecoctl injects the fleet addresses');
    await hlUntil(page.locator('#fleet-table'), 5);
    await capUntil(page, 'Locally every address says: not set', 6);
    await capOn(page, 'Secrets are never stored in a city or workload');
    await hlUntil(page.getByRole('heading', { name: /Credentials for the apps it drives/ }), 8);
    await capOff(page);
    await hlUntil(page.locator('#resume-on-start'), 9);
    const metrics = run('/usr/bin/curl', ['-s', '-H', `Authorization: Bearer ${METRICS_TOKEN}`, `${svc.url}/metrics`]);
    expect(metrics).toContain('cpsim_workloads_running');
    const wr = metrics.split('\n').filter((l) => l.startsWith('cpsim_workloads_running'));
    const health = run('/usr/bin/curl', ['-s', `${svc.url}/healthz`]);
    expect(health).toContain('"status":"ok"');
    await terminal(page, 'Terminal B', [{ cmd: `curl -s -H "Authorization: Bearer ${METRICS_TOKEN}" ${svc.url}/metrics | grep cpsim_workloads_running`, out: wr.join('\n') }], 0);
    await capOn(page, '/metrics');
    await at(12);
    await terminal(page, 'Terminal B', [{ cmd: `curl -s ${svc.url}/healthz`, out: health }], 0, true);
    await capUntil(page, '/healthz', 14);
    await termOff(page);
    await page.locator('#fleet-table').scrollIntoViewIfNeeded();
    await capOn(page, 'Roles: admin, operator, viewer');
    await drift(page, 17, 30);

    // ---- scene 9: recap and the end card -------------------------------------------------------------------------------------------
    await scene(9);
    await capOff(page);
    await page.evaluate(() => ((document.getElementById('pw-cursor') as HTMLElement).style.display = 'none'));
    const recap = ['Cities of sensors on real industrial protocols', 'Bursts on demand', 'Scheduled traffic for the other apps', 'Simulated employee requests for TrustDesk'];
    const recapCard = (k: number) => card(page, '<h1 style="font-size:64px">Recap</h1>' + recap.slice(0, k).map((t) => `<p style="font-size:42px;color:#fff">${t}</p>`).join(''));
    await recapCard(0);
    for (let k = 1; k <= 4; k++) {
      await at(k);
      await recapCard(k);
    }
    await at(5);
    await card(page, '<p>One app: roles, a vault and metrics. It needs only Postgres.</p><pre>' + escHtml(flow) + '</pre>');
    await at(8);
    await card(page, '<h1>docs/GUIDE.md</h1><p style="color:#ffd23f">cpsim serve</p><p>Run it, then point it at the fleet and watch the other apps come alive.</p>');
    await at(11);
    await card(page, '<h1>docs/GUIDE.md</h1><p style="color:#ffd23f">cpsim serve</p><p>Thanks for watching</p>');
    await endScene();
    await hold(1000);
    if (process.env.TIMINGS_OUT) fs.writeFileSync(process.env.TIMINGS_OUT, JSON.stringify({ scene_start_ms: marks, end_ms: Date.now() }));
    await page.evaluate(() => (window as any).__cardOff());
    const video = page.video();
    await page.close(); // the video file is finalized when the page closes
    const target = path.join(__dirname, 'out', 'cpsim-video-script.webm');
    await video?.saveAs(target);
    console.log('recorded:', target);
  } finally {
    await svc.stop();
  }
});
