// Fleet references in a Target URL field: the one-click choice, and the read-only line that shows what the reference resolves to now.
import { h } from "/kit/ui.js";

const REF = /^\$\{([A-Z][A-Z0-9_]*)\}$/;

// "${X} → https://…" or why it does not resolve; empty for a plain URL.
export function resolution(value, fleet = []) {
  const m = REF.exec((value || "").trim());
  if (!m) return "";
  const v = fleet.find((f) => f.name === m[1]);
  return v && v.set ? `${m[0]} → ${v.value}` : `${m[0]} is not set on this app: set the URL by hand or deploy it with ecoctl`;
}

// What a table shows for a target: the literal URL, or the reference and its address.
export function targetText(item, fleet = []) {
  if (!item.target_url) return "generate only";
  if (!item.target || !item.target.reference) return item.target_url;
  return item.target.resolves ? `${item.target_url} → ${item.target.resolved_url}` : `${item.target_url} (not set on this app)`;
}

// A button that puts `${VAR}` into the (free text) input, and a line that follows what the input holds. Nothing is redrawn: what was typed
// stays until the button is clicked.
export function fleetChoice(input, varName, label, fleet, disabled = false) {
  const note = h("p", { class: "hint", "data-fleet-note": "1" });
  const update = () => { note.textContent = resolution(input.value, fleet); };
  input.addEventListener("input", update);
  update();
  const button = h("button", { class: "secondary small", type: "button", disabled, "data-fleet-choice": varName,
    onclick: () => { input.value = "${" + varName + "}"; update(); } }, `Use the fleet's ${label}`);
  return h("div", { class: "btns tight" }, button, note);
}

export function withFleet(fieldEl, choice) { fieldEl.append(choice); return fieldEl; }
