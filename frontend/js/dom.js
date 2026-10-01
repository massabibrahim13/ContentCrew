/**
 * Small DOM helpers.
 *
 * Everything that comes from the server (company names, agent messages, blog
 * text) is inserted as text nodes, never as HTML. That is the frontend half of
 * the "treat external content as data" rule: a competitor page that contains
 * <script> just shows up as the characters "<script>".
 */

/**
 * Create an element. Children can be strings (inserted as text), nodes, arrays, or null.
 *   h("a", { class: "link", href: url }, "Open")
 */
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  append(el, children);
  return el;
}

export function append(parent, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function clear(el) {
  while (el.firstChild) el.removeChild(el.firstChild);
  return el;
}

/** Only allow http(s) links; anything else (javascript:, data:) becomes null. */
export function safeHref(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch {
    return null;
  }
}

export function domainOf(value) {
  try {
    return new URL(value).hostname.replace(/^www\./, "");
  } catch {
    return value || "";
  }
}

export function formatDate(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
}

export function formatTime(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

export function plural(n, word, pluralWord = `${word}s`) {
  return `${n} ${n === 1 ? word : pluralWord}`;
}

/** Brief message in the corner. `tone` is "info" or "error". */
export function toast(message, tone = "info", ms = 4500) {
  let region = document.getElementById("toasts");
  if (!region) {
    region = h("div", { id: "toasts", class: "toast-region", "aria-live": "polite" });
    document.body.append(region);
  }
  const node = h("div", { class: `toast${tone === "error" ? " is-error" : ""}`, role: "status" }, message);
  region.append(node);
  window.setTimeout(() => node.remove(), ms);
}
