/**
 * A deliberately small Markdown renderer for blog drafts.
 *
 * It builds DOM nodes directly (no innerHTML), so model output or scraped text
 * can never inject markup. Supported: #/##/### headings, paragraphs, - and 1.
 * lists, > quotes, --- dividers, **bold**, *italic*, `code`, and [links](https://...).
 */

import { h, safeHref } from "./dom.js";

const INLINE = /(\*\*[^*\n]+\*\*|\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_|`[^`\n]+`|\[[^\]\n]+\]\([^)\s]+\))/g;

function inline(text) {
  const nodes = [];
  let last = 0;
  for (const match of text.matchAll(INLINE)) {
    if (match.index > last) nodes.push(text.slice(last, match.index));
    const token = match[0];
    if (token.startsWith("**")) nodes.push(h("strong", {}, token.slice(2, -2)));
    else if (token.startsWith("`")) nodes.push(h("code", {}, token.slice(1, -1)));
    else if (token.startsWith("[")) {
      const [, label, href] = token.match(/^\[([^\]]+)\]\(([^)]+)\)$/);
      const safe = safeHref(href);
      nodes.push(safe ? h("a", { href: safe, target: "_blank", rel: "noopener noreferrer" }, label) : label);
    } else nodes.push(h("em", {}, token.slice(1, -1)));
    last = match.index + token.length;
  }
  if (last < text.length) nodes.push(text.slice(last));
  return nodes;
}

export function renderMarkdown(source) {
  const fragment = document.createDocumentFragment();
  const lines = String(source || "").replace(/\r\n/g, "\n").split("\n");
  let paragraph = [];
  let list = null;

  const flushParagraph = () => {
    if (paragraph.length) fragment.append(h("p", {}, inline(paragraph.join(" "))));
    paragraph = [];
  };
  const flushList = () => {
    if (list) fragment.append(list);
    list = null;
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = line.match(/^(#{1,3})\s+(.*)$/);
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const quote = line.match(/^>\s?(.*)$/);
    const divider = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line);

    if (!line.trim()) {
      flushParagraph();
      flushList();
    } else if (divider) {
      flushParagraph();
      flushList();
      fragment.append(h("hr"));
    } else if (heading) {
      flushParagraph();
      flushList();
      fragment.append(h(heading[1].length === 3 ? "h3" : "h2", {}, inline(heading[2])));
    } else if (bullet || numbered) {
      flushParagraph();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) {
        flushList();
        list = h(tag);
      }
      list.append(h("li", {}, inline((bullet || numbered)[1])));
    } else if (quote) {
      flushParagraph();
      flushList();
      fragment.append(h("blockquote", {}, inline(quote[1])));
    } else {
      flushList();
      paragraph.push(line.trim());
    }
  }
  flushParagraph();
  flushList();
  return fragment;
}
