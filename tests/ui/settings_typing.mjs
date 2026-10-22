// Regression: saving or deleting one credential redraws the Credentials table; what is being typed in the other rows must survive.
// Run:  node tests/ui/settings_typing.mjs        (no browser, no server: a tiny fake DOM and a stubbed fetch)
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
globalThis.location = { origin: "http://x", hash: "#settings", search: "", pathname: "/" };
const items = [{ name: "PAYBROKER_KEY", saved: false, source: "none", used_by: ["pay"] }, { name: "GATEWAY_KEY", saved: true, source: "saved", used_by: [] }];
const calls = [];
globalThis.fetch = async (p, init = {}) => {
  calls.push(`${init.method || "GET"} ${p}`);
  const body = p === "/api/session" ? { has_admin_password: true } : p === "/api/credentials" ? { unlocked: true, items } : [];
  return { ok: true, status: 200, headers: { get: () => "application/json" }, json: async () => body, text: async () => JSON.stringify(body) };
};
const { state } = await import("/kit/session.js");
state.session = { state: "logged_in", user: { permissions: ["*"] } };
const { initSettings } = await import("/static/settings.js");
await initSettings();
await new Promise((r) => setTimeout(r, 50));       // the tables load after the page is drawn

const walk = (el, out = []) => { out.push(el); (el.children || []).forEach((c) => c && c.children && walk(c, out)); return out; };
const text = (el) => (el.data ?? "") + (el.textContent ?? "") + (el.children || []).map(text).join(" ");
const row = (name) => walk(document.getElementById("view-settings")).find((e) => e.tag === "tr" && text(e).includes(name) && walk(e).some((x) => x.tag === "input"));
const inputOf = (name) => walk(row(name)).find((e) => e.tag === "input");
const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };

const a = inputOf("PAYBROKER_KEY"), b = inputOf("GATEWAY_KEY");
if (!a || !b) fail("the credential rows were not rendered");
a.value = "sk_live_a"; (a.listeners.input || []).forEach((f) => f());
b.value = "half-typed-b"; (b.listeners.input || []).forEach((f) => f());      // typing in a second row...
const save = walk(row("PAYBROKER_KEY")).find((e) => e.tag === "button" && text(e) === "Save");
await save.listeners.click[0]();                                              // ...while the first one is saved (which redraws the table)
await new Promise((r) => setTimeout(r, 50));
if (!calls.includes("PUT /api/credentials/PAYBROKER_KEY")) fail("the save did not happen");
const b2 = inputOf("GATEWAY_KEY");
if (!b2 || b2.value !== "half-typed-b") fail("redrawing the table lost what was typed in another row");
if (inputOf("PAYBROKER_KEY").value !== "") fail("the saved value should be cleared");
console.log("ok: typing in one credential row survives saving another");
process.exit(0);
