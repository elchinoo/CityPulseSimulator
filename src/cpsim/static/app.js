import { api } from "/static/api.js";
import { initCities, stopCities } from "/static/cities.js";
import { initProtocols } from "/static/protocols.js";
import { refreshSession, renderLogin, renderSetup, state } from "/static/session.js";
import { initSettings } from "/static/settings.js";
import { $, h } from "/static/ui.js";

const TABS = ["cities", "protocols", "settings"];
const INIT = { cities: initCities, protocols: initProtocols, settings: initSettings };
const SECTIONS = ["auth-card", "nav", "signout", ...TABS.map((t) => "view-" + t)];

function hideAll() { for (const id of SECTIONS) $(id).hidden = true; }

function showUnavailable(err) {
  hideAll();
  $("auth-card").replaceChildren(
    h("img", { class: "hero-logo", src: "/static/logo.svg", alt: "", width: "72", height: "72" }),
    h("h1", {}, "CityPulse Simulator can't be reached"),
    h("p", { class: "muted" }, err.message),
    h("p", { class: "hint" }, "If this runs on a platform that pauses idle apps, resume it first (for AgentPG: agentpg sandbox resume <sandbox-id>), then retry."),
    h("button", { class: "primary", type: "button", onclick: () => boot() }, "Retry"));
  $("auth-card").hidden = false;
}

async function boot(notice = "") {
  hideAll();
  stopCities();
  let s;
  try { s = await refreshSession(); } catch (err) { showUnavailable(err); return; }
  if (s.state === "setup_required") renderSetup(() => boot());
  else if (s.state === "logged_out") renderLogin(() => boot(), notice);
  else { $("auth-card").hidden = true; $("nav").hidden = false; $("signout").hidden = false; route(); }
}

function route() {
  if (!state.session || state.session.state !== "logged_in") return;
  const [name, ...arg] = location.hash.slice(1).split("/");
  const tab = TABS.includes(name) ? name : "cities";
  if (tab !== "cities") stopCities();
  for (const t of TABS) $("view-" + t).hidden = t !== tab;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.tab === tab);
  INIT[tab](tab === "cities" ? arg : undefined);
}

$("signout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch { /* the cookie is cleared server-side regardless */ }
  boot();
});
let lostTimer;
window.addEventListener("cs-auth-lost", () => { clearTimeout(lostTimer); lostTimer = setTimeout(() => boot("Your session ended. Please sign in again."), 50); });
window.addEventListener("hashchange", route);
boot();
