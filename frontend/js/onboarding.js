/**
 * Onboarding: collect the marketing context in four steps and save it with
 * PUT /api/context. The same page edits an existing context (?edit=1).
 *
 * Checks here mirror the server's rules so people get feedback right away,
 * but the server validates everything again; its field errors are shown on
 * the matching inputs.
 */

import { api } from "./api.js";
import { h, toast } from "./dom.js";
import { icon } from "./icons.js";

const $ = (id) => document.getElementById(id);
const LAST_STEP = 3;
const MAX_COMPETITORS = 6;

const els = {
  title: $("ob-title"),
  loading: $("ob-loading"),
  form: $("ob-form"),
  steps: [...document.querySelectorAll(".ob-step")],
  fieldsets: [...document.querySelectorAll(".ob-fieldset")],
  name: $("company-name"),
  website: $("company-website"),
  industry: $("company-industry"),
  description: $("company-description"),
  descriptionCount: $("description-count"),
  compRows: $("comp-rows"),
  compHead: document.querySelector(".comp-head"),
  addCompetitor: $("add-competitor"),
  review: $("review"),
  formError: $("form-error"),
  back: $("back-button"),
  next: $("next-button"),
  sample: $("use-sample"),
  sampleBox: document.querySelector(".ob-sample"),
  cancel: $("cancel-link"),
};

const state = { step: 0, furthest: 0, editing: false, saving: false };

// ---------------------------------------------------------------------------
// Field errors
// ---------------------------------------------------------------------------

function setFieldError(field, message) {
  const error = document.querySelector(`[data-error-for="${CSS.escape(field)}"]`);
  const input = document.querySelector(`[data-field="${CSS.escape(field)}"]`);
  if (error) {
    error.textContent = message || "";
    error.hidden = !message;
  }
  if (input) {
    const target = input.classList.contains("tag-input") ? input.querySelector(".tag-entry") : input;
    if (message) target.setAttribute("aria-invalid", "true");
    else target.removeAttribute("aria-invalid");
    if (input.classList.contains("tag-input")) {
      if (message) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }
}

function clearErrors() {
  for (const el of document.querySelectorAll("[data-error-for]")) {
    el.hidden = true;
    el.textContent = "";
  }
  for (const el of document.querySelectorAll("[aria-invalid]")) el.removeAttribute("aria-invalid");
  showFormError("");
}

function showFormError(message) {
  els.formError.textContent = message || "";
  els.formError.hidden = !message;
}

function stepOfField(field) {
  if (field.startsWith("competitors")) return 2;
  if (field === "company.target_audience" || field === "company.products") return 1;
  return 0;
}

// ---------------------------------------------------------------------------
// Tag inputs (audiences, products)
// ---------------------------------------------------------------------------

class TagInput {
  constructor(root) {
    this.root = root;
    this.field = root.dataset.field;
    this.list = root.querySelector(".tags");
    this.entry = root.querySelector(".tag-entry");
    this.max = Number(root.dataset.max);
    this.itemMax = Number(root.dataset.itemMax);
    this.items = [];

    root.addEventListener("click", (e) => {
      if (e.target === root || e.target === this.list) this.entry.focus();
    });
    this.entry.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === ",") && !e.isComposing) {
        e.preventDefault();
        this.commit();
      } else if (e.key === "Backspace" && !this.entry.value && this.items.length) {
        this.remove(this.items.length - 1);
      }
    });
    this.entry.addEventListener("blur", () => this.commit());
    this.entry.addEventListener("paste", (e) => {
      const text = e.clipboardData?.getData("text") || "";
      if (!text.includes(",") && !text.includes("\n")) return;
      e.preventDefault();
      for (const part of text.split(/[,\n]/)) this.add(part);
    });
  }

  get values() {
    this.commit();
    return [...this.items];
  }

  set(values) {
    this.items = [];
    for (const value of values || []) this.add(value, { quiet: true });
    this.render();
  }

  commit() {
    if (this.entry.value.trim()) this.add(this.entry.value);
  }

  add(raw, { quiet = false } = {}) {
    const value = String(raw).replace(/\s+/g, " ").trim();
    if (!value) return;
    if (this.items.some((item) => item.toLowerCase() === value.toLowerCase())) {
      this.entry.value = "";
      if (!quiet) setFieldError(this.field, `"${value}" is already in the list.`);
      return;
    }
    if (this.items.length >= this.max) {
      if (!quiet) setFieldError(this.field, `You can add up to ${this.max}.`);
      return;
    }
    if (value.length > this.itemMax) {
      if (!quiet) setFieldError(this.field, `Keep each one under ${this.itemMax} characters.`);
      return;
    }
    this.items.push(value);
    this.entry.value = "";
    setFieldError(this.field, "");
    this.render();
  }

  remove(index) {
    this.items.splice(index, 1);
    this.render();
    this.entry.focus();
  }

  render() {
    this.list.replaceChildren(...this.items.map((item, index) =>
      h("li", { class: "tag" },
        h("span", {}, item),
        h("button", { type: "button", class: "tag-remove", "aria-label": `Remove ${item}`,
          onclick: () => this.remove(index) }, icon("close", { size: 12 })),
      )));
  }
}

