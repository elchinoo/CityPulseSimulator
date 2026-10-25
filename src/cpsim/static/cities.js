import { api } from "/kit/api.js";
import { guard } from "/static/perm.js";
import { fleetChoice, targetText, withFleet } from "/static/fleet.js";
import { $, confirmDialog, field, fill, h, toast } from "/kit/ui.js";

let catalog = null;
let timer = null;
const label = (p) => p.replace("_", " ");

// The CityPulse "workload": every sensor sends a reading every `interval` seconds, on `direct` (no protocol sockets). A city's schedule is its
// poll interval (how often the gateway polls the devices) and each group's frequency (seconds between readings of one sensor).
const LIVE = (name, interval) => ({
  name, seed: 7, poll_interval: interval,
  groups: [["temperature", 12, "d01"], ["humidity", 6, "d01"], ["air_quality", 9, "d06"], ["noise", 6, "d08"], ["water_pressure", 6, "d04"],
    ["smart_meter", 9, "d05"], ["traffic_light", 6, "d03"], ["rain", 6, "d07"]].map(([type, count, district]) => ({ type, count, district, protocol: "direct", frequency: interval, burst_pct: 5 })),
});

const DEMO = (name) => ({
  name, seed: 42, poll_interval: 2,
  groups: [
    { type: "temperature", count: 24, district: "d01", protocol: "modbus_tcp", frequency: 10, burst_pct: 10 },
    { type: "humidity", count: 16, district: "d02", protocol: "modbus_tcp", frequency: 10, burst_pct: 10 },
    { type: "traffic_light", count: 20, district: "d03", protocol: "ethernet_ip", frequency: 10, burst_pct: 5 },
    { type: "radar", count: 12, district: "d03", protocol: "ethernet_ip", frequency: 10, burst_pct: 15 },
    { type: "water_pressure", count: 16, district: "d04", protocol: "ethercat", frequency: 10, burst_pct: 10 },
    { type: "smart_meter", count: 20, district: "d05", protocol: "profinet", frequency: 10, burst_pct: 10 },
    { type: "gas_leak", count: 8, district: "d05", protocol: "profinet", frequency: 10, burst_pct: 5 },
    { type: "air_quality", count: 16, district: "d06", protocol: "profibus", frequency: 15, burst_pct: 20 },
    { type: "co2", count: 12, district: "d06", protocol: "profibus", frequency: 15, burst_pct: 10 },
    { type: "rain", count: 10, district: "d07", protocol: "direct", frequency: 15, burst_pct: 10 },
    { type: "light", count: 10, district: "d07", protocol: "direct", frequency: 15, burst_pct: 10 },
    { type: "noise", count: 10, district: "d08", protocol: "direct", frequency: 15, burst_pct: 10 },
  ],
});

// ---- the generator: varied demo cities (same seed, same city; another seed, another city) ----------------------
function generatorCard(cat) {
  const gen = cat.generator || { sizes: { small: 200, medium: 2000, large: 20000 }, profiles: ["mixed"], max_count: 50 };
  const count = h("input", { id: "gen-count", type: "number", min: "1", max: gen.max_count, value: "1" });
  const size = h("select", { id: "gen-size" }, ...Object.entries(gen.sizes).map(([k, n]) => h("option", { value: k, selected: k === "medium" }, `${k} (~${Math.min(n, cat.limits.city_sensors).toLocaleString()} sensors)`)));
  const profile = h("select", { id: "gen-profile" }, ...gen.profiles.map((p) => h("option", { value: p, selected: p === "mixed" }, p)));
  const seed = h("input", { id: "gen-seed", type: "number", min: "0", max: "4294967295", placeholder: "random" });
  const gname = h("input", { id: "gen-name", maxlength: "40", placeholder: "made up for each city", pattern: "[a-z0-9][a-z0-9-]*" });
  const err = h("p", { class: "error", role: "alert" });
  const out = h("div", {});
  const button = h("button", { class: "primary", type: "submit" }, "Generate demo cities");
  const form = h("form", { id: "gen-form", onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    fill(out);
    const body = { count: Number(count.value) || 1, size: size.value, profile: profile.value };
    if (seed.value !== "") body.seed = Number(seed.value);
    if (gname.value.trim() !== "") body.name = gname.value.trim();
    button.disabled = true;
    try {
      const r = await api("/api/cities/generate", { method: "POST", json: body });
      toast(`Created ${r.created} ${r.created === 1 ? "city" : "cities"}`);
      fill(out, summaryTable(r));
    } catch (x) { err.textContent = x.message; } finally { button.disabled = false; }
  } },
  h("div", { class: "grid3" }, field("How many", count, `1 to ${gen.max_count}; every city is different.`), field("Size", size, "Sensors per city; large is clamped to the limit."), field("Profile", profile, "What kind of city: mixed picks its own themes.")),
  h("div", { class: "grid3" }, field("Seed (optional)", seed, "Same seed, same cities. City n of a batch uses seed + n."), field("Name (optional)", gname, "With several cities it becomes name-1, name-2…")),
  h("div", { class: "btns" }, button));
  return h("div", { class: "card" }, h("p", { class: "muted" }, "Each city gets its own mix of 3 to 12 districts, sensor types that suit each district, group sizes, protocols, frequencies and burst percentages."), form, err, out);
}

