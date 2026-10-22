import { api } from "/kit/api.js";
import { guard } from "/static/perm.js";
import { fleetChoice, targetText, withFleet } from "/static/fleet.js";
import { $, field, fill, h, toast } from "/kit/ui.js";

let catalog = null;
let timer = null;
const STATE = { idle: "offline", running: "ok", done: "ok", stopped: "offline", failed: "bad" };
// Outcome classes that mean something is wrong; everything else (declines, fraud rejections, rate limits...) is an expected result.
const BAD = new Set(["auth_error", "client_error", "server_error", "unavailable", "network_error", "generator_error"]);

export function stopWorkloads() { clearInterval(timer); }

const fleetLabel = (name, fleet = []) => fleet.find((f) => f.name === name)?.label || name;
const kindOf = (key) => catalog.kinds.find((k) => k.key === key);
const ms = (v) => (v == null ? "n/a" : `${Math.round(v * 1000)} ms`);
const getPath = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function setPath(obj, path, value) {
  const keys = path.split(".");
  const last = keys.pop();
  let o = obj;
  for (const k of keys) o = o[k] ||= {};
  o[last] = value;
}

// ---- the list ------------------------------------------------------------------------------------
function listTable(list) {
  const rows = list.map((w) => {
    const s = w.stats;
    return h("tr", { class: "clickable", onclick: () => { location.hash = `#workloads/${w.name}`; } },
      h("td", { class: "title" }, w.name), h("td", {}, kindOf(w.kind)?.label || w.kind), h("td", { class: "muted" }, targetText(w)),
      h("td", { class: "num" }, `${w.batch} / ${w.interval} s`),
      h("td", {}, h("span", { class: `st ${STATE[w.state] || "offline"}` }, w.state)),
      h("td", { class: "num" }, s ? s.items.toLocaleString() : "0"), h("td", { class: "num" }, s ? s.skipped : "0"),
      h("td", { class: "num" }, s ? ms(s.latency_p95) : "n/a"),
      h("td", { class: "wrap" + (s && s.last_error ? " bad" : "") }, s ? s.last_error : ""));
  });
  return list.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Workload", "Kind", "Sends to", "Batch / every", "State", "Items", "Skipped", "p95", "Last error"].map((t) => h("th", {}, t)))), h("tbody", {}, ...rows)))
    : h("div", { class: "card" }, h("p", { class: "empty" }, "No workloads yet. Create one below, for example a PayBroker payment stream every 2 seconds."));
}

