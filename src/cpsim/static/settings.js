import { api } from "/kit/api.js";
import { $, copy, field, fill, fmtDate, h, toast } from "/kit/ui.js";

function tokensCard() {
  const reveal = h("div", { hidden: true });
  const tName = h("input", { id: "tk-name", maxlength: "60", required: true, placeholder: "e.g. Grafana scraper" });
  const tScope = h("select", { id: "tk-scope" }, h("option", { value: "read" }, "Read-only (see cities and companies)"),
    h("option", { value: "metrics" }, "Metrics (scrape /metrics only)"), h("option", { value: "full" }, "Full access (run and change everything)"));
  const tErr = h("p", { class: "error", role: "alert" });
  const table = h("div", {});
  async function renderTable() {
    const list = await api("/api/tokens");
    table.replaceChildren(list.length ? h("div", { class: "card flush" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Name", "Token", "Access", "Created", "Last used", ""].map((t) => h("th", {}, t)))),
      h("tbody", {}, ...list.map((t) => h("tr", {},
        h("td", { class: "title" }, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)), h("td", {}, t.scope),
        h("td", {}, fmtDate(t.created_at)), h("td", {}, fmtDate(t.last_used_at)),
        h("td", { class: "actions" }, t.revoked_at ? h("span", { class: "muted" }, "revoked") : h("button", { class: "danger small", type: "button", onclick: async () => {
          if (!confirm(`Revoke "${t.name}"? Clients using it stop working immediately.`)) return;
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
    h("p", { class: "muted" }, "Cities and companies are stored in the linked Postgres database, so they survive restarts. Ingest and client tokens are never stored: a city or company only names the environment variable that holds its token."),
    h("h2", { class: "section-title" }, "Admin password"), h("div", { class: "card" }, form),
    ...tokensCard());
}