function summaryTable(r) {
  const rows = r.cities.map((c) => h("tr", { class: "clickable", onclick: () => { location.hash = `#cities/${c.name}`; } },
    h("td", { class: "title" }, c.name), h("td", { class: "num" }, c.sensors.toLocaleString()), h("td", { class: "num" }, c.districts.length),
    h("td", {}, c.districts.map((d) => d.kind ? `${d.name} (${d.kind})` : d.name).join(", ")), h("td", { class: "num" }, c.types), h("td", {}, c.protocols.map(label).join(", ")), h("td", { class: "num" }, c.seed)));
  return h("div", {}, h("h3", {}, `Created ${r.created} ${r.created === 1 ? "city" : "cities"}, ${r.sensors.toLocaleString()} sensors in all`),
    h("div", { class: "card flush" }, h("table", { id: "gen-summary" }, h("thead", {}, h("tr", {}, ...["City", "Sensors", "Districts", "Which", "Types", "Protocols", "Seed"].map((t) => h("th", {}, t)))), h("tbody", {}, ...rows))));
}

// ---- the list ------------------------------------------------------------------------------------
async function renderList(box) {
  let cities;
  try { cities = await api("/api/cities"); } catch (err) { fill(box, h("p", { class: "error" }, err.message)); return; }
  const name = h("input", { id: "nc-name", required: true, maxlength: "40", placeholder: "metropolis", pattern: "[a-z0-9][a-z0-9-]*" });
  const err = h("p", { class: "error", role: "alert" });
  const create = async (spec) => {
    err.textContent = "";
    try { await api(`/api/cities/${spec.name}`, { method: "PUT", json: { target_url: catalog.defaults?.city_target_url ?? "", ...spec } }); location.hash = `#cities/${spec.name}`; } catch (e) { err.textContent = e.message; }
  };
  const form = h("form", { class: "grid-inline", onsubmit: (e) => { e.preventDefault(); create({ name: name.value, groups: [] }); } },
    field("City name", name, "Lowercase letters, digits and dashes."),
    h("button", { class: "primary", type: "submit" }, "Create empty city"),
    h("button", { class: "secondary", type: "button", title: "Fixed preset: every one is identical", onclick: () => name.value ? create(DEMO(name.value)) : (err.textContent = "Type a name first.") }, "Fixed demo city (always the same)"),
    ...[2, 5].map((n) => h("button", { class: "secondary", type: "button", title: `Fixed preset: a reading from each of 60 sensors every ${n} seconds, no protocol sockets`,
      onclick: () => name.value ? create(LIVE(name.value, n)) : (err.textContent = "Type a name first.") }, `Fixed live feed every ${n} s`)));
  const gen = generatorCard(catalog);
  const rows = cities.map((c) => h("tr", { class: "clickable", onclick: () => { location.hash = `#cities/${c.name}`; } },
    h("td", { class: "title" }, c.name), h("td", {}, c.sensors.toLocaleString()), h("td", {}, c.protocols.map(label).join(", ") || "none yet"),
    h("td", { class: "muted" }, targetText(c)),
    h("td", {}, h("span", { class: `st ${c.running ? "ok" : "offline"}` }, c.running ? "running" : "stopped"))));
  fill(box, h("h1", {}, "Cities"),
    h("p", { class: "muted" }, "A city is a set of sensor groups. Each group picks a sensor type, a count, a value range, a reporting frequency, a protocol and a burst percentage."),
    cities.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["City", "Sensors", "Protocols", "Sends to", "State"].map((t) => h("th", {}, t)))), h("tbody", {}, ...rows)))
      : h("div", { class: "card" }, h("p", { class: "empty" }, "No cities yet. Generate a few demo cities below: each one is different.")),
    h("h2", { class: "section-title" }, "Generate demo cities"), gen,
    h("h2", { class: "section-title" }, "Fixed presets"),
    h("div", { class: "card" }, h("p", { class: "muted" }, "These always give the very same city (the same districts and sensors). To get cities that differ, use the generator above."), form, err));
  guard(box);
}