// Only the table is redrawn on a timer: the "New workload" form below it must keep what the person is typing.
async function renderList(box) {
  let list;
  try { list = await api("/api/workloads"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const tableBox = h("div", {});
  tableBox.append(listTable(list));
  const name = h("input", { id: "nwl-name", required: true, maxlength: "40", placeholder: "shop-traffic", pattern: "[a-z0-9][a-z0-9-]*" });
  const kind = h("select", { id: "nwl-kind" }, ...catalog.kinds.map((k) => h("option", { value: k.key }, k.label)));
  const err = h("p", { class: "error", role: "alert" });
  const create = async () => {
    err.textContent = "";
    if (!name.value) { err.textContent = "Type a name first."; return; }
    const k = kindOf(kind.value);
    const spec = { kind: k.key, schedule: k.schedule, params: {} };
    if (k.key === "trustdesk") {
      try {
        const companies = await api("/api/companies");
        if (!companies.length) { err.textContent = "Create a company under Companies first: this workload runs one on a schedule."; return; }
        spec.params.company = companies[0].name;
      } catch (e) { err.textContent = e.message; return; }
    }
    try { await api(`/api/workloads/${name.value}`, { method: "PUT", json: spec }); location.hash = `#workloads/${name.value}`; } catch (e) { err.textContent = e.message; }
  };
  fill(box, h("h1", {}, "Workloads"),
    h("p", { class: "muted" }, "A workload pushes simulated traffic to one of the other apps on a schedule: every 2 or 5 seconds, say. "
      + "Pick the kind, set the interval, how many items per tick and what to send. Cities stream to CityPulse on their own schedule (their poll interval and sensor frequency)."),
    tableBox,
    h("h2", { class: "section-title" }, "New workload"),
    h("div", { class: "card" }, h("div", { class: "grid2" }, field("Name", name, "Lowercase letters, digits and dashes."), field("Kind", kind)),
      h("div", { class: "btns" }, h("button", { class: "primary", type: "button", onclick: create }, "Create")), err));
  guard(box);
  clearInterval(timer);
  timer = setInterval(async () => {
    if (location.hash !== "#workloads") { clearInterval(timer); return; }
    try { tableBox.replaceChildren(listTable(await api("/api/workloads"))); } catch { /* the next tick tries again */ }
  }, 3000);
}

// ---- live numbers of one workload ---------------------------------------------------------------------
function kpi(label, value, cls = "") { return h("div", { class: `kpi ${cls}` }, h("b", {}, String(value)), h("span", {}, label)); }

function live(s) {
  const total = Object.values(s.outcomes).reduce((a, b) => a + b, 0) || 1;
  const outcomes = Object.entries(s.outcomes).sort((a, b) => b[1] - a[1]);
  const extra = Object.entries(s.extra || {});
  return h("div", {},
    s.fatal ? h("div", { class: "banner bad" }, `Stopped: ${s.fatal}`) : null,
    s.backing_off ? h("div", { class: "banner" }, "The target is pushing back (429 or 503): backing off before the next tick.") : null,
    s.dry_run ? h("div", { class: "banner" }, "Generate only: no target URL, so nothing is sent.") : null,
    h("div", { class: "kpis" }, kpi("state", s.state, s.state === "failed" ? "bad" : s.state === "running" ? "ok" : ""), kpi("ticks", s.ticks.toLocaleString()),
      kpi("skipped ticks", s.skipped, s.skipped ? "warn" : ""), kpi("items sent", s.items.toLocaleString()), kpi("items / s", s.rate),
      kpi("latency p50", ms(s.latency_p50)), kpi("latency p95", ms(s.latency_p95)),
      kpi("next tick in", s.next_tick_in == null ? "n/a" : `${s.next_tick_in} s`), kpi("errors", s.errors_total, s.errors_total ? "bad" : "")),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Outcome"), h("th", { class: "num" }, "Items"), h("th", { class: "num" }, "Share"))),
      h("tbody", {}, ...(outcomes.length ? outcomes.map(([k, n]) => h("tr", {}, h("td", { class: BAD.has(k) ? "bad" : "" }, k.replace(/_/g, " ")), h("td", { class: "num" }, n.toLocaleString()),
        h("td", { class: "num" }, `${Math.round((100 * n) / total)}%`))) : [h("tr", {}, h("td", { colspan: "3", class: "muted" }, "Nothing sent yet."))])))),
    extra.length ? h("p", { class: "muted" }, extra.map(([k, v]) => `${k.replace(/_/g, " ")}: ${v}`).join(" · ")) : null,
    s.skipped_backoff ? h("p", { class: "muted" }, `${s.skipped_backoff} of the skipped ticks were backoff, the rest found the previous batch still running.`) : null,
    s.recent_errors.length ? h("div", { class: "card" }, h("h3", {}, "Last errors"), h("ul", {}, ...s.recent_errors.slice().reverse().map((e) => h("li", {}, h("strong", {}, e.outcome.replace(/_/g, " ")), `: ${e.message}`)))) : null);
}

