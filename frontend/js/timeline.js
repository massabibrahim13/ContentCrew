/**
 * Renders backend events as the chat timeline.
 *
 * Each user message starts a "turn". Everything the system does in response
 * (workflow steps, agent runs, tool calls, approval requests) hangs off a
 * vertical rail under that message, so students can follow the hand-offs:
 * Supervisor -> Analysis Agent -> you -> Generation Agent -> you -> publish.
 *
 * The timeline only draws what the backend reports. It never invents steps.
 */

import { h, formatTime } from "./dom.js";
import { icon } from "./icons.js";
import { renderSections, renderStats } from "./summary.js";

export const AGENTS = {
  supervisor: { name: "Supervisor Agent", mark: "S" },
  analysis: { name: "Analysis Agent", mark: "A" },
  generation: { name: "Generation Agent", mark: "G" },
};

const TOOLS = {
  google_search: "Google Search",
  web_scraper: "Web Scraper",
  keyword_analysis: "Keyword Analysis",
  competitor_analysis: "Competitor Analysis",
  seo_analysis: "SEO Analysis",
  publish_blog: "Publish Blog",
};

const RUN_STATUS = { running: "Working", completed: "Done", failed: "Failed" };
const TOOL_ICON = { completed: "check", failed: "alert", skipped: "pending" };
const PLAN_ICON = { done: "check", active: "arrow", pending: "pending", skipped: "pending" };
const PLAN_LABEL = { done: "done", active: "in progress", pending: "not started", skipped: "skipped" };

const STAGES = {
  research: {
    title: "Research and content strategy",
    question: "Approve this research and content strategy?",
    approve: "Approve and continue",
    other: { decision: "modify", label: "Request changes" },
    cancel: ["Cancel request", "Confirm cancel"],    // a second click confirms, like Publish
    review: ["Review strategy", "Hide strategy"],
  },
  content: {
    title: "Generated draft",
    question: "Publish it from the editor, or ask for changes with Regenerate. You can also type " +
      "them here, for example: make the introduction more concise.",
    approve: null, // the editor's Publish button is the approval
    other: null,
    review: ["Review details", "Hide details"],
  },
};

export class Timeline {
  /**
   * @param {HTMLElement} root
   * @param {{onApprove, onModify, onCancel, onOpenDraft, onRetry}} handlers
   */
  constructor(root, handlers) {
    this.root = root;
    this.handlers = handlers;
    this.reset();
  }

  reset() {
    this.root.replaceChildren();
    this.activity = null;
    this.runs = new Map();
    this.toolRows = new Map();
    this.approvals = new Map();
    this.lastUserMessage = null;
    this.lastBlog = null;
  }

  get isEmpty() {
    return this.root.childElementCount === 0;
  }

  add(event) {
    switch (event.type) {
      case "message":
        return event.role === "user" ? this.#userMessage(event) : this.#agentMessage(event);
      case "workflow":
        return this.#step(event);
      case "agent_started":
        return this.#agentStarted(event);
      case "agent_completed":
        return this.#agentCompleted(event);
      case "plan_updated":
        return this.#planUpdated(event);
      case "node_started":
      case "node_completed":
        return this.#node(event);
      case "tool_started":
      case "tool_completed":
        return this.#tool(event);
      case "agent":   // events saved before Phase 4
        return event.status === "running" ? this.#agentStarted(event) : this.#agentCompleted(event);
      case "tool":    // events saved before Phase 4
        return this.#tool({ ...event, type: event.status === "running" ? "tool_started" : "tool_completed" });
      case "approval_required":
        return this.#approval(event);
      case "approval_resolved":
        return this.#approvalResolved(event);
      case "blog_ready":
        return this.#blogReady(event);
      case "blog_published":
        return this.#blogPublished(event);
      case "error":
        return this.#error(event);
      default:
        return null; // unknown event types are ignored, not fatal
    }
  }

  /** Show an error inside a pending approval card (e.g. the request failed). */
  setApprovalError(stage, message) {
    const card = this.approvals.get(stage);
    if (!card) return;
    card.error.textContent = message;
    card.error.hidden = !message;
  }

  setApprovalBusy(stage, busy) {
    const card = this.approvals.get(stage);
    if (!card) return;
    for (const button of card.root.querySelectorAll("button")) button.disabled = busy;
  }

  // -- turns ----------------------------------------------------------------

