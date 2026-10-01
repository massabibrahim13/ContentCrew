/** Public page for a published post: /blog/<slug> */

import { formatDate } from "./dom.js";
import { renderMarkdown } from "./markdown.js";

const status = document.getElementById("post-status");
const slug = decodeURIComponent(window.location.pathname.split("/").filter(Boolean).pop() || "");

async function load() {
  try {
    const response = await fetch(`/api/posts/${encodeURIComponent(slug)}`, { headers: { Accept: "application/json" } });
    const data = await response.json().catch(() => null);
    if (!response.ok) throw new Error(data?.error?.message || "The post couldn't be loaded.");

    const { post } = data;
    document.title = `${post.title} – ContentCrew`;
    document.getElementById("post-title").textContent = post.title;
    document.getElementById("post-date").textContent = `Published ${formatDate(post.published_at)}`;
    document.getElementById("post-body").replaceChildren(renderMarkdown(post.content));
    document.getElementById("post").hidden = false;
    status.hidden = true;
  } catch (err) {
    status.textContent = err.message === "Failed to fetch"
      ? "Can't reach the ContentCrew server. Check that it's running."
      : err.message;
    status.classList.add("is-error");
  }
}

load();
