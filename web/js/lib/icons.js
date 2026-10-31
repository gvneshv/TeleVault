/**
 * Inline SVG icons for the app shell - no CDN, no icon font, no build step.
 *
 * Where they come from
 *   - chats, messages, deleted, stats, health: hand-drawn in the approved chat-view mockup (claude/chat-view-spec.md); the owner asked to keep the stats and health ones.
 *   - everything marked "// lucide: <name>": Lucide (https://lucide.dev), package lucide-static 1.66.0.
 *     Lucide is ISC-licensed, and the icons it derived from Feather (log-out is one of them) are MIT-licensed:
 *     both notices travel in web/icons/LICENSE-lucide.txt - keep that file when copying more icons from Lucide.
 *   The Lucide names are the owner's shortlist (first candidate of each).
 *   To change one, swap the node list for another icon's (lucide-static ships them as icon-nodes.json) and keep the "// lucide:" comment truthful.
 *
 * All icons are drawn on a 24x24 grid with a 1.8 stroke in currentColor, so colour comes from the CSS of whatever contains them (hover, active, theme).
 * An element opts in with data-icon="<name>"; renderIcons() puts the <svg> inside it.
 * Icons are decoration only (aria-hidden): the button that holds one always has its own text or aria-label, which is what assistive technology reads.
 */

const SVG_NS = "http://www.w3.org/2000/svg";