// ---- one city ------------------------------------------------------------------------------------
function statsPanel(s) {
  const kv = (k, v) => [h("dt", {}, k), h("dd", {}, String(v))];
  const eps = Object.entries(s.endpoints || {});
  const bursts = Object.entries(s.bursts || {}).map(([k, n]) => `${k} ×${n}`).join(", ") || "none";
  return h("div", { class: "panel-grid" },
    h("div", { class: "card" }, h("h3", {}, "Delivery to CityPulse"),
      h("dl", { class: "facts" }, ...kv("Sensors", s.sensors.toLocaleString()), ...kv("Readings sent", s.sent.toLocaleString()), ...kv("Send rate", `${s.send_rate}/s`),
        ...kv("Waiting for retry", s.buffer.toLocaleString()), ...kv("Failed attempts", s.send_failures), ...kv("Dropped", s.dropped),
        ...kv("Uptime", `${s.uptime}s`), ...kv("Status", s.problem || "ok"))),
    h("div", { class: "card" }, h("h3", {}, "Behaviour"),
      h("dl", { class: "facts" }, ...kv("Bursts active", bursts), ...kv("City-wide boost", s.boost ? `${s.boost}s left` : "off"),
        ...kv("Silent updates", s.silenced.toLocaleString()), ...kv("Corrupt values", s.bad_values.toLocaleString()),
        ...kv("Devices unreachable", s.devices_unreachable))),
    h("div", { class: "card flush" }, h("h3", { class: "pad" }, "Field-device endpoints (loopback unless exposed)"),
      eps.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["Protocol", "Address", "Devices", "Polls ok", "Polls failed"].map((t) => h("th", {}, t)))),
        h("tbody", {}, ...eps.map(([p, e]) => h("tr", {}, h("td", {}, label(p)), h("td", {}, h("code", {}, `${e.host}:${e.port}`)), h("td", { class: "num" }, e.devices),
          h("td", { class: "num" }, (s.polls_ok[p] || 0).toLocaleString()), h("td", { class: "num" }, (s.polls_failed[p] || 0).toLocaleString()))))) :
        h("p", { class: "empty" }, "No protocol endpoints: every group is direct.")));
}

