// The "Generate demo cities" form on the Cities page: it posts what was chosen and shows a summary table of what was created.
// Run:  node tests/ui/cities_generate.mjs        (no browser, no server: a tiny fake DOM and a stubbed fetch)
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
globalThis.location = { origin: "http://x", hash: "#cities", search: "", pathname: "/" };

const catalog = { types: [{ key: "noise", label: "Noise", unit: "dB", lo: 20, hi: 140 }], districts: [], protocols: ["direct", "modbus_tcp"], limits: { city_sensors: 10000, group_count: 5000 },
  generator: { sizes: { small: 200, medium: 2000, large: 20000 }, profiles: ["balanced", "industrial", "coastal", "tourist", "residential", "mixed"], max_count: 50 } };
const created = { created: 2, sensors: 2300, cities: [
  { name: "tide-port", seed: 5, sensors: 1200, groups: 9, types: 8, protocols: ["direct", "profinet"], districts: [{ id: "alder-quay", name: "Alder Quay", kind: "harbour" }, { id: "linden-heights", name: "Linden Heights", kind: "residential" }] },
  { name: "alder-vale", seed: 6, sensors: 1100, groups: 7, types: 6, protocols: ["modbus_tcp"], districts: [{ id: "north-mill", name: "North Mill", kind: "industrial" }] }] };
const calls = [];
globalThis.fetch = async (p, init = {}) => {
  calls.push({ p, method: init.method || "GET", body: init.body });
  const city = { name: "tide-port", target_url: "", token_env: "CITYPULSE_INGEST_TOKEN", seed: 5, poll_interval: 2, expose: false, autostart: false, running: false,
  groups: [{ type: "noise", count: 3, district: "alder-quay", protocol: "direct", frequency: 30, burst_pct: 5 }] };
  const body = p === "/api/catalog" ? catalog : p === "/api/cities/tide-port" ? city : p === "/api/cities/generate" ? created : [];
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { initCities } = await import("/static/cities.js");
await initCities();

function El_defaultSelect() { const c = document.createElement; document.createElement = (t) => { const e = c(t); if (t === "select") e.value = "noise"; return e; }; }
const find = (el, pred) => (pred(el) ? el : (el.children || []).map((c) => (c && c.children ? find(c, pred) : null)).find(Boolean));
const byId = (id) => find(document.getElementById("view-cities"), (e) => e.attrs?.id === id);
const text = (el) => (el.data ?? "") + (el.textContent ?? "") + (el.children || []).map(text).join(" ");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

const form = byId("gen-form");
if (!form) fail("the generate form was not rendered");
const view = text(document.getElementById("view-cities"));
if (!/Generate demo cities/.test(view)) fail("no 'Generate demo cities' heading");
if (!/Fixed presets/.test(view) || !/Fixed demo city/.test(view)) fail("the fixed presets are not labelled as fixed");
const profile = byId("gen-profile");
if (profile.children.length !== 6 || !profile.children.some((o) => o.value === "mixed" && o.attrs.selected !== undefined)) fail("profiles missing, or mixed is not the default");
if (byId("gen-size").children.length !== 3) fail("sizes missing");

byId("gen-count").value = "2"; byId("gen-size").value = "small"; byId("gen-profile").value = "coastal"; byId("gen-seed").value = "5"; byId("gen-name").value = "";
await form.listeners.submit[0]({ preventDefault() {} });
const post = calls.find((c) => c.p === "/api/cities/generate");
if (!post || post.method !== "POST") fail("no POST to /api/cities/generate");
const sent = JSON.parse(post.body);
if (sent.count !== 2 || sent.size !== "small" || sent.profile !== "coastal" || sent.seed !== 5 || "name" in sent) fail("wrong body " + post.body);
const table = byId("gen-summary");
if (!table) fail("no summary table after generating");
const t = text(table);
for (const want of ["tide-port", "alder-vale", "1,200", "Alder Quay (harbour), Linden Heights (residential)", "North Mill (industrial)", "profinet"]) if (!t.includes(want)) fail(`summary lacks ${want}`);
if (find(table, (e) => e.tag === "tr" && e.listeners.click) === undefined) fail("rows should open the city");
El_defaultSelect();
await initCities(["tide-port"]);                  // a city page offers its own (generated) districts, not only d01..d12
const dist = byId("g-district");
if (!dist || dist.tag !== "input" || dist.attrs.list !== "g-district-list") fail("the district is not a free id input with suggestions");
if (dist.value !== "alder-quay" || !byId("g-district-list").children.some((o) => o.value === "alder-quay")) fail("a generated district is not suggested");
console.log("ok: the generate form posts its choices and summarises the created cities");
process.exit(0);
