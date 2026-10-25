// Regression: revoking a token, deleting a saved credential and deleting a city ask through the kit's confirmDialog (never a native
// confirm()). The dialog lives under <body>, so a refresh or a redraw cannot wipe it or the phrase typed into it; deleting a city
// needs its name typed. Run:  node tests/ui/delete_dialogs.mjs        (no browser, no server: a tiny fake DOM and a stubbed fetch)
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

let active = null;
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this.dataset = {}; this.value = ""; this.listeners = {}; this.style = {}; this.parent = null; this.disabled = false; this.className = ""; this.id = ""; this.textContent = ""; this.classes = new Set(); this.classList = { toggle() {}, add: (c) => this.classes.add(c), remove: (c) => this.classes.delete(c) }; }
  get nodeType() { return 1; }
  append(...k) { for (const x of k) { if (x.nodeType === 1) x.parent = this; this.children.push(x); } }
  replaceChildren(...k) { this.children = []; this.append(...k); }
  setAttribute(k, v) { this.attrs[k] = v; if (k === "id") this.id = v; }
  addEventListener(t, f) { (this.listeners[t] ||= []).push(f); }
  fire(t, ev = {}) { const e = { target: this, preventDefault() {}, ...ev }; for (let n = this; n; n = n.parent) for (const f of n.listeners[t] || []) f(e); }
  async click() { if (!this.disabled) await Promise.all((this.listeners.click || []).map((f) => f({ target: this, preventDefault() {} }))); }
  focus() { if (!this.disabled) active = this; }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); this.parent = null; }
  contains(n) { return n === this || this.children.some((c) => c.contains && c.contains(n)); }
  find(pred) { if (pred(this)) return this; for (const c of this.children) { const f = c.find && c.find(pred); if (f) return f; } return null; }
  text() { return this.textContent + this.children.map((c) => (c.text ? c.text() : c.data ?? "")).join(" "); }
  querySelector() { return null; } querySelectorAll() { return []; } matches() { return false; }
}
const body = new El("body");
const els = {};
globalThis.document = { body, documentElement: { dataset: { csrf: "X-CPSim-CSRF" } }, getElementById: (id) => (els[id] ||= new El("#" + id)),
  createElement: (t) => { const e = new El(t); if (t === "select") e.value = "noise"; return e; }, createElementNS: (_, t) => new El(t), createTextNode: (d) => ({ nodeType: 3, data: String(d), text: () => String(d) }), querySelectorAll: () => [] };
Object.defineProperty(document, "activeElement", { get: () => active });
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class {};
globalThis.confirm = globalThis.alert = globalThis.prompt = () => { throw new Error("a native dialog was used"); };
globalThis.location = { origin: "http://x", hash: "#settings", search: "", pathname: "/" };

const city = { name: "tide-port", target_url: "", token_env: "CITYPULSE_INGEST_TOKEN", seed: 5, poll_interval: 2, expose: false, autostart: false, running: false,
  groups: [{ type: "noise", count: 3, district: "alder-quay", protocol: "direct", frequency: 30, burst_pct: 5 }] };
const tokens = [{ id: 7, name: "grafana", prefix: "cps_ab", scope: "read", created_at: "2026-01-01T00:00:00Z", last_used_at: null, revoked_at: null }];
const creds = [{ name: "GATEWAY_KEY", saved: true, source: "saved", used_by: [] }];
const calls = [];
globalThis.fetch = async (p, init = {}) => {
  const m = init.method || "GET";
  calls.push(`${m} ${p}`);
  const reply = { "/api/session": { has_admin_password: true }, "/api/tokens": tokens, "/api/credentials": { unlocked: true, items: creds },
    "/api/run-settings": { resume_on_start: true, default: true, stagger_seconds: 1 }, "/api/catalog": { types: [{ key: "noise", label: "Noise", unit: "dB", lo: 20, hi: 140 }], districts: [], protocols: ["direct"], limits: { city_sensors: 10000, group_count: 5000 } },
    "/api/cities/tide-port": city };
  const b = m === "GET" ? (reply[p] ?? []) : {};
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => b, text: async () => JSON.stringify(b) };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };

const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const tick = () => new Promise((r) => setTimeout(r, 10));
const overlay = () => body.find((e) => e.className === "confirm-overlay");
const dbtn = (label) => overlay().find((e) => e.tag === "button" && e.text() === label);
const deletes = () => calls.filter((c) => c.startsWith("DELETE"));
const press = (view, label) => view.find((e) => e.tag === "button" && e.text() === label);
let pending;
const open = async (view, label) => { pending = press(view, label).click(); await tick(); if (!overlay()) fail(`${label} opened no dialog`); };

// Settings: token revoke and saved-credential delete
const { initSettings } = await import("/static/settings.js");
await initSettings();
await tick();
const settings = document.getElementById("view-settings");

await open(settings, "Revoke");
if (!overlay().text().includes('Revoke "grafana"? Clients using it stop working immediately.')) fail("revoke message changed: " + overlay().text());
if (dbtn("Revoke").className !== "danger-solid") fail("revoke is not a danger dialog");
if (settings.contains(overlay())) fail("the dialog must live under <body>, outside the page that redraws");
await dbtn("Cancel").click(); await pending;
if (overlay() || deletes().length) fail("Cancel must close the dialog and delete nothing");
await open(settings, "Revoke");
await dbtn("Revoke").click(); await pending; await tick();
if (deletes().join() !== "DELETE /api/tokens/7") fail("confirming revokes exactly once: " + deletes());

await open(settings, "Delete");
if (!overlay().text().includes("Delete the saved GATEWAY_KEY? Workloads that use it stop working.")) fail("credential message changed");
overlay().fire("keydown", { key: "Escape" }); await pending;
if (overlay() || deletes().length !== 1) fail("Escape cancels");
await open(settings, "Delete");
await dbtn("Delete").click(); await pending; await tick();
if (deletes().pop() !== "DELETE /api/credentials/GATEWAY_KEY") fail("credential delete not sent");

// A city: the name must be typed, and typing survives the page redrawing (the live stats timer, a re-render of the view)
globalThis.location.hash = "#cities";
const { initCities, stopCities } = await import("/static/cities.js");
await initCities(["tide-port"]);
const cities = document.getElementById("view-cities");
await open(cities, "Delete city");
const input = overlay().find((e) => e.tag === "input");
if (!input || dbtn("Delete city").disabled !== true) fail("deleting a city must ask for its name");
input.value = "tide"; input.fire("input");
await initCities(["tide-port"]);                               // the page redraws behind the dialog
if (overlay().find((e) => e.tag === "input") !== input || input.value !== "tide") fail("the typed name must survive a redraw");
if (!dbtn("Delete city").disabled) fail("a partial name must not arm the button");
const before = deletes().length;
input.value = "tide-port"; input.fire("input");
await dbtn("Delete city").click(); await pending; await tick();
if (deletes().length !== before + 1 || deletes().pop() !== "DELETE /api/cities/tide-port") fail("the typed name should delete the city");
stopCities();
console.log("ok: revoke, credential delete and city delete use the kit dialog and survive redraws");
process.exit(0);
