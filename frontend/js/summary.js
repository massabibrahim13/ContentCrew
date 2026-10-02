/**
 * Renders the summaries the backend attaches to approvals:
 *   {headline, stats: [{label, value}], sections: [{title, items, note, open}]}
 * Used by the approval cards in the chat and the details panel in the editor,
 * so both show the same facts in the same way.
 */

import { h } from "./dom.js";

/** The counts row: big number, small label underneath. */
export function renderStats(stats, className = "summary-stats") {
  if (!Array.isArray(stats) || !stats.length) return null;
  return h("dl", { class: className },
    stats.map((stat) => h("div", { class: "summary-stat" },
      h("dt", {}, stat.label),
      h("dd", {}, String(stat.value)),
    )),
  );
}

/** One block per section. A single item reads as a sentence, several as a list. */
export function renderSections(sections) {
  return (sections || []).map((section) => {
    const items = Array.isArray(section.items) ? section.items.filter(Boolean) : [];
    return h("section", { class: "summary-section" },
      h("h4", {}, section.title),
      items.length === 1 ? h("p", { class: "summary-text" }, items[0])
        : items.length ? h("ul", {}, items.map((item) => h("li", {}, item))) : null,
      section.note ? h("p", { class: "summary-note" }, section.note) : null,
    );
  });
}
