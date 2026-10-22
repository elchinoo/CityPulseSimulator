// The "Use the fleet's ..." choice next to a Target URL (city, company, workload): one click puts the reference in, shows what it resolves to,
// and never wipes what was typed in the other fields. Run:  node tests/ui/fleet_choice.mjs        (no browser, no server: a tiny fake DOM and a stubbed fetch)        (no browser, no server: a tiny fake DOM and a stubbed fetch)
import { pathToFileURL } from "node:url";
import { register } from "node:module";
import path from "node:path";

const here = path.dirname(new URL(import.meta.url).pathname);
const STATIC = path.resolve(here, "../../src/cpsim/static");
const KIT = path.resolve(here, "../../../agentpg-kit/src/apgkit/static");
const loader = `export async function resolve(s, c, n) {
  if (s.startsWith("/kit/")) return { url: ${JSON.stringify(pathToFileURL(KIT).href)} + "/" + s.slice(5), shortCircuit: true };
  if (s.startsWith("/static/")) return { url: ${JSON.stringify(pathToFileURL(STATIC).href)} + "/" + s.slice(8), shortCircuit: true };
  return n(s, c);
}`;
register("data:text/javascript," + encodeURIComponent(loader));

class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.value = ""; this.listeners = {}; this.classList = { toggle() {}, add() {}, remove() {} }; this.style = {}; this.textContent = ""; }
  append(...k) { this.children.push(...k); }
  replaceChildren(...k) { this.children = k; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  get nodeType() { return 1; }
  querySelector() { return null; } querySelectorAll() { return []; } matches() { return false; }
}
const els = {};
globalThis.document = { documentElement: { dataset: { csrf: "X-CPSim-CSRF" } }, getElementById: (id) => (els[id] ||= new El("#" + id)),
  createElement: (t) => new El(t), createElementNS: (_, t) => new El(t), createTextNode: (d) => ({ nodeType: 3, data: String(d) }), querySelectorAll: () => [] };
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class {};
globalThis.location = { origin: "http://x", hash: "#workloads/shop", search: "", pathname: "/" };

const FLEET = ["CITYPULSE_URL", "PAYBROKER_URL", "TRUSTDESK_URL", "PAGERLENS_URL", "LLMGW_URL", "IDENTITY_URL"]
  .map((name) => ({ name, label: { CITYPULSE_URL: "CityPulse", PAYBROKER_URL: "PayBroker", TRUSTDESK_URL: "TrustDesk", PAGERLENS_URL: "PagerLens", LLMGW_URL: "LLM gateway", IDENTITY_URL: "identity provider" }[name], set: name !== "LLMGW_URL", value: name === "LLMGW_URL" ? "" : `https://${name.split("_")[0].toLowerCase()}.fleet.example` }));
