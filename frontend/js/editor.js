/**
 * The right-hand blog editor.
 *
 * Closed until the Generation Agent reports a draft (a `blog_ready` event).
 * Shows the draft rendered, lets the user edit the Markdown, and exposes the
 * three actions from the design: Edit, Regenerate, Publish. Publishing is a
 * two-step click and goes through the content-approval endpoint, so the
 * server decides whether the post can go out.
 */

import { renderMarkdown } from "./markdown.js";

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

    els.close.addEventListener("click", () => this.close());
    els.edit.addEventListener("click", () => this.#toggleEdit());
    els.regenerate.addEventListener("click", () => this.#regenerate());
    els.publish.addEventListener("click", () => this.#publish());
    els.title.addEventListener("input", () => this.#markDirty());
    els.source.addEventListener("input", () => this.#markDirty());
  }

  get isOpen() {
    return !this.els.panel.hidden;
  }

  get blogId() {
    return this.blog?.id ?? null;
  }

  open(blog) {
    this.blog = blog;
    this.dirty = false;
    this.els.title.value = blog.title;
    this.els.source.value = blog.content;
    this.#setMode("preview");
    this.#renderStatus();
    this.setError("");
    this.els.panel.hidden = false;
    this.handlers.onOpenChange?.(true);
    this.els.panel.focus({ preventScroll: true });
  }

  close() {
    if (!this.isOpen) return;
    this.els.panel.hidden = true;
    this.#resetConfirm();
    this.handlers.onOpenChange?.(false);
  }

  setError(message) {
    this.els.error.textContent = message || "";
    this.els.error.hidden = !message;
  }

  // -- internals ------------------------------------------------------------

  #renderStatus() {
    const published = this.blog?.status === "published";
    this.els.status.textContent = published ? "Published" : this.dirty ? "Unsaved changes" : "Draft";
    this.els.status.dataset.state = published ? "published" : this.dirty ? "dirty" : "draft";
    for (const button of [this.els.edit, this.els.regenerate, this.els.publish]) button.disabled = published;
    this.els.publish.textContent = published ? "Published" : "Publish";
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

  #markDirty() {
    this.dirty = true;
    this.#renderStatus();
  }

  async #save() {
    if (!this.dirty) return true;
    this.#setBusy(true);
    try {
      const { blog } = await this.handlers.onSave(this.blog.id, {
        title: this.els.title.value,
        content: this.els.source.value,
      });
      this.blog = blog;
      this.els.title.value = blog.title;
      this.els.source.value = blog.content;
      this.dirty = false;
      this.setError("");
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

  async #regenerate() {
    this.#setBusy(true);
    try {
      await this.handlers.onRegenerate(this.blog);
      this.setError("");
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
    for (const button of [this.els.edit, this.els.regenerate, this.els.publish]) button.disabled = busy;
    this.els.panel.setAttribute("aria-busy", busy ? "true" : "false");
  }
}
