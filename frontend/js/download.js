/**
 * Download a blog post as a file, so it can go on any website.
 *
 * The file is built in the browser from what the editor shows (including edits
 * that aren't saved yet), so nothing is sent to the server.
 *
 *   html  a small, readable web page: open it in a browser or Word, or copy its
 *         text into a website editor
 *   md    the Markdown source, for sites and tools that accept Markdown
 */

import { h } from "./dom.js";
import { renderMarkdown } from "./markdown.js";

const FORMATS = {
  html: { ext: "html", type: "text/html;charset=utf-8", build: (title, content) => htmlFile(title, content) },
  md: { ext: "md", type: "text/markdown;charset=utf-8", build: (title, content) => markdownFile(title, content) },
};

const PAGE_STYLE = [
  "body{max-width:720px;margin:48px auto;padding:0 20px;font:18px/1.7 Georgia,'Times New Roman',serif;color:#1b201d}",
  "h1,h2,h3{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.25}",
  "h1{font-size:2em;margin:0 0 .8em}h2{margin-top:1.8em}",
  "a{color:#1f5135}",
  "blockquote{margin:1.2em 0;padding-left:16px;border-left:3px solid #d8dcd5;color:#4a524c}",
  "code{font-family:Consolas,monospace;font-size:.9em}",
  "hr{border:0;border-top:1px solid #d8dcd5;margin:2em 0}",
].join("");

/** "How Agentic AI Is Changing Brand Activations" -> "how-agentic-ai-is-changing-brand-activations" */
export function fileSlug(title) {
  const slug = String(title || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 60)
    .replace(/-+$/, "");
  return slug || "blog-post";
}

export function markdownFile(title, content) {
  return `# ${title}\n\n${String(content || "").trim()}\n`;
}

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const escapeHtml = (text) => text.replace(/[&<>"']/g, (c) => ESCAPES[c]);

/**
 * A complete web page. The post is rendered as DOM nodes and then serialized,
 * so its text is always escaped and can never become markup.
 */
export function htmlFile(title, content) {
  const article = h("article", {}, h("h1", {}, title), renderMarkdown(content));
  return [
    "<!doctype html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${escapeHtml(title)}</title>`,
    `<style>${PAGE_STYLE}</style>`,
    "</head>",
    "<body>",
    article.outerHTML,
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

export function downloadBlog({ title, content }, format) {
  const kind = FORMATS[format];
  if (!kind) throw new Error(`Unknown format: ${format}`);
  const cleanTitle = String(title || "").replace(/\s+/g, " ").trim() || "Untitled post";
  const url = URL.createObjectURL(new Blob([kind.build(cleanTitle, content)], { type: kind.type }));
  const link = h("a", { href: url, download: `${fileSlug(cleanTitle)}.${kind.ext}`, hidden: true });
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}