const kinds = ["paybroker", "llmgw", "pagerlens", "trustdesk", "identity"].map((key) => ({ key, label: key, schedule: {}, fields: [], presets: [], min_interval: 0.2, max_batch: 500, token_env: key === "paybroker" ? "PAYBROKER_SECRET_KEY" : "" }));
const kindVars = { paybroker: "PAYBROKER_URL", llmgw: "LLMGW_URL", pagerlens: "PAGERLENS_URL", trustdesk: "TRUSTDESK_URL", identity: "IDENTITY_URL" };
const reply = {
  "/api/workloads/catalog": { kinds, kind_vars: kindVars, fleet: FLEET, limits: { interval: [0.2, 3600], max_rate: 200 } },
  "/api/workloads/shop": { name: "shop", kind: "paybroker", target_url: "https://old.example", token_env: "PAYBROKER_SECRET_KEY", params: {}, state: "idle",
    schedule: { interval: 2, jitter_pct: 0, batch: 3, concurrency: 4, duration: null, total: null, ticks: null, start_on_boot: false } },
  "/api/workloads/gw": { name: "gw", kind: "llmgw", target_url: "", token_env: "", params: {}, state: "idle",
    schedule: { interval: 2, jitter_pct: 0, batch: 3, concurrency: 4, duration: null, total: null, ticks: null, start_on_boot: false } },
  "/api/catalog": { types: [{ key: "noise", label: "Noise", unit: "dB", lo: 20, hi: 140 }], districts: [], protocols: ["direct"], limits: { city_sensors: 10000, group_count: 5000 },
    generator: { sizes: { small: 200 }, profiles: ["mixed"], max_count: 50 }, fleet: FLEET, defaults: { city_target_url: "${CITYPULSE_URL}" } },
  "/api/cities/tide": { name: "tide", target: { reference: null }, target_url: "https://half.example", token_env: "CITYPULSE_INGEST_TOKEN", seed: 5, poll_interval: 2, expose: false, autostart: false, running: false,
    groups: [{ type: "noise", count: 3, district: "d01", protocol: "direct", frequency: 30, burst_pct: 5 }] },
  "/api/hr/catalog": { types: [], presets: [], departments: [], limits: { employees: 2000, per_type: 5000, total: 20000 }, fleet: FLEET },
  "/api/companies/acme": { name: "acme", target_url: "", token_env: "TRUSTDESK_CLIENT_TOKEN", seed: null, employees: { new: 1, tenured: 1, use_existing: false }, requests: {}, problem_pct: 25, withdraw_pct: 3,
    rate: 5, concurrency: 4, employee_prefix: "SIM", schedule: null, state: "idle", schedule_run: null },
};
reply["/api/session"] = { has_admin_password: true };
reply["/api/fleet"] = { vars: FLEET };
reply["/api/credentials"] = { unlocked: true, items: [] };
const calls = [];
globalThis.fetch = async (p, init = {}) => {
  calls.push({ p, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null });
  const body = reply[p] ?? [];
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const ticks = [];
globalThis.setInterval = (f) => { ticks.push(f); return ticks.length; };

const walk = (el, out = []) => { out.push(el); (el.children || []).forEach((c) => c && c.children && walk(c, out)); return out; };
const text = (el) => (el.data ?? "") + (el.textContent ?? "") + (el.children || []).map(text).join(" ");
const byId = (view, id) => walk(document.getElementById(view)).find((e) => e.attrs?.id === id);
const choice = (view) => walk(document.getElementById(view)).filter((e) => e.attrs?.["data-fleet-choice"]);
const note = (view) => walk(document.getElementById(view)).find((e) => e.attrs?.["data-fleet-note"]);
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const type = (el, v) => { el.value = v; (el.listeners.input || []).forEach((f) => f()); };

// ---- a workload: PayBroker offers PayBroker, and only that -------------------------------------------------
const { initWorkloads } = await import("/static/workloads.js");
await initWorkloads(["shop"]);
await new Promise((r) => setTimeout(r, 20));
let buttons = choice("view-workloads");
if (buttons.length !== 1 || buttons[0].attrs["data-fleet-choice"] !== "PAYBROKER_URL") fail("a paybroker workload should offer exactly the PayBroker choice");
if (!/Use the fleet's PayBroker/.test(text(buttons[0]))) fail("the button should read: Use the fleet's PayBroker, got " + text(buttons[0]));
const url = byId("view-workloads", "wl-url"), token = byId("view-workloads", "wl-token");
if (url.value !== "https://old.example") fail("a literal URL must still be shown as typed");
type(token, "MY_OWN_KEY");                                  // typed before the click ...
url.value = "https://half-typed";                           // ... as is this
for (const t of ticks) await t();
await new Promise((r) => setTimeout(r, 20));
if (byId("view-workloads", "wl-token") !== token || token.value !== "MY_OWN_KEY" || url.value !== "https://half-typed") fail("a refresh lost what was typed");
await buttons[0].listeners.click[0]();
if (url.value !== "${PAYBROKER_URL}") fail("the click should put ${PAYBROKER_URL} in the field, got " + url.value);
if (token.value !== "MY_OWN_KEY" || byId("view-workloads", "wl-token") !== token) fail("the click wiped another field");
if (!/\$\{PAYBROKER_URL\} → https:\/\/paybroker\.fleet\.example/.test(text(note("view-workloads")))) fail("the resolved address should be shown: " + text(note("view-workloads")));
type(url, "${LLMGW_URL}");
if (!/LLMGW_URL\} is not set on this app/.test(text(note("view-workloads")))) fail("an unset reference should say so: " + text(note("view-workloads")));
type(url, "${PAYBROKER_URL}");
const save = walk(document.getElementById("view-workloads")).find((e) => e.tag === "button" && text(e).trim() === "Save");
const form = walk(document.getElementById("view-workloads")).find((e) => e.tag === "form" && walk(e).includes(save));
await form.listeners.submit[0]({ preventDefault() {} });
const put = calls.find((c) => c.method === "PUT" && c.p === "/api/workloads/shop");
if (!put || put.body.target_url !== "${PAYBROKER_URL}" || put.body.token_env !== "MY_OWN_KEY") fail("the saved spec should carry the reference and the typed credential name: " + JSON.stringify(put));
type(url, "https://typed.example");
if (text(note("view-workloads")).trim() !== "") fail("a literal URL has no resolution line");
await initWorkloads(["gw"]);
buttons = choice("view-workloads");
if (buttons.length !== 1 || buttons[0].attrs["data-fleet-choice"] !== "LLMGW_URL") fail("an llmgw workload should offer the gateway choice");
console.log("ok: workload choice");

// ---- a city -----------------------------------------------------------------------------------------------
const mk = document.createElement;
document.createElement = (t) => { const e = mk(t); if (t === "select") e.value = "noise"; return e; };      // the fake DOM selects nothing by itself
const { initCities } = await import("/static/cities.js");
globalThis.location.hash = "#cities/tide";
await initCities(["tide"]);
await new Promise((r) => setTimeout(r, 20));
buttons = choice("view-cities");
if (buttons.length !== 1 || buttons[0].attrs["data-fleet-choice"] !== "CITYPULSE_URL" || !/Use the fleet's CityPulse/.test(text(buttons[0]))) fail("a city should offer the CityPulse choice");
const curl = byId("view-cities", "c-url"), ctoken = byId("view-cities", "c-token");
type(ctoken, "MY_TOKEN");
await buttons[0].listeners.click[0]();
if (curl.value !== "${CITYPULSE_URL}" || ctoken.value !== "MY_TOKEN") fail("city choice: " + curl.value + " / " + ctoken.value);
if (!/→ https:\/\/citypulse\.fleet\.example/.test(text(note("view-cities")))) fail("the city should show the address");
console.log("ok: city choice");

// ---- a company ---------------------------------------------------------------------------------------------
const { initCompanies } = await import("/static/companies.js");
globalThis.location.hash = "#companies/acme";
await initCompanies(["acme"]);
await new Promise((r) => setTimeout(r, 20));
buttons = choice("view-companies");
if (buttons.length !== 1 || buttons[0].attrs["data-fleet-choice"] !== "TRUSTDESK_URL" || !/Use the fleet's TrustDesk/.test(text(buttons[0]))) fail("a company should offer the TrustDesk choice");
const co = byId("view-companies", "co-url"), cotoken = byId("view-companies", "co-token");
type(cotoken, "MY_CLIENT_TOKEN");
await buttons[0].listeners.click[0]();
if (co.value !== "${TRUSTDESK_URL}" || cotoken.value !== "MY_CLIENT_TOKEN") fail("company choice: " + co.value);
console.log("ok: company choice");

// ---- the read-only Fleet card on Settings ------------------------------------------------------------------
const { initSettings } = await import("/static/settings.js");
await initSettings();
await new Promise((r) => setTimeout(r, 30));
const table = byId("view-settings", "fleet-table");
if (!table) fail("Settings has no Fleet card");
const rows = walk(table).filter((e) => e.tag === "tr").map(text);
if (rows.length !== 7) fail("the Fleet card should list the six variables, got " + rows.length + " rows");
if (!rows.some((r) => /LLMGW_URL/.test(r) && /not set/.test(r)) || !rows.some((r) => /CITYPULSE_URL/.test(r) && /https:\/\/citypulse\.fleet\.example/.test(r))) fail("set and unset variables are not shown right: " + rows.join(" | "));
if (walk(table).some((e) => e.tag === "input" || e.tag === "button")) fail("the Fleet card is read-only");
console.log("ok: fleet card");
process.exit(0);
