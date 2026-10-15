import { api } from "/static/api.js";
import { $, field, fill, h, toast } from "/static/ui.js";

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
      window.dispatchEvent(new CustomEvent("cs-auth-lost"));
    } catch (ex) { err.textContent = ex.message; }
  } },
  has ? field("Current password", cur) : null, field(has ? "New password (12+ characters)" : "Admin password (12+ characters)", nw), field("Repeat", nw2),
  h("button", { class: "primary", type: "submit" }, has ? "Change password" : "Set admin password"), err);
  fill($("view-settings"), h("h1", {}, "Settings"),
    h("p", { class: "muted" }, `Cities are stored in ${s.store === "postgres" ? "the linked Postgres database, so they survive restarts" : "a local file (data/state.json), which a container loses when it is recreated"}. Ingest tokens are never stored: a city only names the environment variable that holds its token.`),
    h("h2", { class: "section-title" }, "Admin password"), h("div", { class: "card" }, form));
}
