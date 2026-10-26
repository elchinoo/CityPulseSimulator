import { h, scrollRegion } from "/kit/ui.js";

// A table card that may scroll sideways on a phone: keyboard users can reach the scroll (a labelled, focusable region).
export const flushCard = (label, ...kids) => scrollRegion(h("div", { class: "card flush" }, ...kids), label);