  #userMessage(event) {
    this.lastUserMessage = event.content;
    this.activity = h("ol", { class: "activity", "aria-label": "What the agents did" });
    const turn = h("section", { class: "turn" },
      h("div", { class: "msg-user" },
        h("p", { class: "msg-user-text" }, event.content),
        h("time", { class: "msg-time", datetime: event.created_at }, formatTime(event.created_at)),
      ),
      this.activity,
    );
    this.root.append(turn);
    return turn;
  }

  #ensureActivity() {
    if (!this.activity) {
      this.activity = h("ol", { class: "activity" });
      this.root.append(h("section", { class: "turn" }, this.activity));
    }
    return this.activity;
  }

  #item(className, node, ...content) {
    const li = h("li", { class: `act ${className}` }, h("span", { class: "act-node" }, node), h("div", { class: "act-body" }, content));
    this.#ensureActivity().append(li);
    return li;
  }

  // -- event renderers ------------------------------------------------------

  #step(event) {
    if (event.tone === "notice") {
      return this.#item("act-notice", icon("info", { size: 14 }), h("p", {}, event.message));
    }
    return this.#item("act-step", h("span", { class: "dot" }), h("p", {}, event.message));
  }

  #agentMessage(event) {
    const agent = AGENTS[event.agent] || AGENTS.supervisor;
    return this.#item(`act-say agent-${event.agent || "supervisor"}`, agent.mark,
      h("p", { class: "act-who" }, agent.name),
      h("p", { class: "act-say-text" }, event.content),
    );
  }

  #run(agentKey, runId) {
    if (this.runs.has(runId)) return this.runs.get(runId);
    const agent = AGENTS[agentKey] || { name: agentKey, mark: "?" };
    const status = h("span", { class: "run-status" });
    const log = h("div", { class: "run-log" });
    const root = this.#item(`act-agent agent-${agentKey}`, agent.mark,
      h("div", { class: "run-head" }, h("h3", { class: "run-name" }, agent.name), status),
      log,
    );
    // The log keeps everything the agent reported, in order: notes, its plan, each
    // LangGraph node it ran (with that node's tool calls underneath).
    const run = { root, status, log, plan: null, nodes: new Map(), looseTools: null, lastNote: null };
    this.runs.set(runId, run);
    return run;
  }

  #note(run, text) {
    if (!text || text === run.lastNote) return;
    run.lastNote = text;
    run.looseTools = null;
    run.log.append(h("p", { class: "run-note" }, text));
  }

  #setRunStatus(run, status) {
    run.root.dataset.status = status;
    run.status.replaceChildren(
      status === "running" ? h("span", { class: "spinner", "aria-hidden": "true" })
                           : icon(status === "failed" ? "alert" : "check", { size: 14 }),
      RUN_STATUS[status] || status,
    );
  }

  #agentStarted(event) {
    const run = this.#run(event.agent, event.run_id);
    this.#setRunStatus(run, "running");
    this.#note(run, event.message);
    if (event.plan) this.#planUpdated(event);
    return run.root;
  }

  #agentCompleted(event) {
    const run = this.#run(event.agent, event.run_id);
    this.#setRunStatus(run, event.status === "failed" ? "failed" : "completed");
    this.#note(run, event.message);
    if (event.plan) this.#planUpdated(event);
    return run.root;
  }

  #planUpdated(event) {
    const run = this.runs.get(event.run_id);
    if (!run || !Array.isArray(event.plan) || !event.plan.length) return null;
    if (!run.plan) {
      run.plan = h("ol", { class: "plan", "aria-label": "Plan" });
      run.log.append(run.plan);
      run.looseTools = null;
    }
    run.plan.replaceChildren(...event.plan.map((step) => {
      const status = PLAN_ICON[step.status] ? step.status : "pending";
      return h("li", { class: `plan-step is-${status}` },
        icon(PLAN_ICON[status], { size: 14 }),
        h("span", {}, step.label),
        h("span", { class: "visually-hidden" }, ` (${PLAN_LABEL[status]})`),
      );
    }));
    return run.root;
  }

  #node(event) {
    const run = this.#run(event.agent, event.run_id);
    let node = run.nodes.get(event.node);
    if (!node || (event.type === "node_started" && node.done)) {
      // A node that runs again (search round 2, a retry) gets a fresh row.
      const glyph = h("span", { class: "node-glyph" });
      const text = h("span", { class: "node-text" });
      const tools = h("ul", { class: "tools", hidden: true });
      const root = h("div", { class: "node-step" },
        h("p", { class: "node-line" }, glyph, text, h("code", { class: "node-name" }, event.node)),
        tools,
      );
      node = { root, glyph, text, tools, done: false };
      run.nodes.set(event.node, node);
      run.log.append(root);
      run.looseTools = null;
      run.lastNote = null;
    }
    const finished = event.type === "node_completed";
    const failed = finished && event.status === "failed";
    node.done = finished;
    node.root.dataset.status = failed ? "failed" : finished ? "completed" : "running";
    node.glyph.replaceChildren(finished ? icon(failed ? "alert" : "check", { size: 14 })
                                        : h("span", { class: "spinner", "aria-hidden": "true" }));
    if (event.message) node.text.textContent = event.message;
    return node.root;
  }

  #tool(event) {
    const run = this.#run(event.agent, event.run_id);
    let row = this.toolRows.get(event.call_id);
    if (!row) {
      const glyph = h("span", { class: "tool-glyph" });
      const message = h("p", { class: "tool-message" });
      const result = h("p", { class: "tool-result", hidden: true });
      row = {
        root: h("li", { class: "tool" }, glyph, h("div", { class: "tool-body" },
          h("p", { class: "tool-name" }, TOOLS[event.tool] || event.tool, " ", h("code", {}, event.tool)),
          message, result,
        )),
        glyph, message, result,
      };
      this.toolRows.set(event.call_id, row);
      const node = event.node ? run.nodes.get(event.node) : null;
      let list = node?.tools;
      if (!list) {
        if (!run.looseTools) {
          run.looseTools = h("ul", { class: "tools" });
          run.log.append(run.looseTools);
        }
        list = run.looseTools;
      }
      list.hidden = false;
      list.append(row.root);
    }
    const status = event.type === "tool_started" ? "running" : event.status;
    row.root.dataset.status = status;
    row.glyph.replaceChildren(
      status === "running" ? h("span", { class: "spinner", "aria-hidden": "true" })
                           : icon(TOOL_ICON[status] || "check", { size: 15 }),
    );
    if (event.message) row.message.textContent = event.message;
    if (event.result) {
      row.result.textContent = event.result;
      row.result.hidden = false;
    }
    return row.root;
  }

  #approval(event) {
    const stage = STAGES[event.stage];
    if (!stage) return null;
    const summary = event.summary || {};
    const error = h("p", { class: "approval-error", role: "alert", hidden: true });

    // Compact card: counts at the top, the few sections marked "open" (the summary,
    // anything flagged for you), and the rest behind "Review strategy". Approvals
    // saved before stats existed are shown in full, as they were.
    const compact = Array.isArray(summary.stats) && summary.stats.length > 0;
    const all = summary.sections || [];
    const shown = compact ? all.filter((s) => s.open) : all;
    const folded = compact ? all.filter((s) => !s.open) : [];
    let review = null;
    if (folded.length) {
      const detailsId = `approval-details-${event.id}`;
      const details = h("div", { class: "approval-sections approval-details", id: detailsId, hidden: true },
        renderSections(folded));
      const label = h("span", {}, stage.review[0]);
      const toggle = h("button", {
        type: "button", class: "approval-toggle", "aria-expanded": "false", "aria-controls": detailsId,
        onclick: () => {
          const open = details.hidden;
          details.hidden = !open;
          toggle.setAttribute("aria-expanded", String(open));
          label.textContent = stage.review[open ? 1 : 0];
        },
      }, label, icon("chevron", { size: 16 }));
      review = [toggle, details];
    }

    const actions = h("div", { class: "approval-actions" });
    let modifyForm = null;

    if (stage.approve) {
      actions.append(h("button", {
        type: "button", class: "btn btn-primary",
        onclick: () => this.handlers.onApprove(event.stage),
      }, stage.approve));
    }
    if (stage.other) {
      const feedback = h("textarea", { class: "textarea", id: `feedback-${event.id}`, rows: 3, maxlength: 1000 });
      modifyForm = h("form", { class: "approval-modify", hidden: true },
        h("label", { class: "field-label", for: `feedback-${event.id}` }, "What should change?"),
        feedback,
        h("div", { class: "approval-actions" },
          h("button", { type: "submit", class: "btn btn-primary btn-sm" }, "Send changes"),
          h("button", { type: "button", class: "btn btn-quiet btn-sm", onclick: () => {
            modifyForm.hidden = true;
            actions.hidden = false;
          } }, "Cancel"),
        ),
      );
      modifyForm.addEventListener("submit", (e) => {
        e.preventDefault();
        const text = feedback.value.trim();
        if (!text) {
          error.textContent = "Say what should change before sending.";
          error.hidden = false;
          feedback.focus();
          return;
        }
        this.handlers.onModify(event.stage, text);
      });
      actions.append(h("button", { type: "button", class: "btn btn-secondary", onclick: () => {
        actions.hidden = true;
        modifyForm.hidden = false;
        feedback.focus();
      } }, stage.other.label));
    }
    if (stage.cancel) {
      let confirmTimer = null;
      const cancel = h("button", { type: "button", class: "btn btn-quiet approval-cancel", onclick: () => {
        if (!confirmTimer) {
          cancel.textContent = stage.cancel[1];
          cancel.classList.add("is-confirming");
          confirmTimer = window.setTimeout(() => {
            confirmTimer = null;
            cancel.textContent = stage.cancel[0];
            cancel.classList.remove("is-confirming");
          }, 5000);
          return;
        }
        window.clearTimeout(confirmTimer);
        this.handlers.onCancel?.(event.stage);
      } }, stage.cancel[0]);
      actions.append(cancel);
    }
    if (event.stage === "content" && this.lastBlog) {
      const blogId = this.lastBlog;
      actions.append(h("button", { type: "button", class: "btn btn-primary",
        onclick: () => this.handlers.onOpenDraft(blogId) }, "Open draft"));
    }

    const outcome = h("p", { class: "approval-outcome", hidden: true });
    const card = h("section", { class: "approval", "aria-label": "Your approval is needed" },
      h("header", { class: "approval-head" },
        h("h3", {}, "Your approval is needed"),
        h("p", { class: "approval-stage" }, stage.title),
      ),
      event.message ? h("p", { class: "approval-message" }, event.message) : null,
      compact ? renderStats(summary.stats, "summary-stats approval-stats")
              : summary.headline ? h("p", { class: "approval-headline" }, summary.headline) : null,
      shown.length ? h("div", { class: "approval-sections" }, renderSections(shown)) : null,
      review,
      h("p", { class: "approval-question" }, stage.question),
      actions, modifyForm, error, outcome,
    );

    const li = this.#item("act-approval", "!", card);
    this.approvals.set(event.stage, { root: card, actions, modifyForm, error, outcome });
    return li;
  }

  #approvalResolved(event) {
    const card = this.approvals.get(event.stage);
    if (!card) return null;
    card.root.classList.add("is-resolved");
    card.actions.hidden = true;
    if (card.modifyForm) card.modifyForm.hidden = true;
    card.error.hidden = true;
    const text = {
      approve: "Approved. The workflow continued.",
      modify: "Changes requested. The Analysis Agent is revising the research.",
      regenerate: "Changes requested. The Generation Agent is revising the draft.",
      reject: "Request cancelled. Nothing was written or published.",
    }[event.decision] || "Decision recorded.";
    card.outcome.replaceChildren(icon(event.decision === "reject" ? "close" : "check", { size: 14 }), text);
    card.outcome.hidden = false;
    this.approvals.delete(event.stage);
    return card.root;
  }

  #blogReady(event) {
    this.lastBlog = event.blog_id;
    // A new draft replaces the previous one; the old entries stay, marked as older versions.
    for (const old of this.root.querySelectorAll(".act-blog:not(.is-old)")) {
      old.classList.add("is-old");
      old.querySelector(".blog-label").textContent = "Older version: ";
      old.querySelector("button").textContent = "View";
    }
    return this.#item("act-blog", icon("doc", { size: 14 }),
      h("p", {}, h("span", { class: "blog-label" }, "Draft ready: "), h("strong", {}, event.title || "Untitled")),
      h("button", { type: "button", class: "btn btn-secondary btn-sm",
        onclick: () => this.handlers.onOpenDraft(event.blog_id) }, "Open draft"),
    );
  }

  #blogPublished(event) {
    return this.#item("act-published", icon("check", { size: 14 }),
      h("p", {}, "Published: ", h("strong", {}, event.title)),
      h("a", { class: "btn btn-secondary btn-sm", href: `/blog/${encodeURIComponent(event.slug)}`,
        target: "_blank", rel: "noopener" }, "View post"),
    );
  }

  #error(event) {
    // Only the latest error offers Retry; earlier ones are history.
    for (const old of this.root.querySelectorAll(".act-error .retry-button")) old.remove();
    return this.#item("act-error", icon("alert", { size: 14 }),
      h("p", {}, event.message),
      event.retryable
        ? h("button", { type: "button", class: "btn btn-secondary btn-sm retry-button",
            onclick: (e) => {
              e.currentTarget.disabled = true;
              this.handlers.onRetry();
            } }, "Retry")
        : null,
    );
  }
}
