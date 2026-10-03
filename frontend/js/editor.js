/**
 * The right-hand blog editor.
 *
 * Closed until the Generation Agent reports a draft (a `blog_ready` event).
 * Shows the draft rendered, lets the user edit the Markdown, and exposes the
 * three actions from the design: Edit, Regenerate, Publish. Download (in the
 * header) saves the post as a web page or Markdown file for any website.
 *
 * - Publishing is a two-step click and goes through the content-approval
 *   endpoint, so the server decides whether the post can go out.
 * - Regenerate opens a small form for optional feedback ("make the introduction
 *   more concise"); the Generation Agent then redoes only what that's about.
 * - "Outline and SEO" shows the measured facts about this draft (the same ones
 *   as the approval card in the chat), folded away until the user wants them.
 */

import { downloadBlog } from "./download.js";
import { renderMarkdown } from "./markdown.js";
import { renderSections } from "./summary.js";

const CONFIRM_MS = 5000;

export class BlogEditor {
  /**
   * @param {Record<string, HTMLElement>} els
   * @param {{onSave, onRegenerate, onPublish, onOpenChange}} handlers
   */
  constructor(els, handlers) {
    this.els = els;
    this.handlers = handlers;
    this.blog = null;
    this.mode = "preview";
    this.confirmTimer = null;
    this.busy = false;
    this.current = true;   // false = an older version, kept for reference: read-only
    this.locked = false;   // true = the agents are working (rewriting or publishing): read-only for now

    els.noteAction.addEventListener("click", () => this.handlers.onOpenLatest?.());

    els.close.addEventListener("click", () => this.close());
    els.edit.addEventListener("click", () => this.#toggleEdit());
    els.regenerate.addEventListener("click", () => this.#showRegenerate(true));
    els.regenCancel.addEventListener("click", () => this.#showRegenerate(false));
    els.regenForm.addEventListener("submit", (e) => {
      e.preventDefault();
      this.#regenerate();
    });
    els.publish.addEventListener("click", () => this.#publish());
    els.title.addEventListener("input", () => {
      this.#fitTitle();
      this.#markDirty();
    });
    els.title.addEventListener("keydown", (e) => {
      if (e.key === "Enter") e.preventDefault();      // a title is one line, even though it wraps
    });
    els.source.addEventListener("input", () => this.#markDirty());

    // Download: always available, whatever the draft's state; it saves what's on screen.
    els.downloadToggle.addEventListener("click", () => this.#showDownloads(els.downloadMenu.hidden));
    els.downloadMenu.addEventListener("click", (e) => {
      const option = e.target.closest("[data-format]");
      if (option) this.#download(option.dataset.format);
    });
    els.download.addEventListener("keydown", (e) => {
      if (e.key !== "Escape" || els.downloadMenu.hidden) return;
      e.stopPropagation();                          // close the menu, not the editor
      this.#showDownloads(false);
      els.downloadToggle.focus();
    });
    els.download.addEventListener("focusout", (e) => {
      if (e.relatedTarget && !els.download.contains(e.relatedTarget)) this.#showDownloads(false);
    });
    document.addEventListener("click", (e) => {
      if (!els.download.contains(e.target)) this.#showDownloads(false);
    });
    // Refit the title whenever its width changes (panel opening, window resizing).
    if ("ResizeObserver" in window) new ResizeObserver(() => this.#fitTitle()).observe(els.title);
    else window.addEventListener("resize", () => this.#fitTitle());
  }

  get isOpen() {
    return !this.els.panel.hidden;
  }

  get blogId() {
    return this.blog?.id ?? null;
  }

  get hasUnsavedChanges() {
    return Boolean(this.isOpen && this.dirty);
  }

  /** Save pending edits (e.g. before feedback is sent from the chat). Returns false if saving failed. */
  saveChanges() {
    return this.#save();
  }

  /**
   * current: is this the chat's newest draft? Older versions are read-only.
   * locked: are the agents busy with it? Then nothing can be changed until they finish.
   */
  setAccess({ current = this.current, locked = this.locked } = {}) {
    this.current = current;
    this.locked = locked;
    if (this.blog) this.#renderStatus();
  }

  open(blog, details = null, access = {}) {
    const sameBlog = this.blog?.id === blog.id;
    this.blog = blog;
    this.dirty = false;
    this.current = access.current ?? true;
    this.locked = access.locked ?? false;
    this.els.title.value = blog.title;
    this.els.source.value = blog.content;
    this.#setMode("preview");
    this.#showRegenerate(false);
    this.#showDownloads(false);
    if (!sameBlog || details) this.setDetails(details);
    this.#renderStatus();
    this.setError("");
    this.els.panel.hidden = false;
    this.handlers.onOpenChange?.(true);       // the layout changes first, then the title is measured
    this.#fitTitle();
    this.els.panel.focus({ preventScroll: true });
  }

  close() {
    if (!this.isOpen) return;
    this.els.panel.hidden = true;
    this.#resetConfirm();
    this.#showDownloads(false);
    this.handlers.onOpenChange?.(false);
  }

  setError(message) {
    this.els.error.textContent = message || "";
    this.els.error.hidden = !message;
  }

  /** The approval summary for this draft: {stats, sections}. Null hides the panel. */
  setDetails(summary) {
    const { details, detailsMeta, detailsBody } = this.els;
    if (!summary || !Array.isArray(summary.sections) || !summary.sections.length) {
      details.hidden = true;
      detailsBody.replaceChildren();
      return;
    }
    const stats = summary.stats || [];
    const pick = (label) => stats.find((s) => s.label === label)?.value;
    const meta = [
      pick("words") && `${pick("words")} words`,
      pick("keywords used") && `${pick("keywords used")} keywords`,
      pick("to check") ? `${pick("to check")} to check` : null,
    ].filter(Boolean);
    detailsMeta.textContent = meta.join(" · ");          // the counts, in one quiet line
    detailsBody.replaceChildren(...renderSections(summary.sections));
    details.hidden = false;
  }

  // -- internals ------------------------------------------------------------

  #renderStatus() {
    const published = this.blog?.status === "published";
    const old = !published && !this.current;
    const locked = !published && this.current && this.locked;
    const [label, tone] = published ? ["Published", "published"]
      : old ? ["Older version", "old"]
      : this.dirty ? ["Unsaved changes", "dirty"]
      : locked ? ["Agents working", "locked"]
      : ["Draft", "draft"];
    this.els.status.textContent = label;
    this.els.status.dataset.state = tone;

    const readOnly = published || old || locked;
    for (const button of [this.els.edit, this.els.regenerate, this.els.publish]) {
      button.disabled = readOnly || this.busy;
    }
    this.els.publish.textContent = published ? "Published" : "Publish";
    if (readOnly) {
      this.#showRegenerate(false);
      this.#resetConfirm();
      if (this.mode === "edit" && !this.dirty) this.#setMode("preview");
    }

    // Say why the editor is read-only, so a disabled button never looks broken.
    const note = old ? "This is an older version, kept for reference. Only the latest draft can be edited or published."
      : locked ? "The agents are working on this draft. Editing unlocks when the next version is ready for review."
      : "";
    this.els.noteText.textContent = note;
    this.els.note.hidden = !note;
    this.els.noteAction.hidden = !old;
  }

  #setMode(mode) {
    this.mode = mode;
    const editing = mode === "edit";
    this.els.title.readOnly = !editing;
    this.els.source.hidden = !editing;
    this.els.preview.hidden = editing;
    this.els.edit.textContent = editing ? "Save changes" : "Edit";
    if (!editing) this.els.preview.replaceChildren(renderMarkdown(this.els.source.value));
    else this.els.source.focus();
  }

  /** The title wraps instead of being cut off: grow the field to fit it. */
  #fitTitle() {
    const title = this.els.title;
    if (this.els.panel.hidden) return;
    title.style.height = "auto";
    const height = `${title.scrollHeight}px`;
    if (title.style.height !== height) title.style.height = height;
  }

  #markDirty() {
    this.dirty = true;
    this.#renderStatus();
  }

  async #save() {
    if (!this.dirty) return true;
    this.#setBusy(true);
    try {
      const { blog } = await this.handlers.onSave(this.blog.id, {
        title: this.els.title.value.replace(/\s*\n\s*/g, " ").trim(),
        content: this.els.source.value,
      });
      this.blog = blog;
      this.els.title.value = blog.title;
      this.els.source.value = blog.content;
      this.dirty = false;
      this.setError("");
      this.#fitTitle();
      return true;
    } catch (err) {
      const fields = err.fields || {};
      this.setError(fields.title || fields.content || err.message);
      return false;
    } finally {
      this.#setBusy(false);
      this.#renderStatus();
    }
  }

  async #toggleEdit() {
    if (this.mode === "preview") {
      this.#setMode("edit");
      return;
    }
    if (await this.#save()) this.#setMode("preview");
  }

  #showRegenerate(show) {
    const { regenForm, regenText, actions, regenerate } = this.els;
    regenForm.hidden = !show;
    actions.hidden = show;
    regenerate.setAttribute("aria-expanded", String(show));
    if (show) {
      this.#resetConfirm();
      regenText.focus();
    } else {
      regenText.value = "";
    }
  }

  #showDownloads(show) {
    this.els.downloadMenu.hidden = !show;
    this.els.downloadToggle.setAttribute("aria-expanded", String(show));
  }

  /** The file holds what the editor shows, including edits that aren't saved yet. */
  #download(format) {
    this.#showDownloads(false);
    try {
      downloadBlog({ title: this.els.title.value, content: this.els.source.value }, format);
    } catch {
      this.setError("The file couldn't be created. Try again.");
    }
  }

