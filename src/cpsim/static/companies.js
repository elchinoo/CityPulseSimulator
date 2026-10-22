import { api } from "/kit/api.js";
import { guard } from "/static/perm.js";
import { $, field, fill, h, toast } from "/kit/ui.js";

let catalog = null;
let timer = null;
const KINDS = { leave: "Leave", travel: "Travel", expense: "Expenses", other: "Other requests" };
const STATE = { idle: "offline", running: "ok", done: "ok", stopped: "offline", failed: "bad" };

export function stopCompanies() { clearInterval(timer); }

// ---- the list ------------------------------------------------------------------------------------
async function renderList(box) {
  let list;
  try { list = await api("/api/companies"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const name = h("input", { id: "nco-name", required: true, maxlength: "40", placeholder: "acme", pattern: "[a-z0-9][a-z0-9-]*" });
  const err = h("p", { class: "error", role: "alert" });
  const create = async (preset) => {
    err.textContent = "";
    if (!name.value) { err.textContent = "Type a name first."; return; }
    const spec = preset ? { employees: preset.employees, requests: preset.requests } : { employees: { new: 5, tenured: 20 }, requests: {} };
    try { await api(`/api/companies/${name.value}`, { method: "PUT", json: spec }); location.hash = `#companies/${name.value}`; } catch (e) { err.textContent = e.message; }
  };
  const rows = list.map((c) => h("tr", { class: "clickable", onclick: () => { location.hash = `#companies/${c.name}`; } },
    h("td", { class: "title" }, c.name), h("td", { class: "num" }, c.employees.toLocaleString()), h("td", { class: "num" }, c.requests.toLocaleString()),
    h("td", { class: "num" }, c.types), h("td", { class: "muted" }, c.target_url || "generate only"),
    h("td", {}, h("span", { class: `st ${STATE[c.state] || "offline"}` }, c.state))));
  fill(box, h("h1", {}, "Companies"),
    h("p", { class: "muted" }, "A company is a simulated employer. It creates employees (new hires and long-serving staff) and sends the number of requests of each type that you choose to TrustDesk, as a client application would."),
    list.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Company", "Employees", "Requests", "Types", "Sends to", "State"].map((t) => h("th", {}, t)))), h("tbody", {}, ...rows)))
      : h("div", { class: "card" }, h("p", { class: "empty" }, "No companies yet. Start from a preset such as “Hundreds of mixed requests”.")),
    h("h2", { class: "section-title" }, "New company"),
    h("div", { class: "card" }, field("Company name", name, "Lowercase letters, digits and dashes."),
      h("div", { class: "btns wrap" }, h("button", { class: "secondary", type: "button", onclick: () => create(null) }, "Create empty"),
        ...catalog.presets.map((p) => h("button", { class: "primary", type: "button", onclick: () => create(p) }, p.label))), err));
  guard(box);
}

// ---- one company -----------------------------------------------------------------------------------
function progress(s) {
  const t = s.totals;
  const kv = (k, v) => [h("dt", {}, k), h("dd", {}, String(v))];
  const rows = Object.entries(s.types).map(([k, c]) => h("tr", {}, h("td", {}, catalog.types.find((x) => x.key === k)?.label || k),
    ...["planned", "sent", "accepted", "invalid", "errors", "withdrawn"].map((f) => h("td", { class: "num" + (f === "invalid" || f === "errors" ? (c[f] ? " bad" : "") : "") }, c[f]))));
  const pct = t.planned ? Math.round((100 * t.sent) / t.planned) : 0;
  const st = Object.entries(s.statuses || {});
  return h("div", {},
    s.fatal ? h("div", { class: "banner bad" }, s.fatal) : null,
    s.unknown_types?.length ? h("div", { class: "banner" }, `The target does not know these request types, so they will be refused: ${s.unknown_types.join(", ")}`) : null,
    h("div", { class: "card" }, h("div", { class: "row" }, h("h3", {}, `${s.state}${s.dry_run ? " (generate only, nothing is sent)" : ""}`),
      h("span", { class: "muted" }, `${t.sent.toLocaleString()} of ${t.planned.toLocaleString()} sent · ${s.rate}/s · ${s.elapsed}s${s.seed != null ? ` · seed ${s.seed}` : ""}`)),
      h("progress", { max: "100", value: String(pct), class: "wide-progress" }),
      h("dl", { class: "facts" }, ...kv("Accepted", t.accepted), ...kv("Refused as invalid", t.invalid), ...kv("Other errors", t.errors), ...kv("Withdrawn by the employee", t.withdrawn),
        ...kv("Built to break a policy", `${t.problem_planned} of ${t.planned}`),
        ...kv("Employees", `${s.employees.created} created, ${s.employees.existing} already there, ${s.employees.failed} failed`)),
      st.length ? h("p", {}, "Where TrustDesk has them now: ", ...st.flatMap(([k, n]) => [h("span", { class: "badge" }, `${k.replace("_", " ")} ${n}`), " "]),
        h("span", { class: "muted" }, ` (sample of ${s.status_sample})`)) : null),
    h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Request type", "Planned", "Sent", "Accepted", "Invalid", "Errors", "Withdrawn"].map((x) => h("th", { class: x === "Request type" ? "" : "num" }, x)))), h("tbody", {}, ...rows))),
    s.errors.length ? h("div", { class: "card" }, h("h3", {}, "First problems"), h("ul", {}, ...s.errors.map((e) => h("li", {}, h("strong", {}, e.what), `: ${e.message}`)))) : null);
}

function previewPanel(p) {
  const rows = Object.entries(p.types).map(([k, c]) => h("tr", {}, h("td", {}, catalog.types.find((x) => x.key === k)?.label || k),
    h("td", { class: "num" }, c.planned), h("td", { class: "num" }, c.problem),
    h("td", {}, h("code", {}, JSON.stringify(p.samples[k]?.[0] || {}).slice(0, 170)))));
  return h("div", { class: "card flush" }, h("h3", { class: "pad" }, `Preview: ${p.employees} employees (${p.new_hires} new hires), ${p.requests} requests, ${p.problem} built to break a policy · seed ${p.seed}`),
    h("table", {}, h("thead", {}, h("tr", {}, ...["Type", "Planned", "Problem", "Example"].map((x) => h("th", {}, x)))), h("tbody", {}, ...rows)));
}

async function renderCompany(box, name) {
  let c;
  try { c = await api(`/api/companies/${name}`); } catch (err) { fill(box, h("p", { class: "error" }, err.message), h("a", { href: "#companies" }, "← all companies")); return; }
  const running = c.state === "running";
  const schedRunning = c.schedule_run?.state === "running";
  const err = h("p", { class: "error", role: "alert" });
  const url = h("input", { id: "co-url", value: c.target_url, disabled: running, placeholder: "https://your-trustdesk… (empty: generate only)" });
  const tokenEnv = h("input", { id: "co-token", value: c.token_env, disabled: running, maxlength: "64" });
  const num = (id, v, attrs = {}) => h("input", { id, type: "number", value: v ?? "", disabled: running, ...attrs });
  const fNew = num("co-new", c.employees.new, { min: "0", max: String(catalog.limits.employees) });
  const fTen = num("co-ten", c.employees.tenured, { min: "0", max: String(catalog.limits.employees) });
  const fUse = h("input", { id: "co-use", type: "checkbox", checked: c.employees.use_existing, disabled: running });
  const fSeed = num("co-seed", c.seed, { min: "0", placeholder: "random each run" });
  const fProb = num("co-prob", c.problem_pct, { min: "0", max: "100" });
  const fWd = num("co-wd", c.withdraw_pct, { min: "0", max: "30" });
  const fRate = num("co-rate", c.rate, { min: "0.1", max: "200", step: "0.1" });
  const fConc = num("co-conc", c.concurrency, { min: "1", max: "16" });
  const fPrefix = h("input", { id: "co-prefix", value: c.employee_prefix, disabled: running, maxlength: "8" });

  const sc = c.schedule || {};
  const fSchedOn = h("input", { id: "co-sched-on", type: "checkbox", checked: !!c.schedule, disabled: running || schedRunning });
  const fEvery = num("co-sched-every", sc.interval ?? 60, { min: "5", max: "86400", step: "1" });
  const fJit = num("co-sched-jit", sc.jitter_pct ?? 0, { min: "0", max: "50" });
  const fDur = num("co-sched-dur", sc.duration, { min: "1", placeholder: "forever" });
  const fRuns = num("co-sched-runs", sc.runs, { min: "1", placeholder: "forever" });
  const fBoot = h("input", { id: "co-sched-boot", type: "checkbox", checked: !!sc.start_on_boot, disabled: running || schedRunning });
  for (const el of [fEvery, fJit, fDur, fRuns]) el.disabled = running || schedRunning;

  const counts = {};
  const total = h("strong", {}, "0");
  const recount = () => { total.textContent = Object.values(counts).reduce((n, el) => n + (Number(el.value) || 0), 0).toLocaleString(); };
  const grid = Object.entries(KINDS).map(([kind, title]) => h("fieldset", { class: "kind" }, h("legend", {}, title),
    ...catalog.types.filter((t) => t.kind === kind).map((t) => {
      const el = h("input", { type: "number", min: "0", max: String(catalog.limits.per_type), value: c.requests[t.key] || 0, disabled: running, "aria-label": t.label });
      el.addEventListener("input", recount);
      counts[t.key] = el;
      return h("label", { class: "count-row" }, h("span", {}, t.label), el);
    })));
  recount();

  const spec = () => ({
    target_url: url.value.trim(), token_env: tokenEnv.value.trim(), seed: fSeed.value === "" ? null : Number(fSeed.value),
    employees: { new: Number(fNew.value), tenured: Number(fTen.value), use_existing: fUse.checked },
    requests: Object.fromEntries(Object.entries(counts).map(([k, el]) => [k, Number(el.value) || 0]).filter(([, n]) => n > 0)),
    schedule: fSchedOn.checked ? { interval: Number(fEvery.value), jitter_pct: Number(fJit.value), duration: fDur.value === "" ? null : Number(fDur.value),
      runs: fRuns.value === "" ? null : Number(fRuns.value), start_on_boot: fBoot.checked } : null,
    problem_pct: Number(fProb.value), withdraw_pct: Number(fWd.value), rate: Number(fRate.value), concurrency: Number(fConc.value),
    employee_prefix: fPrefix.value.trim(),
  });
  const save = async (quiet) => { err.textContent = ""; try { await api(`/api/companies/${name}`, { method: "PUT", json: spec() }); if (!quiet) toast("Saved"); return true; } catch (e) { err.textContent = e.message; return false; } };
  const preset = (p) => { fNew.value = p.employees.new; fTen.value = p.employees.tenured; for (const [k, el] of Object.entries(counts)) el.value = p.requests[k] || 0; recount(); };

  const live = h("div", {});
  const preview = h("div", {});
  const draw = async () => {
    try {
      const st = await api(`/api/companies/${name}/status`);
      fill(live, progress(st));
      if (running && st.state !== "running") renderCompany(box, name);   // the run ended: unlock the form and buttons
    } catch { fill(live); }
  };
  const act = (path, msg) => async () => {
    err.textContent = "";
    if (path === "run" && !(await save(true))) return;
    try { await api(`/api/companies/${name}/${path}`, { method: "POST" }); if (msg) toast(msg); renderCompany(box, name); } catch (e) { err.textContent = e.message; }
  };
  const actSched = (path, msg) => async () => {
    err.textContent = "";
    if (path === "start" && !(await save(true))) return;
    try { await api(`/api/companies/${name}/schedule/${path}`, { method: "POST" }); toast(msg); renderCompany(box, name); } catch (e) { err.textContent = e.message; }
  };
  const refresh = async (e) => {
    e.target.disabled = true;
    try { await api(`/api/companies/${name}/refresh`, { method: "POST" }); await draw(); } catch (ex) { err.textContent = ex.message; } finally { e.target.disabled = false; }
  };
  const doPreview = async () => {
    err.textContent = "";
    if (!(await save(true))) return;
    try { fill(preview, previewPanel(await api(`/api/companies/${name}/preview`, { method: "POST" }))); } catch (e) { err.textContent = e.message; }
  };

  const form = h("form", { onsubmit: async (e) => { e.preventDefault(); await save(); } },
    h("div", { class: "grid2" }, field("TrustDesk URL", url, "https, or http only for this machine. Empty means generate only: nothing is sent."),
      field("Token variable", tokenEnv, "The NAME of the environment variable that holds a TrustDesk token with the Client application scope. The token itself is never stored here.")),
    h("h3", {}, "Employees"),
    h("div", { class: "grid3" }, field("New hires (last ~75 days)", fNew, "They test probation rules."), field("Tenured (1 to 15 years)", fTen, "They build leave and sick history."),
      field("Employee prefix", fPrefix, "Numbers look like SIM12345-0001.")),
    h("label", { class: "check", for: "co-use" }, fUse, " Also use employees that already exist in TrustDesk"),
    h("h3", {}, h("span", {}, "Requests to send "), h("span", { class: "muted" }, "(total "), total, h("span", { class: "muted" }, ")")),
    h("div", { class: "btns wrap tight" }, h("span", { class: "muted" }, "Presets:"), ...catalog.presets.map((p) => h("button", { class: "secondary small", type: "button", disabled: running, onclick: () => preset(p) }, p.label))),
    h("div", { class: "kinds" }, ...grid),
    h("h3", {}, "Behaviour"),
    h("div", { class: "grid3" }, field("Problem requests (%)", fProb, "How many are built to break a typical policy: too little notice, over a limit, no receipt, overlapping leave, duplicates."),
      field("Withdrawn after sending (%)", fWd), field("Random seed", fSeed, "Same seed, same people and requests.")),
    h("div", { class: "grid3" }, field("Requests per second (at most)", fRate), field("Parallel connections", fConc)),
    h("h3", {}, "Repeat on a schedule"),
    h("label", { class: "check", for: "co-sched-on" }, fSchedOn, " Run this company again and again (recurring requests for TrustDesk)"),
    h("div", { class: "grid3" }, field("Every (seconds between run starts)", fEvery, "5 to 86400. A run that is still going makes the next tick skip."),
      field("Jitter (%)", fJit), field("Stop after (seconds)", fDur, "Empty: forever.")),
    h("div", { class: "grid3" }, field("Stop after (runs)", fRuns, "Empty: forever.")),
    h("label", { class: "check", for: "co-sched-boot" }, fBoot, " Start the schedule when the app starts"),
    h("div", { class: "btns" }, h("button", { class: "primary", type: "submit", disabled: running || schedRunning }, "Save")));

  fill(box, h("a", { href: "#companies" }, "← all companies"), h("h1", {}, c.name),
    h("div", { class: "card burst-big" },
      running ? h("button", { class: "secondary", type: "button", onclick: act("stop", "Stopping") }, "Stop")
        : h("button", { class: "primary", type: "button", onclick: act("run", "Started") }, "Save and run"),
      h("button", { class: "secondary", type: "button", disabled: running, "data-ro-ok": "1", onclick: doPreview }, "Preview"),
      h("button", { class: "secondary", type: "button", onclick: refresh }, "Refresh TrustDesk statuses"),
      schedRunning ? h("button", { class: "secondary", type: "button", onclick: actSched("stop", "Schedule stopped") }, "Stop schedule")
        : h("button", { class: "secondary", type: "button", disabled: running || !c.schedule, onclick: actSched("start", "Schedule started") }, "Start schedule"),
      h("span", { class: "muted" }, running ? "Running: settings are locked until it stops." : "Preview shows what would be sent without sending anything.")),
    c.schedule_run ? h("p", { class: "muted" }, `Schedule ${c.schedule_run.state}: ${c.schedule_run.ticks} runs started, ${c.schedule_run.skipped} ticks skipped, ${c.schedule_run.items} requests`
      + (c.schedule_run.last_error ? `, last error: ${c.schedule_run.last_error}` : "") + (c.schedule_run.fatal ? `, stopped: ${c.schedule_run.fatal}` : "")) : null,
    err, live, preview,
    h("h2", { class: "section-title" }, "What to send"), h("div", { class: "card" }, form),
    h("div", { class: "btns" }, h("button", { class: "danger", type: "button", onclick: async () => { if (!confirm(`Delete ${name}?`)) return; await api(`/api/companies/${name}`, { method: "DELETE" }); location.hash = "#companies"; } }, "Delete company")));
  guard(box);
  await draw();
  clearInterval(timer);
  timer = setInterval(() => { if (!location.hash.startsWith(`#companies/${name}`)) clearInterval(timer); else draw(); }, running ? 1500 : 5000);
}

export async function initCompanies(arg) {
  const box = $("view-companies");
  clearInterval(timer);
  if (!catalog) catalog = await api("/api/hr/catalog");
  if (arg && arg[0]) await renderCompany(box, arg[0]); else await renderList(box);
}
