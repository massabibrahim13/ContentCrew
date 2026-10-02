/** Inline SVG icons, built with DOM calls (no innerHTML). */

const PATHS = {
  check: ["M4 10.5l4 4 8-9"],
  arrow: ["M4 10h11", "M11 5.5l4.5 4.5-4.5 4.5"],
  pending: [],
  alert: ["M10 5.5v6", "M10 14.5v.01"],
  info: ["M10 9v5", "M10 6v.01"],
  doc: ["M6 3.5h6l3 3v10H6z", "M12 3.5v3h3", "M8.5 10h4", "M8.5 13h4"],
  close: ["M5.5 5.5l9 9", "M14.5 5.5l-9 9"],
  send: ["M4 10h11", "M10.5 5l5 5-5 5"],
  chevron: ["M6 8l4 4 4-4"],
};

const NS = "http://www.w3.org/2000/svg";

export function icon(name, { size = 16, label } = {}) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.classList.add("icon", `icon-${name}`);
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else {
    svg.setAttribute("aria-hidden", "true");
  }

  if (name === "pending" || name === "alert" || name === "info") {
    const circle = document.createElementNS(NS, "circle");
    circle.setAttribute("cx", "10");
    circle.setAttribute("cy", "10");
    circle.setAttribute("r", name === "pending" ? "5" : "7.5");
    svg.append(circle);
  }
  for (const d of PATHS[name] || []) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