  async #regenerate() {
    // Save first: a targeted revision starts from the version in the editor.
    if (!(await this.#save())) return;
    if (this.mode === "edit") this.#setMode("preview");
    const feedback = this.els.regenText.value.trim();
    this.#setBusy(true);
    try {
      await this.handlers.onRegenerate(this.blog, feedback || null);
      this.setError("");
      this.#showRegenerate(false);
    } catch (err) {
      this.setError(err.message);
    } finally {
      this.#setBusy(false);
      this.#renderStatus();
    }
  }

  async #publish() {
    if (!this.confirmTimer) {
      this.els.publish.textContent = "Confirm publish";
      this.els.publish.classList.add("is-confirming");
      this.confirmTimer = window.setTimeout(() => this.#resetConfirm(), CONFIRM_MS);
      return;
    }
    this.#resetConfirm();
    if (!(await this.#save())) return;
    if (this.mode === "edit") this.#setMode("preview");

    this.#setBusy(true);
    try {
      await this.handlers.onPublish(this.blog);
      this.setError("");
    } catch (err) {
      this.setError(err.message);
    } finally {
      this.#setBusy(false);
      this.#renderStatus();
    }
  }

  #resetConfirm() {
    window.clearTimeout(this.confirmTimer);
    this.confirmTimer = null;
    this.els.publish.classList.remove("is-confirming");
    if (this.blog?.status !== "published") this.els.publish.textContent = "Publish";
  }

  #setBusy(busy) {
    this.busy = busy;
    const buttons = [this.els.edit, this.els.regenerate, this.els.publish, this.els.regenSend, this.els.regenCancel];
    for (const button of buttons) button.disabled = busy;
    this.els.panel.setAttribute("aria-busy", busy ? "true" : "false");
  }
}