// ---- one workload ----------------------------------------------------------------------------------
async function renderWorkload(box, name) {
  let w;
  try { w = await api(`/api/workloads/${name}`); } catch (err) { fill(box, h("p", { class: "error" }, err.message), h("a", { href: "#workloads" }, "← all workloads")); return; }
  const kind = kindOf(w.kind);
  const running = w.state === "running";
  const err = h("p", { class: "error", role: "alert" });
  const inp = (id, v, attrs = {}) => h("input", { id, type: "number", value: v ?? "", disabled: running, ...attrs });

  const url = h("input", { id: "wl-url", value: w.target_url, disabled: running, placeholder: kind.url_hint || "https://…  (empty: generate only)" });
  const tokenEnv = kind.token_env ? h("input", { id: "wl-token", value: w.token_env, disabled: running, maxlength: "64" }) : null;
  const pubEnv = w.kind === "paybroker" ? h("input", { id: "wl-pub", value: w.publishable_env || "", disabled: running, maxlength: "64", placeholder: "optional, e.g. PAYBROKER_PUBLISHABLE_KEY" }) : null;
  const s = w.schedule;
  const lim = catalog.limits;
  const fInterval = inp("wl-interval", s.interval, { min: String(kind.min_interval), max: String(lim.interval[1]), step: "0.1" });
  const fJitter = inp("wl-jitter", s.jitter_pct, { min: "0", max: "50", step: "1" });
  const fBatch = inp("wl-batch", s.batch, { min: "1", max: String(kind.max_batch) });
  const fConc = inp("wl-conc", s.concurrency, { min: "1", max: "64" });
  const fDur = inp("wl-dur", s.duration, { min: "1", placeholder: "forever" });
  const fTotal = inp("wl-total", s.total, { min: "1", placeholder: "forever" });
  const fTicks = inp("wl-ticks", s.ticks, { min: "1", placeholder: "forever" });
  const fBoot = h("input", { id: "wl-boot", type: "checkbox", checked: s.start_on_boot, disabled: running });

  const params = {};
  const paramFields = kind.fields.map((f) => {
    const v = getPath(w.params, f.key);
    let el;
    if (f.type === "bool") el = h("input", { id: `wp-${f.key}`, type: "checkbox", checked: !!v, disabled: running });
    else if (f.type === "select") el = h("select", { id: `wp-${f.key}`, disabled: running }, ...f.options.map((o) => h("option", { value: o, selected: o === (v ?? "") }, o || "default")));
    else if (f.type === "number") el = h("input", { id: `wp-${f.key}`, type: "number", value: v ?? "", disabled: running, min: f.min ?? false, max: f.max ?? false, step: f.step ?? "any" });
    else el = h("input", { id: `wp-${f.key}`, value: Array.isArray(v) ? v.join(", ") : (v ?? ""), disabled: running, maxlength: "300" });
    params[f.key] = el;
    return field(f.label, el, f.hint);
  });

  const spec = () => {
    const p = {};
    for (const f of kind.fields) {
      const el = params[f.key];
      if (f.type === "bool") setPath(p, f.key, el.checked);
      else if (f.type === "number") { if (el.value !== "") setPath(p, f.key, Number(el.value)); }
      else if (f.type === "list") setPath(p, f.key, el.value.split(",").map((x) => x.trim()).filter(Boolean));
      else if (el.value !== "" || f.type === "text") setPath(p, f.key, el.value);
    }
    const num = (el) => (el.value === "" ? null : Number(el.value));
    const out = {
      kind: w.kind, target_url: url.value.trim(), params: p,
      schedule: { interval: Number(fInterval.value), jitter_pct: Number(fJitter.value), batch: Number(fBatch.value), concurrency: Number(fConc.value),
        duration: num(fDur), total: num(fTotal), ticks: num(fTicks), start_on_boot: fBoot.checked },
    };
    if (tokenEnv) out.token_env = tokenEnv.value.trim();
    if (pubEnv && pubEnv.value.trim()) out.publishable_env = pubEnv.value.trim();
    return out;
  };
  const save = async (quiet) => { err.textContent = ""; try { await api(`/api/workloads/${name}`, { method: "PUT", json: spec() }); if (!quiet) toast("Saved"); return true; } catch (e) { err.textContent = e.message; return false; } };
  const preset = (p) => {
    fInterval.value = p.schedule.interval ?? fInterval.value;
    fBatch.value = p.schedule.batch ?? fBatch.value;
    for (const f of kind.fields) {
      const v = getPath(p.params, f.key);
      if (v === undefined) continue;
      if (f.type === "bool") params[f.key].checked = !!v; else params[f.key].value = Array.isArray(v) ? v.join(", ") : v;
    }
  };

  const box2 = h("div", {});
  const draw = async () => {
    try {
      const st = await api(`/api/workloads/${name}/status`);
      fill(box2, live(st));
      if (running && st.state !== "running") renderWorkload(box, name);   // it ended: unlock the form and buttons
    } catch { fill(box2); }
  };
  const act = (path, msg) => async () => {
    err.textContent = "";
    if ((path === "start" || path === "run-once") && !(await save(true))) return;
    try { await api(`/api/workloads/${name}/${path}`, { method: "POST" }); if (msg) toast(msg); renderWorkload(box, name); } catch (e) { err.textContent = e.message; }
  };
  const scenario = w.kind === "pagerlens" && running
    ? (() => {
      const sel = h("select", { id: "wl-scenario", "aria-label": "Failure scenario" }, ...kindOf("pagerlens").fields.find((f) => f.key === "scenario").options.map((o) => h("option", { value: o }, o.replace(/_/g, " "))));
      return h("span", { class: "btns tight" }, sel, h("button", { class: "secondary", type: "button", onclick: async () => {
        try { await api(`/api/workloads/${name}/scenario`, { method: "POST", json: { scenario: sel.value } }); toast(`Scenario: ${sel.value.replace(/_/g, " ")}`); await draw(); } catch (e) { err.textContent = e.message; }
      } }, "Switch scenario"));
    })() : null;

  const form = h("form", { onsubmit: async (e) => { e.preventDefault(); await save(); } },
    h("p", { class: "muted" }, kind.description),
    h("div", { class: "btns wrap tight" }, h("span", { class: "muted" }, "Presets:"), ...kind.presets.map((p) => h("button", { class: "secondary small", type: "button", disabled: running, onclick: () => preset(p) }, p.label))),
    h("div", { class: "grid2" }, w.kind === "trustdesk" ? null : withFleet(field("Target URL", url, "https, or http only for this machine. Empty means generate only: nothing is sent. Or follow the fleet: the address is looked up when the workload starts."),
      catalog.kind_vars?.[w.kind] ? fleetChoice(url, catalog.kind_vars[w.kind], fleetLabel(catalog.kind_vars[w.kind], catalog.fleet), catalog.fleet || [], running) : h("span", {})),
      tokenEnv ? field("Credential variable", tokenEnv, `The NAME of the environment variable that holds the credential. ${kind.env_hint} The value itself is never stored here.`) : h("p", { class: "hint" }, kind.env_hint)),
    pubEnv ? field("Publishable key variable", pubEnv, "Tokenize cards with a publishable key, like a browser would. The name only.") : null,
    h("h3", {}, "Schedule"),
    h("div", { class: "grid3" }, field("Every (seconds)", fInterval, `${kind.min_interval} to ${lim.interval[1]}. For example 2 or 5.`), field("Jitter (%)", fJitter, "Each tick fires up to this share of the interval early or late."),
      field("Items per tick", fBatch, `1 to ${kind.max_batch}. Ceiling: ${lim.max_rate} items/s in all.`)),
    h("div", { class: "grid3" }, field("Parallel requests", fConc), field("Stop after (seconds)", fDur, "Empty: forever."), field("Stop after (items)", fTotal, "Empty: forever.")),
    h("div", { class: "grid3" }, field("Stop after (ticks)", fTicks, "Empty: forever.")),
    h("label", { class: "check", for: "wl-boot" }, fBoot, " Start this workload when the app starts"),
    paramFields.length ? h("h3", {}, "What to send") : null,
    h("div", { class: "grid3" }, ...paramFields),
    h("div", { class: "btns" }, h("button", { class: "primary", type: "submit", disabled: running }, "Save")));

  fill(box, h("a", { href: "#workloads" }, "← all workloads"), h("h1", {}, w.name), h("p", { class: "muted" }, kind.label),
    h("div", { class: "card burst-big" },
      running ? h("button", { class: "secondary", type: "button", onclick: act("stop", "Stopping") }, "Stop")
        : h("button", { class: "primary", type: "button", onclick: act("start", "Started") }, "Save and start"),
      h("button", { class: "secondary", type: "button", disabled: running, onclick: act("run-once", "One batch sent") }, "Run once"),
      scenario,
      h("span", { class: "muted" }, running ? "Running: settings are locked until it stops." : "Run once sends a single batch right now and stops.")),
    err, box2,
    h("h2", { class: "section-title" }, "Settings"), h("div", { class: "card" }, form),
    h("div", { class: "btns" }, h("button", { class: "danger", type: "button", onclick: async () => { if (!confirm(`Delete ${name}?`)) return; await api(`/api/workloads/${name}`, { method: "DELETE" }); location.hash = "#workloads"; } }, "Delete workload")));
  guard(box);
  await draw();
  clearInterval(timer);
  timer = setInterval(() => { if (!location.hash.startsWith(`#workloads/${name}`)) clearInterval(timer); else draw(); }, running ? 1000 : 5000);
}

export async function initWorkloads(arg) {
  const box = $("view-workloads");
  clearInterval(timer);
  if (!catalog) catalog = await api("/api/workloads/catalog");
  if (arg && arg[0]) await renderWorkload(box, arg[0]); else await renderList(box);
}
