import { api } from "/kit/api.js";
import { $, confirmDialog, copy, field, fill, fmtDate, h, toast } from "/kit/ui.js";
import { flushCard } from "/static/cards.js";

function tokensCard() {
  const reveal = h("div", { hidden: true });
  const tName = h("input", { id: "tk-name", maxlength: "60", required: true, placeholder: "e.g. Grafana scraper" });
  const tScope = h("select", { id: "tk-scope" }, h("option", { value: "read" }, "Read-only (see cities and companies)"),
    h("option", { value: "metrics" }, "Metrics (scrape /metrics only)"), h("option", { value: "full" }, "Full access (run and change everything)"));
  const tErr = h("p", { class: "error", role: "alert" });
  const table = h("div", {});
  async function renderTable() {
    const list = await api("/api/tokens");
    table.replaceChildren(list.length ? flushCard("API tokens", h("table", {},
      h("thead", {}, h("tr", {}, ...["Name", "Token", "Access", "Created", "Last used", "Actions"].map((t) => (t === "Actions" ? h("th", {}, h("span", { class: "sr-only" }, t)) : h("th", {}, t))))),
      h("tbody", {}, ...list.map((t) => h("tr", {},
        h("td", { class: "title" }, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)), h("td", {}, t.scope),
        h("td", {}, fmtDate(t.created_at)), h("td", {}, fmtDate(t.last_used_at)),
        h("td", { class: "actions" }, t.revoked_at ? h("span", { class: "muted" }, "revoked") : h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!(await confirmDialog(`Revoke "${t.name}"? Clients using it stop working immediately.`, { title: "Revoke token", confirmLabel: "Revoke", danger: true }))) return;
          try { await api(`/api/tokens/${t.id}`, { method: "DELETE" }); toast("Token revoked"); renderTable(); } catch (err) { toast(err.message, true); }
        } }, "Revoke"))))))) : h("p", { class: "hint" }, "No tokens yet."));
  }
  const form = h("form", { class: "grid-inline", onsubmit: async (e) => {
    e.preventDefault();
    tErr.textContent = "";
    try {
      const t = await api("/api/tokens", { method: "POST", json: { name: tName.value, scope: tScope.value } });
      tName.value = "";
      reveal.replaceChildren(
        h("p", { class: "warn-text" }, "Copy this token now. It is shown only once and cannot be recovered; only a hash is stored."),
        h("div", { class: "codewrap" }, h("pre", { class: "code" }, h("code", {}, t.token)), h("button", { class: "secondary small", type: "button", onclick: () => copy(t.token, "Token copied") }, "Copy")));
      reveal.hidden = false;
      renderTable();
    } catch (ex) { tErr.textContent = ex.message; }
  } }, field("Name", tName), field("Access", tScope), h("button", { class: "primary", type: "submit" }, "Create token"));
  renderTable().catch((err) => table.replaceChildren(h("p", { class: "error" }, err.message)));
  return [h("h2", { class: "section-title" }, "API tokens"),
    h("p", { class: "muted" }, "For scripts and scrapers. Prefer the narrowest access: a metrics token can read nothing but /metrics."),
    h("div", { class: "card" }, form, tErr), reveal, table];
}

function credentialsCard() {
  const box = h("div", {});
  const note = h("p", { class: "muted" });
  // Saving or deleting one credential redraws the table; whatever is being typed in the other rows (and the focus) must survive that.
  const typed = new Map();
  let focused = null;
  async function render() {
    const d = await api("/api/credentials");
    note.textContent = d.unlocked ? "" : "The credential store is locked: the app has no CPSIM_ENCRYPTION_KEY, so nothing can be saved. Signing in with the admin password unlocks it for this session.";
    box.replaceChildren(flushCard("Saved credentials", h("table", {},
      h("thead", {}, h("tr", {}, ...["Name", "Source", "Used by", "New value", "Actions"].map((t) => (t === "Actions" ? h("th", {}, h("span", { class: "sr-only" }, t)) : h("th", {}, t))))),
      h("tbody", {}, ...d.items.map((c) => {
        const input = h("input", { type: "password", autocomplete: "off", maxlength: "4096", placeholder: c.saved ? "replace saved value" : "paste the credential", disabled: !d.unlocked, "aria-label": `New value for ${c.name}` });
        input.value = typed.get(c.name) || "";
        input.addEventListener("input", () => typed.set(c.name, input.value));
        input.addEventListener("focus", () => { focused = c.name; });
        input.addEventListener("blur", () => { if (focused === c.name) focused = null; });
        if (focused === c.name) queueMicrotask(() => input.focus?.());
        const save = async () => {
          if (!input.value) return;
          try { await api(`/api/credentials/${encodeURIComponent(c.name)}`, { method: "PUT", json: { value: input.value } }); input.value = ""; typed.delete(c.name); toast("Saved"); render(); } catch (err) { toast(err.message, true); }
        };
        input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } });
        return h("tr", {}, h("td", { class: "title" }, h("code", {}, c.name)),
          h("td", {}, c.source === "environment" ? "environment variable (wins)" : c.source === "saved" ? "saved here" : "not set"),
          h("td", { class: "muted" }, c.used_by.join(", ") || "-"), h("td", {}, input),
          h("td", { class: "actions" }, h("button", { class: "secondary small", type: "button", disabled: !d.unlocked, onclick: save }, "Save"),
            c.saved ? h("button", { class: "danger small", type: "button", onclick: async () => {
              if (!(await confirmDialog(`Delete the saved ${c.name}? Workloads that use it stop working.`, { title: "Delete saved credential", confirmLabel: "Delete", danger: true }))) return;
              try { await api(`/api/credentials/${encodeURIComponent(c.name)}`, { method: "DELETE" }); toast("Deleted"); render(); } catch (err) { toast(err.message, true); }
            } }, "Delete") : null));
      })))));
  }
  render().catch((err) => box.replaceChildren(h("p", { class: "error" }, err.message)));
  return [h("h2", { class: "section-title" }, "Credentials for the apps it drives"),
    h("p", { class: "muted" }, "A PayBroker merchant secret key, a gateway client key, an ingest token. Paste one once: it is encrypted with the vault and never shown again. An environment variable of the same name takes precedence. Apps that trust the identity provider (CityPulse, PagerLens, TrustDesk, the gateway) need none: the simulator signs in as its own service client."),
    note, box];
}