const audience = new TagInput(document.querySelector('[data-field="company.target_audience"]'));
const products = new TagInput(document.querySelector('[data-field="company.products"]'));

// ---------------------------------------------------------------------------
// Competitor rows
// ---------------------------------------------------------------------------

function readCompetitorRows() {
  return [...els.compRows.querySelectorAll(".comp-row")].map((row) => ({
    name: row.querySelector(".comp-name").value,
    website: row.querySelector(".comp-website").value,
  }));
}

function renderCompetitorRows(rows, focusIndex = null) {
  els.compRows.replaceChildren(...rows.map((row, i) => {
    const nameField = `competitors.${i}.name`;
    const siteField = `competitors.${i}.website`;
    return h("li", { class: "comp-row" },
      h("div", { class: "comp-cell" },
        h("input", { class: "input comp-name", value: row.name || "", maxlength: 80,
          "data-field": nameField, "aria-label": `Competitor ${i + 1} name` }),
        h("p", { class: "field-error", "data-error-for": nameField, hidden: true }),
      ),
      h("div", { class: "comp-cell" },
        h("input", { class: "input comp-website", value: row.website || "", maxlength: 300,
          inputmode: "url", placeholder: "competitor.com", "data-field": siteField,
          "aria-label": `Competitor ${i + 1} website` }),
        h("p", { class: "field-error", "data-error-for": siteField, hidden: true }),
      ),
      h("button", { type: "button", class: "btn btn-quiet btn-sm comp-remove",
        "aria-label": `Remove competitor ${i + 1}`, onclick: () => {
          const current = readCompetitorRows();
          current.splice(i, 1);
          renderCompetitorRows(current);
          els.addCompetitor.focus();
        } }, "Remove"),
    );
  }));

  const empty = rows.length === 0;
  els.compHead.hidden = empty;
  if (empty) {
    els.compRows.append(h("li", { class: "comp-empty" }, "No competitors yet. Add a few, or continue and let the Analysis Agent find them."));
  }
  els.addCompetitor.hidden = rows.length >= MAX_COMPETITORS;
  if (focusIndex !== null) els.compRows.querySelectorAll(".comp-name")[focusIndex]?.focus();
}

els.addCompetitor.addEventListener("click", () => {
  const rows = readCompetitorRows();
  if (rows.length >= MAX_COMPETITORS) return;
  rows.push({ name: "", website: "" });
  renderCompetitorRows(rows, rows.length - 1);
});

// ---------------------------------------------------------------------------
// Reading and validating the form
// ---------------------------------------------------------------------------

function looksLikeUrl(value) {
  return /^(https?:\/\/)?[^\s/]+\.[^\s/]+(\/\S*)?$/i.test(value.trim());
}

function collect() {
  return {
    company: {
      name: els.name.value.trim(),
      website: els.website.value.trim(),
      industry: els.industry.value.trim(),
      description: els.description.value.trim(),
      target_audience: audience.values,
      products: products.values,
    },
    competitors: readCompetitorRows()
      .map((c) => ({ name: c.name.trim(), website: c.website.trim() }))
      .filter((c) => c.name || c.website),
  };
}

