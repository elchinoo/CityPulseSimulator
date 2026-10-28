// The Database tab: app.js mounts the kit's initInsights into #view-database; the panel renders the payload of GET /api/insights, with an
// honest "not available" state per source on plain Postgres. Run:  node tests/ui/insights_tab.mjs   (no browser, no server)
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
  focus() {} querySelector() { return null; } querySelectorAll() { return []; } matches() { return false; }
}
const els = {};
globalThis.document = { documentElement: { dataset: { csrf: "X-CPSim-CSRF" } }, getElementById: (id) => (els[id] ||= new El("#" + id)),
  createElement: (t) => new El(t), createElementNS: (_, t) => new El(t), createTextNode: (d) => ({ nodeType: 3, data: d }), querySelectorAll: () => [] };
globalThis.window = { dispatchEvent() {}, addEventListener() {} };
globalThis.CustomEvent = class {};
globalThis.location = { origin: "http://x", hash: "#database", search: "", pathname: "/" };

const populated = {
  sources: { edb_stat_monitor: { available: true, version: "2.1" } },
  top_queries: { available: true, source: "edb_stat_monitor", order: "total", since: null, text_hidden: 0,
    queries: [{ queryid: "1", query: "SELECT spec FROM cpsim_city WHERE name = $1", calls: 10, total_ms: 50, mean_ms: 5, max_ms: 9, rows: 10, since: null }] },
  tuner: { available: true, recommendations: [{ setting: "shared_buffers", value: "186 MB", raw: "shared_buffers = '186 MB'" }], global_stats: null },
  advisor: { available: true, suggestions: [] },
};
const plain = { sources: {}, top_queries: { available: false, reason: "no statistics extension" },
  tuner: { available: false, reason: "edb_pg_tuner is not installed" }, advisor: { available: false, reason: "query_advisor is not installed" } };
let payload = populated;
const calls = [];
globalThis.fetch = async (p) => {
  calls.push(p);
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => payload, text: async () => JSON.stringify(payload) };
};
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
const text = (el) => (el.data ?? "") + (el.textContent ?? "") + (el.children || []).map(text).join(" ");

const { api } = await import("/kit/api.js");
const { initInsights } = await import("/kit/insights.js");
const root = document.getElementById("view-database");
await initInsights(root, api);
if (!calls.includes("/api/insights?limit=50")) fail("the tab calls GET /api/insights?limit=50, got " + calls.join(","));
if (!text(root).includes("cpsim_city") || !text(root).includes("shared_buffers")) fail("top queries and tuner are shown");
payload = plain;
await initInsights(root, api);
if (!text(root).includes("Not available on this database.") || !text(root).includes("edb_pg_tuner is not installed")) fail("a not-available state with the reason");
const app = (await import("node:fs")).readFileSync(path.join(STATIC, "app.js"), "utf8");
if (!app.includes('database: "settings:manage"') || !app.includes("initInsights")) fail("app.js wires the Database tab for admins");
console.log("ok insights tab");