// Read-only: the seven addresses ecoctl (or the person who deployed this app) gave it. A city, company or workload can follow them.
function fleetCard() {
  const box = h("div", {});
  api("/api/fleet").then((d) => {
    const rows = (d.vars || []).map((v) => h("tr", {}, h("td", { class: "title" }, h("code", {}, v.name)), h("td", {}, v.label),
      h("td", {}, h("span", { class: `st ${v.set ? "ok" : "offline"}` }, v.set ? "set" : "not set")), h("td", { class: "wrap" }, v.set ? v.value : "")));
    box.replaceChildren(flushCard("Fleet", h("table", { id: "fleet-table" },
      h("thead", {}, h("tr", {}, ...["Variable", "App", "State", "Address"].map((t) => h("th", {}, t)))), h("tbody", {}, ...rows))));
  }).catch((err) => box.replaceChildren(h("p", { class: "error" }, err.message)));
  return [h("h2", { class: "section-title" }, "Fleet"),
    h("p", { class: "muted" }, "The addresses of the other apps, given to this app when ecoctl deploys it. A target written as ${CITYPULSE_URL} (use the button next to the URL field) follows them: when an app is recreated and this one is redeployed, nothing needs retyping. These are URLs, not secrets."),
    box];
}

// What runs when the app restarts. The box saves by itself and nothing else on the page is redrawn, so typing elsewhere is never lost.
function resumeCard() {
  const box = h("input", { id: "resume-on-start", type: "checkbox", disabled: true });
  const note = h("p", { class: "hint", id: "resume-note" });
  const err = h("p", { class: "error", role: "alert" });
  const show = (d) => {
    box.checked = d.resume_on_start;
    box.disabled = false;
    note.textContent = `One item every ${d.stagger_seconds} s so a big fleet does not start all at once (CPSIM_RESUME_STAGGER_SECONDS). Until this is changed here, the default is ${d.default ? "on" : "off"} (CPSIM_RESUME_ON_START).`;
  };
  box.addEventListener("change", async () => {
    err.textContent = "";
    const want = box.checked;
    try { show(await api("/api/run-settings", { method: "PUT", json: { resume_on_start: want } })); toast(want ? "Will resume after a restart" : "Will not resume after a restart"); }
    catch (ex) { box.checked = !want; err.textContent = ex.message; }
  });
  api("/api/run-settings").then(show).catch((ex) => { err.textContent = ex.message; });
  return [h("h2", { class: "section-title" }, "After a restart"),
    h("p", { class: "muted" }, "Cities, workloads and company runs or schedules that were running (started by a person or a schedule, and not stopped) start again when the app starts. Stopping one yourself keeps it stopped. This is separate from each one's own \"start when the app starts\" box, which starts it even if it was stopped."),
    h("div", { class: "card" }, h("label", { class: "check", for: "resume-on-start" }, box, " Resume what was running when the app restarts"), note, err)];
}

export async function initSettings() {
  const s = await api("/api/session");
  const has = s.has_admin_password;
  const cur = h("input", { id: "pw-cur", type: "password", autocomplete: "current-password" });
  const nw = h("input", { id: "pw-new", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const nw2 = h("input", { id: "pw-new2", type: "password", autocomplete: "new-password", minlength: "12", required: true });
  const err = h("p", { class: "error", role: "alert" });
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    err.textContent = "";
    if (nw.value !== nw2.value) { err.textContent = "The new passwords do not match."; return; }
    try {
      await api("/api/admin-password", { method: "POST", json: { current: has ? cur.value : null, new: nw.value } });
      cur.value = nw.value = nw2.value = "";
      toast("Password saved. You were signed out everywhere.");
      window.dispatchEvent(new CustomEvent("apg-auth-lost"));
    } catch (ex) { err.textContent = ex.message; }
  } },
  has ? field("Current password", cur) : null, field(has ? "New password (12+ characters)" : "Admin password (12+ characters)", nw), field("Repeat", nw2),
  h("button", { class: "primary", type: "submit" }, has ? "Change password" : "Set admin password"), err);
  fill($("view-settings"), h("h1", {}, "Settings"),
    h("p", { class: "muted" }, "Cities and companies are stored in the linked Postgres database, so they survive restarts. A city, company or workload only names the credential it uses (an environment variable name); the value comes from the environment or from Settings > Credentials."),
    h("h2", { class: "section-title" }, "Admin password"), h("div", { class: "card" }, form),
    ...resumeCard(), ...fleetCard(), ...credentialsCard(), ...tokensCard());
}
