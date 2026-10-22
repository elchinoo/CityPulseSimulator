import { can } from "/kit/session.js";

// A viewer sees everything and changes nothing. The server refuses their writes (403); this only disables the controls so
// they are not offered. Buttons marked data-ro-ok (like Preview, which sends nothing) stay usable.
export function guard(box) {
  if (can("sim:edit")) return;
  for (const el of box.querySelectorAll("button, input, select, textarea")) if (!el.dataset.roOk) el.disabled = true;
}
