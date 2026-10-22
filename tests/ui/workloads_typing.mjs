// Regression: the Workloads list refreshes every few seconds; the "New workload" form below it must keep what is being typed.
// Run:  node tests/ui/workloads_typing.mjs        (no browser, no server: a tiny fake DOM and a stubbed fetch)
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
  createElement: (t) => new El(t), createElementNS: (_, t) => new El(t), createTextNode: (d) => ({ nodeType: 3, data: d }), querySelectorAll: () => [] };
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class {};
globalThis.location = { origin: "http://x", hash: "#workloads", search: "", pathname: "/" };
const kinds = [{ key: "paybroker", label: "PayBroker payments", schedule: {}, fields: [], presets: [], min_interval: 0.2, max_batch: 500 }];
globalThis.fetch = async (p) => {
  const body = p.endsWith("/catalog") ? { kinds, limits: { interval: [0.2, 3600], max_rate: 200 } } : [];
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };

const realSetInterval = globalThis.setInterval;
const ticks = [];
globalThis.setInterval = (f) => { ticks.push(f); return ticks.length; };
const { initWorkloads } = await import("/static/workloads.js");
await initWorkloads();

const find = (el, id) => (el.attrs?.id === id ? el : (el.children || []).map((c) => find(c, id)).find(Boolean));
const input = find(document.getElementById("view-workloads"), "nwl-name");
if (!input) { console.error("FAIL: the name input was not rendered"); process.exit(1); }
input.value = "batman-sho";
for (const t of ticks) await t();                 // the refresh timer fires, twice
await new Promise((r) => setTimeout(r, 50));       // a refresh is not awaited by the timer
const after = find(document.getElementById("view-workloads"), "nwl-name");
if (after !== input || after.value !== "batman-sho") { console.error("FAIL: the refresh replaced the form and lost what was typed"); process.exit(1); }
console.log("ok: the typed name survives the list refresh");
globalThis.setInterval = realSetInterval;
process.exit(0);
