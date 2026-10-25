import { api } from "/kit/api.js";
import { initCities, stopCities } from "/static/cities.js";
import { initCompanies, stopCompanies } from "/static/companies.js";
import { initProtocols } from "/static/protocols.js";
import { initWorkloads, stopWorkloads } from "/static/workloads.js";
import { bootSession, can, renderLogin, renderSetup, state } from "/kit/session.js";
import { initSettings } from "/static/settings.js";
import { $, h } from "/kit/ui.js";

const TABS = ["cities", "companies", "workloads", "protocols", "settings"];
const INIT = { cities: initCities, companies: initCompanies, workloads: initWorkloads, protocols: initProtocols, settings: initSettings };
const SECTIONS = ["auth-card", "nav", "signout", ...TABS.map((t) => "view-" + t)];

function hideAll() { for (const id of SECTIONS) $(id).hidden = true; }

const PAUSED_HINT = "If this runs on a platform that pauses idle apps, resume it first (for AgentPG: agentpg sandbox resume <sandbox-id>), then retry.";

async function boot(notice = "") {
  hideAll();
  stopCities();
  stopCompanies();
  stopWorkloads();
  const card = $("auth-card");
  card.hidden = false;
  // onReady also runs after a successful "Try again", so a retry lands in the app instead of an empty card.
  const onReady = (s) => {
    if (s.state === "setup_required") renderSetup(() => boot(), "CityPulse Simulator");
    else if (s.state === "logged_out") renderLogin(() => boot(), notice, "CityPulse Simulator");
    else showApp();
  };
  const s = await bootSession(card, { text: "Connecting to CityPulse Simulator\u2026", onReady });   // Loading while single sign-on settles; error + Try again if it cannot be reached
  if (!s) card.append(h("p", { class: "hint" }, PAUSED_HINT));
}

// What each tab needs (the server enforces it; this only hides what would be refused). Protocols is plain documentation.
const NEEDS = { cities: "sim:read", companies: "sim:read", workloads: "sim:read", protocols: null, settings: "settings:manage" };
const allowed = (t) => !NEEDS[t] || can(NEEDS[t]);

function showApp() {
  $("auth-card").hidden = true;
  for (const a of document.querySelectorAll("#nav a")) a.hidden = !allowed(a.dataset.tab);
  $("nav").hidden = false;
  $("signout").hidden = false;
  route();
}

function route() {
  if (!state.session || state.session.state !== "logged_in") return;
  const [name, ...arg] = location.hash.slice(1).split("/");
  const open = TABS.filter(allowed);
  const tab = open.includes(name) ? name : (open[0] || "protocols");
  if (tab !== "cities") stopCities();
  if (tab !== "companies") stopCompanies();
  if (tab !== "workloads") stopWorkloads();
  for (const t of TABS) $("view-" + t).hidden = t !== tab;
  for (const a of document.querySelectorAll("#nav a")) a.classList.toggle("active", a.dataset.tab === tab);
  INIT[tab](tab === "cities" || tab === "companies" || tab === "workloads" ? arg : undefined);
}

$("signout").addEventListener("click", async () => {
  let out = {};
  try { out = await api("/api/logout", { method: "POST" }); } catch { /* the cookie is cleared server-side regardless */ }
  if (out.logout_url) { location.assign(out.logout_url); return; }   // also end the single sign-on session
  boot();
});
let lostTimer;
window.addEventListener("apg-auth-lost", () => { clearTimeout(lostTimer); lostTimer = setTimeout(() => boot("Your session ended. Please sign in again."), 50); });
window.addEventListener("hashchange", route);
boot();