function validateStep(step) {
  const errors = {};
  const { company } = collect();

  if (step === 0) {
    if (company.name.length < 2) errors["company.name"] = "Enter the company name.";
    if (company.website && !looksLikeUrl(company.website)) {
      errors["company.website"] = "Enter a valid web address, like example.com.";
    }
    if (company.description.length < 20) {
      errors["company.description"] = "Describe the company in at least 20 characters.";
    }
  }
  if (step === 1) {
    if (company.target_audience.length === 0) {
      errors["company.target_audience"] = "Add at least one target audience.";
    }
  }
  if (step === 2) {
    const seen = new Set();
    readCompetitorRows().forEach((row, i) => {
      const name = row.name.trim();
      const website = row.website.trim();
      if (!name && !website) return;
      if (!name) errors[`competitors.${i}.name`] = "Enter the competitor's name.";
      else if (seen.has(name.toLowerCase())) errors[`competitors.${i}.name`] = "Already in the list.";
      seen.add(name.toLowerCase());
      if (website && !looksLikeUrl(website)) errors[`competitors.${i}.website`] = "Enter a valid web address.";
    });
  }

  for (const [field, message] of Object.entries(errors)) setFieldError(field, message);
  const first = Object.keys(errors)[0];
  if (first) document.querySelector(`[data-field="${CSS.escape(first)}"]`)?.focus?.();
  return !first;
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

function goToStep(step, { focus = true } = {}) {
  state.step = step;
  state.furthest = Math.max(state.furthest, step);

  els.fieldsets.forEach((fieldset, i) => { fieldset.hidden = i !== step; });
  els.steps.forEach((button, i) => {
    button.disabled = i > state.furthest;
    button.classList.toggle("is-done", i < state.furthest && i !== step);
    if (i === step) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });

  els.back.hidden = step === 0;
  els.next.textContent = step < LAST_STEP ? "Continue" : state.editing ? "Save changes" : "Save and open workspace";
  if (step === LAST_STEP) renderReview();
  showFormError("");

  if (focus) {
    const first = els.fieldsets[step].querySelector("input, textarea, button");
    first?.focus({ preventScroll: true });
    if (window.matchMedia("(max-width: 880px)").matches) els.form.scrollIntoView({ block: "start" });
  }
}

function renderReview() {
  const { company, competitors } = collect();
  const missing = (text) => h("span", { class: "review-missing" }, text);
  const row = (label, value) => h("div", {}, h("dt", {}, label), h("dd", {}, value));
  const section = (title, step, ...rows) =>
    h("section", { class: "review-section" },
      h("div", { class: "review-head" },
        h("h3", {}, title),
        h("button", { type: "button", class: "btn btn-quiet btn-sm", onclick: () => goToStep(step) }, "Edit"),
      ),
      h("dl", { class: "review-list" }, rows),
    );

  els.review.replaceChildren(
    section("Company", 0,
      row("Name", company.name),
      row("Website", company.website || missing("Not added")),
      row("Industry", company.industry || missing("Not added")),
      row("Description", company.description),
    ),
    section("Audience and offering", 1,
      row("Target audience", company.target_audience.join(", ")),
      row("Products and services", company.products.join(", ") || missing("Not added")),
    ),
    section("Competitors", 2,
      competitors.length
        ? competitors.map((c, i) => row(`Competitor ${i + 1}`, c.website ? `${c.name} (${c.website})` : c.name))
        : row("Competitors", missing("None. The Analysis Agent will look for them.")),
    ),
  );
}

// ---------------------------------------------------------------------------
// Load, fill, save
// ---------------------------------------------------------------------------

function fill(context) {
  const company = context.company || {};
  els.name.value = company.name || "";
  els.website.value = company.website || "";
  els.industry.value = company.industry || "";
  els.description.value = company.description || "";
  updateDescriptionCount();
  audience.set(company.target_audience || []);
  products.set(company.products || []);
  renderCompetitorRows((context.competitors || []).map((c) => ({ name: c.name, website: c.website || "" })));
}

function updateDescriptionCount() {
  const length = els.description.value.length;
  els.descriptionCount.textContent = `${length} / 600`;
  els.descriptionCount.classList.toggle("is-over", length > 600);
}

async function save() {
  if (state.saving) return;
  for (const step of [0, 1, 2]) {
    if (!validateStep(step)) {
      goToStep(step, { focus: false });
      validateStep(step);
      return;
    }
  }

  state.saving = true;
  els.next.disabled = true;
  els.next.textContent = "Saving";
  try {
    await api.saveContext(collect());
    window.location.assign("/app");
  } catch (err) {
    state.saving = false;
    els.next.disabled = false;
    goToStep(LAST_STEP, { focus: false }); // restores the button label
    const fields = Object.entries(err.fields || {});
    if (fields.length) {
      const firstStep = Math.min(...fields.map(([field]) => stepOfField(field)));
      goToStep(firstStep, { focus: false });
      for (const [field, message] of fields) setFieldError(field, message);
      showFormError("Some details need fixing before they can be saved.");
    } else {
      showFormError(err.message);
    }
  }
}

els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (state.step < LAST_STEP) {
    if (validateStep(state.step)) goToStep(state.step + 1);
  } else {
    save();
  }
});

els.back.addEventListener("click", () => goToStep(Math.max(0, state.step - 1)));

els.steps.forEach((button) => button.addEventListener("click", () => {
  const target = Number(button.dataset.goto);
  if (target > state.step && !validateStep(state.step)) return;
  goToStep(target);
}));

els.form.addEventListener("input", (e) => {
  const field = e.target.closest("[data-field]")?.dataset.field;
  if (field) setFieldError(field, "");
  if (e.target === els.description) updateDescriptionCount();
});

els.sample.addEventListener("click", async () => {
  els.sample.disabled = true;
  try {
    fill(await api.getSampleContext());
    clearErrors();
    state.furthest = LAST_STEP;
    goToStep(LAST_STEP);
    toast("Sample company filled in. Check the details, then save.");
  } catch (err) {
    showFormError(err.message);
  } finally {
    els.sample.disabled = false;
  }
});

async function init() {
  let loadError = "";
  try {
    const context = await api.getContext();
    if (context.onboarded) {
      state.editing = true;
      state.furthest = LAST_STEP;
      fill(context);
      els.title.textContent = "Edit marketing context";
      document.title = "Edit marketing context – ContentCrew";
      els.cancel.hidden = false;
      els.sampleBox.hidden = true;
    } else {
      renderCompetitorRows([{ name: "", website: "" }]);
    }
  } catch (err) {
    renderCompetitorRows([{ name: "", website: "" }]);
    loadError = `Your saved context couldn't be loaded: ${err.message}`;
  }
  els.loading.hidden = true;
  els.form.hidden = false;
  goToStep(0, { focus: false });
  showFormError(loadError);
}

init();