/** @type {Record<string, Array<[string, Record<string, string>]>>} */
const ICONS = {
  chats: [
    [
      "path",
      { d: "M21 12a8 8 0 0 1-11.6 7.1L4 20.5l1.4-4.6A8 8 0 1 1 21 12z" },
    ],
  ],
  messages: [["path", { d: "M4 6h16M4 12h16M4 18h10" }]],
  deleted: [
    ["path", { d: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6" }],
  ],
  stats: [["path", { d: "M5 20V10M12 20V4M19 20v-7" }]],
  health: [["path", { d: "M3 12h4l2-6 4 12 2-6h6" }]],
  settings: [
    ["path", { d: "M11 10.27 7 3.34" }],
    ["path", { d: "m11 13.73-4 6.93" }],
    ["path", { d: "M12 22v-2" }],
    ["path", { d: "M12 2v2" }],
    ["path", { d: "M14 12h8" }],
    ["path", { d: "m17 20.66-1-1.73" }],
    ["path", { d: "m17 3.34-1 1.73" }],
    ["path", { d: "M2 12h2" }],
    ["path", { d: "m20.66 17-1.73-1" }],
    ["path", { d: "m20.66 7-1.73 1" }],
    ["path", { d: "m3.34 17 1.73-1" }],
    ["path", { d: "m3.34 7 1.73 1" }],
    ["circle", { cx: "12", cy: "12", r: "2" }],
    ["circle", { cx: "12", cy: "12", r: "8" }],
  ], // lucide: cog
  backfill: [
    ["path", { d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }],
    ["path", { d: "M3 3v5h5" }],
    ["path", { d: "M12 7v5l4 2" }],
  ], // lucide: rotate-ccw-clock
  admin: [
    [
      "path",
      {
        d: "M16.051 12.616a1 1 0 0 1 1.909.024l.737 1.452a1 1 0 0 0 .737.535l1.634.256a1 1 0 0 1 .588 1.806l-1.172 1.168a1 1 0 0 0-.282.866l.259 1.613a1 1 0 0 1-1.541 1.134l-1.465-.75a1 1 0 0 0-.912 0l-1.465.75a1 1 0 0 1-1.539-1.133l.258-1.613a1 1 0 0 0-.282-.866l-1.156-1.153a1 1 0 0 1 .572-1.822l1.633-.256a1 1 0 0 0 .737-.535z",
      },
    ],
    ["path", { d: "M8 15H7a4 4 0 0 0-4 4v2" }],
    ["circle", { cx: "10", cy: "7", r: "4" }],
  ], // lucide: user-star
  "archiver-stopped": [
    ["path", { d: "M12 22V12" }],
    ["path", { d: "m16.5 14.5 5 5" }],
    ["path", { d: "m16.5 19.5 5-5" }],
    [
      "path",
      {
        d: "M21 10.5V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.729l7 4a2 2 0 0 0 2 .001l.13-.074",
      },
    ],
    ["path", { d: "M3.29 7 12 12l8.71-5" }],
    ["path", { d: "m7.5 4.27 8.997 5.148" }],
  ], // lucide: package-x
  "archiver-running": [
    ["path", { d: "M12 22V12" }],
    ["path", { d: "M16 17h6" }],
    ["path", { d: "M19 14v6" }],
    [
      "path",
      {
        d: "M21 10.535V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.729l7 4a2 2 0 0 0 2 .001l1.675-.955",
      },
    ],
    ["path", { d: "M3.29 7 12 12l8.71-5" }],
    ["path", { d: "m7.5 4.27 8.997 5.148" }],
  ], // lucide: package-plus
  logout: [
    ["path", { d: "m16 17 5-5-5-5" }],
    ["path", { d: "M21 12H9" }],
    ["path", { d: "M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" }],
  ], // lucide: log-out
  language: [
    ["path", { d: "M21.54 15H17a2 2 0 0 0-2 2v4.54" }],
    [
      "path",
      {
        d: "M7 3.34V5a3 3 0 0 0 3 3a2 2 0 0 1 2 2c0 1.1.9 2 2 2a2 2 0 0 0 2-2c0-1.1.9-2 2-2h3.17",
      },
    ],
    [
      "path",
      { d: "M11 21.95V18a2 2 0 0 0-2-2a2 2 0 0 1-2-2v-1a2 2 0 0 0-2-2H2.05" },
    ],
    ["circle", { cx: "12", cy: "12", r: "10" }],
  ], // lucide: earth
  "theme-light": [
    ["circle", { cx: "12", cy: "12", r: "4" }],
    ["path", { d: "M12 2v2" }],
    ["path", { d: "M12 20v2" }],
    ["path", { d: "m4.93 4.93 1.41 1.41" }],
    ["path", { d: "m17.66 17.66 1.41 1.41" }],
    ["path", { d: "M2 12h2" }],
    ["path", { d: "M20 12h2" }],
    ["path", { d: "m6.34 17.66-1.41 1.41" }],
    ["path", { d: "m19.07 4.93-1.41 1.41" }],
  ], // lucide: sun
  "theme-dark": [
    [
      "path",
      {
        d: "M20.985 12.486a9 9 0 1 1-9.473-9.472c.405-.022.617.46.402.803a6 6 0 0 0 8.268 8.268c.344-.215.825-.004.803.401",
      },
    ],
  ], // lucide: moon
  collapse: [
    ["path", { d: "M2 12h6" }],
    ["path", { d: "M22 12h-6" }],
    ["path", { d: "M12 2v2" }],
    ["path", { d: "M12 8v2" }],
    ["path", { d: "M12 14v2" }],
    ["path", { d: "M12 20v2" }],
    ["path", { d: "m19 9-3 3 3 3" }],
    ["path", { d: "m5 15 3-3-3-3" }],
  ], // lucide: fold-horizontal
  expand: [
    ["path", { d: "M16 12h6" }],
    ["path", { d: "M8 12H2" }],
    ["path", { d: "M12 2v2" }],
    ["path", { d: "M12 8v2" }],
    ["path", { d: "M12 14v2" }],
    ["path", { d: "M12 20v2" }],
    ["path", { d: "m19 15 3-3-3-3" }],
    ["path", { d: "m5 9-3 3 3 3" }],
  ], // lucide: unfold-horizontal
};

/**
 * Build the <svg> element for one icon name, or null if the name is unknown (a typo shows nothing rather than breaking the page).
 * @param {string} name
 * @returns {SVGSVGElement | null}
 */
export function iconElement(name) {
  const nodes = ICONS[name];
  if (!nodes) return null;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.classList.add("icon");
  for (const [tag, attrs] of nodes) {
    const el = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs))
      el.setAttribute(key, value);
    svg.append(el);
  }
  return svg;
}

/**
 * Fill every [data-icon] element under `root` that does not have its icon yet.
 * Safe to call repeatedly (e.g. after the Admin tab is added).
 * @param {ParentNode} [root]
 */
export function renderIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((holder) => {
    if (holder.querySelector("svg")) return;
    const svg = iconElement(holder.getAttribute("data-icon") ?? "");
    if (svg) holder.append(svg);
  });
}

/**
 * Swap the icon inside an element, e.g. the collapse/expand button.
 * @param {Element} holder
 * @param {string} name
 */
export function setIcon(holder, name) {
  holder.setAttribute("data-icon", name);
  holder.querySelector("svg")?.remove();
  renderIcons(holder.parentElement ?? document);
}

export const ICON_NAMES = Object.keys(ICONS);