async function renderCity(box, name) {
  let city;
  try { city = await api(`/api/cities/${name}`); } catch (err) { fill(box, h("p", { class: "error" }, err.message), h("a", { href: "#cities" }, "← all cities")); return; }
  const locked = city.running;
  const err = h("p", { class: "error", role: "alert" });
  const save = async (spec, msg) => {
    err.textContent = "";
    try { await api(`/api/cities/${name}`, { method: "PUT", json: spec }); if (msg) toast(msg); renderCity(box, name); } catch (e) { err.textContent = e.message; }
  };
  const { running, target, will_resume, ...spec } = city;

  const url = h("input", { id: "c-url", value: city.target_url, placeholder: "https://your-citypulse… (empty: generate only)", disabled: locked });
  const tokenEnv = h("input", { id: "c-token", value: city.token_env, disabled: locked, maxlength: "64" });
  const seed = h("input", { id: "c-seed", type: "number", min: "0", value: city.seed ?? "", placeholder: "random each run", disabled: locked });
  const poll = h("input", { id: "c-poll", type: "number", min: "0.5", max: "60", step: "0.5", value: city.poll_interval, disabled: locked });
  const expose = h("input", { id: "c-expose", type: "checkbox", checked: city.expose, disabled: locked });
  const auto = h("input", { id: "c-auto", type: "checkbox", checked: city.autostart, disabled: locked });
  const settings = h("form", { onsubmit: (e) => { e.preventDefault(); save({ ...spec, target_url: url.value.trim(), token_env: tokenEnv.value.trim(), seed: seed.value === "" ? null : Number(seed.value), poll_interval: Number(poll.value), expose: expose.checked, autostart: auto.checked }, "Saved"); } },
    h("div", { class: "grid2" }, withFleet(field("CityPulse URL", url, "https, or http only for this machine. Or follow the fleet: the address is looked up when the city starts."), fleetChoice(url, "CITYPULSE_URL", "CityPulse", catalog.fleet || [], locked)),
      field("Token variable", tokenEnv, "The NAME of the environment variable that holds the CityPulse ingest token. The token itself is never stored here.")),
    h("div", { class: "grid3" }, field("Random seed", seed, "Same seed, same data and bursts."), field("Gateway poll interval (s)", poll)),
    h("label", { class: "check", for: "c-expose" }, expose, " Let other machines reach the protocol endpoints (binds all interfaces)"),
    h("label", { class: "check", for: "c-auto" }, auto, " Start this city when the app starts"),
    h("div", { class: "btns" }, h("button", { class: "primary", type: "submit", disabled: locked }, "Save settings")));

  // groups
  const typeSel = h("select", { id: "g-type" }, ...catalog.types.map((t) => h("option", { value: t.key }, `${t.label} (${t.unit})`)));
  const count = h("input", { id: "g-count", type: "number", min: "1", max: catalog.limits.group_count, value: "10" });
  // a district is any short id (alder-quay, d01): the ones this city already uses and the fixed presets' ids are suggested
  const known = [...new Set([...spec.groups.map((g) => g.district), ...catalog.districts.map((d) => d.id)])];
  const district = h("input", { id: "g-district", value: known[0] || "d01", list: "g-district-list", maxlength: "24", required: true, pattern: "[a-z0-9][a-z0-9_-]{0,23}", autocomplete: "off" });
  const districtList = h("datalist", { id: "g-district-list" }, ...known.map((d) => h("option", { value: d })));
  const proto = h("select", { id: "g-proto" }, ...catalog.protocols.map((p) => h("option", { value: p }, label(p))));
  const lo = h("input", { id: "g-lo", type: "number", step: "any" });
  const hi = h("input", { id: "g-hi", type: "number", step: "any" });
  const freq = h("input", { id: "g-freq", type: "number", min: "1", max: "3600", value: "30" });
  const burst = h("input", { id: "g-burst", type: "number", min: "0", max: "100", step: "1", value: "10" });
  const hint = h("p", { class: "hint" });
  const setHint = () => { const t = catalog.types.find((x) => x.key === typeSel.value); hint.textContent = `Physical limits for this type: ${t.lo} to ${t.hi} ${t.unit} (a value outside them is a sensor fault). Leave the range empty to use its usual range.`; };
  typeSel.addEventListener("change", setHint);
  setHint();
  const addGroup = h("form", { onsubmit: (e) => {
    e.preventDefault();
    const g = { type: typeSel.value, count: Number(count.value), district: district.value.trim(), protocol: proto.value, frequency: Number(freq.value), burst_pct: Number(burst.value) };
    if (lo.value !== "") g.lo = Number(lo.value);
    if (hi.value !== "") g.hi = Number(hi.value);
    save({ ...spec, groups: [...spec.groups, g] }, "Group added");
  } },
  h("div", { class: "grid3" }, field("Sensor type", typeSel), field("How many", count), field("District", district, "A short id such as alder-quay or d01.")),
  h("div", { class: "grid3" }, field("Protocol", proto, "How the data reaches the gateway."), field("Range from", lo), field("Range to", hi)),
  h("div", { class: "grid3" }, field("Every (seconds)", freq), field("Burst percentage", burst, "0 never, 100 constantly: how likely a sensor is to enter a random burst episode (spike, noise, stuck, dropout, flood, corrupt).")),
  hint, districtList, h("div", { class: "btns" }, h("button", { class: "primary", type: "submit", disabled: locked }, "Add sensor group")));

  const groupRows = spec.groups.map((g, i) => h("tr", {}, h("td", {}, g.type.replace("_", " ")), h("td", { class: "num" }, g.count.toLocaleString()), h("td", {}, g.district),
    h("td", {}, label(g.protocol)), h("td", {}, g.lo != null || g.hi != null ? `${g.lo ?? "…"} to ${g.hi ?? "…"}` : "usual"), h("td", { class: "num" }, `${g.frequency}s`),
    h("td", { class: "num" }, `${g.burst_pct}%`),
    h("td", { class: "actions" }, h("button", { class: "danger small", type: "button", disabled: locked, onclick: () => save({ ...spec, groups: spec.groups.filter((_, j) => j !== i) }) }, "Remove"))));

  // run controls
  const secs = h("select", { id: "storm-secs" }, ...[[60, "1 minute"], [180, "3 minutes"], [300, "5 minutes"]].map(([v, t]) => h("option", { value: v, selected: v === 180 }, t)));
  const act = async (path, msg, body) => { err.textContent = ""; try { await api(`/api/cities/${name}/${path}`, { method: "POST", json: body }); if (msg) toast(msg); renderCity(box, name); } catch (e) { err.textContent = e.message; } };
  const controls = h("div", { class: "card burst-big" },
    locked ? h("button", { class: "secondary", type: "button", onclick: () => act("stop", "Stopped") }, "Stop city") : h("button", { class: "primary", type: "button", onclick: () => act("start", "Started") }, "Start city"),
    h("div", { class: "row left" }, h("label", { for: "storm-secs" }, "Burst the whole city for"), secs,
      h("button", { class: "burst small-burst", type: "button", disabled: !locked, onclick: () => act("burst", "Burst started", { seconds: Number(secs.value) }) }, "BURST")),
    h("span", { class: "muted" }, locked ? "Running: changes are locked until you stop it." : "Stopped."),
    locked && will_resume ? h("span", { class: "muted", id: "c-resume" }, "Will resume after a restart (Stop city to keep it stopped).") : null);

  const live = h("div", {});
  const draw = async () => {
    if (!locked) { fill(live); return; }
    try { fill(live, statsPanel(await api(`/api/cities/${name}/stats`))); } catch { /* the next tick retries */ }
  };

  fill(box, h("a", { href: "#cities" }, "← all cities"), h("h1", {}, city.name),
    controls, err, live,
    h("h2", { class: "section-title" }, `Sensor groups (${spec.groups.reduce((n, g) => n + g.count, 0).toLocaleString()} sensors)`),
    spec.groups.length ? h("div", { class: "card flush" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Type", "Count", "District", "Protocol", "Range", "Every", "Burst", ""].map((t) => h("th", {}, t)))), h("tbody", {}, ...groupRows)))
      : h("div", { class: "card" }, h("p", { class: "empty" }, "No sensors yet. Add a group below.")),
    h("h2", { class: "section-title" }, "Add a sensor group"), h("div", { class: "card" }, addGroup),
    h("h2", { class: "section-title" }, "City settings"), h("div", { class: "card" }, settings),
    h("div", { class: "btns" }, h("button", { class: "danger", type: "button", onclick: async () => { if (!(await confirmDialog(`Delete ${name}?`, { title: "Delete city", confirmLabel: "Delete city", danger: true, typeToConfirm: name }))) return; await api(`/api/cities/${name}`, { method: "DELETE" }); location.hash = "#cities"; } }, "Delete city")));
  guard(box);
  await draw();
  clearInterval(timer);
  if (locked) timer = setInterval(() => { if (!location.hash.startsWith(`#cities/${name}`)) clearInterval(timer); else draw(); }, 2000);
}

export async function initCities(arg) {
  const box = $("view-cities");
  clearInterval(timer);
  if (!catalog) catalog = await api("/api/catalog");
  if (arg && arg[0]) await renderCity(box, arg[0]); else await renderList(box);
}
export function stopCities() { clearInterval(timer); }
