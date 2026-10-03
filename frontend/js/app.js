/**
 * Dashboard controller.
 *
 * Flow: the user sends a message -> POST /api/chat starts (or continues) a
 * session -> the backend publishes events -> the live stream delivers them ->
 * the timeline draws them. The page URL carries ?session=<id> so a reload
 * replays the same chat.
 */

import { api, openEventStream } from "./api.js";
import { h, toast } from "./dom.js";
import { BlogEditor } from "./editor.js";
import { renderContext, renderContextError } from "./context-panel.js";
import { Timeline } from "./timeline.js";

const $ = (id) => document.getElementById(id);

const els = {
  workspace: $("workspace"),
  workspaceName: $("workspace-name"),
  status: $("session-status"),
  statusText: $("session-status-text"),
  newChat: $("new-chat"),
  contextToggle: $("context-toggle"),
  contextPanel: $("context-panel"),
  contextBody: $("context-body"),
  threadScroll: $("thread-scroll"),
  emptyState: $("empty-state"),
  emptyTitle: $("empty-title"),
  suggestions: $("suggestions"),
  composer: $("composer"),
  input: $("composer-input"),
  send: $("composer-send"),
  composerError: $("composer-error"),
  composerHint: $("composer-hint"),
  count: $("composer-count"),
  scrim: $("scrim"),
};

const MAX_MESSAGE = 2000;
const OFFLINE_GRACE_MS = 4000;

const state = {
  sessionId: null,
  lastEventId: 0,
  stream: null,
  sending: false,
  statusTimer: null,
  offlineTimer: null,
  lastBlogId: null,       // the newest draft in this chat: the only one that can be edited or published
  draftDetails: new Map(), // blog id -> its review summary (outline, keywords, readability...)
  contentPending: false,  // is the newest draft waiting for review right now?
};

/* Workflow status -> what the top bar says. */
const STATUS_LABELS = {
  REQUESTED: ["Request received", "working"],
  PLANNING: ["Supervisor is planning", "working"],
  RESEARCHING: ["Analysis Agent is researching", "working"],
  ANALYZING: ["Analysis Agent is analyzing", "working"],
  WAITING_FOR_RESEARCH_APPROVAL: ["Research approval required", "waiting"],
  GENERATING: ["Generation Agent is writing", "working"],
  WAITING_FOR_CONTENT_APPROVAL: ["Content approval required", "waiting"],
  PUBLISHING: ["Publishing", "working"],
  COMPLETED: ["Done", "done"],
  CANCELLED: ["Request cancelled", "done"],
  FAILED: ["Stopped with an error", "failed"],
};

/* While a node runs, the top bar names the agent and the step: "Analysis Agent → Keyword Analysis". */
const AGENT_NAMES = { supervisor: "Supervisor", analysis: "Analysis Agent", generation: "Generation Agent" };
const STEP_LABELS = {
  create_plan: "Planning",
  route_task: "Routing",
  prepare_research: "Research Planning",
  discover_competitors: "Competitor Discovery",
  search_competitor_content: "Web Search",
  scrape_content: "Web Scraping",
  analyze_competitors: "Competitor Analysis",
  analyze_keywords: "Keyword Analysis",
  identify_content_gaps: "Content Gaps",
  create_research_summary: "Research Summary",
  prepare_generation: "Reading the Research",
  build_outline: "Outline",
  generate_blog: "Writing",
  optimize_blog: "SEO Optimization",
  prepare_blog_review: "Saving the Draft",
  plan_revision: "Revision Plan",
  revise_blog: "Revising Sections",
  publish_blog: "Publishing",
};

function stepLabel(agent, node) {
  if (!AGENT_NAMES[agent] || !STEP_LABELS[node]) return null;
  return `${AGENT_NAMES[agent]} → ${STEP_LABELS[node]}`;
}

// ---------------------------------------------------------------------------
// Timeline + editor
// ---------------------------------------------------------------------------

const timeline = new Timeline($("timeline"), {
  onApprove: (stage) => decide(stage, "approve"),
  onModify: (stage, feedback) => decide(stage, "modify", feedback),
  onCancel: (stage) => decide(stage, "reject"),
  onOpenDraft: (blogId) => openDraft(blogId),
  onRetry: () => retry(),
});

const editor = new BlogEditor(
  {
    panel: $("editor-panel"),
    title: $("editor-title"),
    preview: $("editor-preview"),
    source: $("editor-source"),
    status: $("editor-status"),
    error: $("editor-error"),
    edit: $("editor-edit"),
    regenerate: $("editor-regenerate"),
    publish: $("editor-publish"),
    close: $("editor-close"),
    actions: $("editor-actions"),
    regenForm: $("editor-regen"),
    regenText: $("editor-regen-text"),
    regenSend: $("editor-regen-send"),
    regenCancel: $("editor-regen-cancel"),
    details: $("editor-details"),
    detailsMeta: $("editor-details-meta"),
    detailsBody: $("editor-details-body"),
    note: $("editor-note"),
    noteText: $("editor-note-text"),
    noteAction: $("editor-note-action"),
    download: $("editor-download"),
    downloadToggle: $("editor-download-toggle"),
    downloadMenu: $("editor-download-menu"),
  },
  {
    onSave: (blogId, changes) => api.updateBlog(blogId, changes),
    onOpenLatest: () => state.lastBlogId && openDraft(state.lastBlogId),
    onRegenerate: (_blog, feedback) =>
      api.decideContent(state.sessionId, "regenerate", feedback || undefined).then(refreshStatusSoon),
    onPublish: () => api.decideContent(state.sessionId, "approve").then(refreshStatusSoon),
    onOpenChange: (open) => {
      els.workspace.dataset.editor = open ? "open" : "closed";
      updateScrim();
    },
  },
);

// ---------------------------------------------------------------------------
// Status
// ---------------------------------------------------------------------------

function setStatus(label, tone) {
  els.status.dataset.state = tone;
  els.statusText.textContent = label;
}

function showWorkflowStatus(status) {
  if (!status) {
    setStatus("No active chat", "idle");
    return;
  }
  let [label, tone] = STATUS_LABELS[status.workflow_status] || [status.workflow_status, "idle"];
  if (tone === "working") label = stepLabel(status.current_agent, status.current_node) || label;
  if (status.workflow_status === "COMPLETED" && status.publish_status === "published") {
    label = "Blog published successfully";
  }
  setStatus(label, tone);
}

/** The same labels, straight from live events (no wait for the next status poll). */
function showEventStatus(event) {
  if (event.type === "node_started") {
    const label = stepLabel(event.agent, event.node);
    if (label) setStatus(label, "working");
  } else if (event.type === "approval_required") {
    setStatus(event.stage === "research" ? "Research approval required" : "Content approval required", "waiting");
  } else if (event.type === "blog_published") {
    setStatus("Blog published successfully", "done");
  } else if (event.type === "error") {
    setStatus("Stopped with an error", "failed");
  }
}

/** The editor is read-only unless the current draft is waiting for review. */
function syncEditorAccess() {
  if (!editor.blogId) return;
  editor.setAccess({ current: editor.blogId === state.lastBlogId, locked: !state.contentPending });
}

function refreshStatusSoon() {
  window.clearTimeout(state.statusTimer);
  const sessionId = state.sessionId;
  if (!sessionId) return;
  state.statusTimer = window.setTimeout(async () => {
    try {
      const status = await api.getStatus(sessionId);
      if (sessionId === state.sessionId) showWorkflowStatus(status);
    } catch (err) {
      if (err.code === "network_error") setStatus("Can't reach the server", "offline");
    }
  }, 250);
}

// ---------------------------------------------------------------------------
// Events
// ---------------------------------------------------------------------------

// Follow new events while the reader is at the bottom; stop if they scroll up to read.
let followNewEvents = true;

function isNearBottom() {
  const el = els.threadScroll;
  return el.scrollHeight - el.scrollTop - el.clientHeight < 160;
}

function scrollToBottom() {
  els.threadScroll.scrollTop = els.threadScroll.scrollHeight;
}

function handleEvent(event, { replay = false } = {}) {
  if (!event || typeof event.id !== "number" || event.id <= state.lastEventId) return;
  state.lastEventId = event.id;
  els.emptyState.hidden = true;

  const stick = replay || followNewEvents || (event.type === "message" && event.role === "user");
  timeline.add(event);

  // Track which draft is current and whether it's waiting for review, so the editor
  // only allows edits, rewrites and Publish on the newest draft while the graph is paused for it.
  if (event.type === "blog_ready") state.lastBlogId = event.blog_id;
  if (event.type === "approval_required" && event.stage === "content" && state.lastBlogId) {
    state.contentPending = true;
    state.draftDetails.set(state.lastBlogId, event.summary);  // also feeds the editor's "Outline and SEO"
    if (editor.blogId === state.lastBlogId) editor.setDetails(event.summary);
  }
  if ((event.type === "approval_resolved" && event.stage === "content") || event.type === "blog_published") {
    state.contentPending = false;
  }
  if (["blog_ready", "approval_required", "approval_resolved", "blog_published"].includes(event.type)) {
    syncEditorAccess();
  }
  if (stick) {
    scrollToBottom();
    followNewEvents = true;
  }

  if (!replay) {
    showEventStatus(event);
    if (event.type === "blog_ready") openDraft(event.blog_id);
    if (event.type === "blog_published" && editor.blogId === event.blog_id) openDraft(event.blog_id);
    if (event.type === "approval_required" && event.stage === "research") loadContext(); // research numbers
    refreshStatusSoon();
  }
}

function openStream() {
  closeStream();
  const sessionId = state.sessionId;
  state.stream = openEventStream(sessionId, state.lastEventId, {
    onEvent: (event) => {
      if (sessionId === state.sessionId) handleEvent(event);
    },
    onConnectionChange: (connection) => {
      if (connection === "open") {
        window.clearTimeout(state.offlineTimer);
        refreshStatusSoon();
      } else {
        // The stream reconnects every minute by design; only complain if it stays down.
        window.clearTimeout(state.offlineTimer);
        state.offlineTimer = window.setTimeout(
          () => setStatus("Live updates paused, reconnecting", "offline"), OFFLINE_GRACE_MS);
      }
    },
  });
}

function closeStream() {
  state.stream?.close();
  state.stream = null;
  window.clearTimeout(state.offlineTimer);
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

async function loadSession(sessionId) {
  try {
    const { session, events } = await api.getSession(sessionId);
    state.sessionId = session.id;
    resetThread();
    for (const event of events) handleEvent(event, { replay: true });
    els.emptyState.hidden = !timeline.isEmpty;
    showWorkflowStatus({ workflow_status: session.status, running: false });
    refreshStatusSoon();
    openStream();

    const lastDraft = [...events].reverse().find((e) => e.type === "blog_ready");
    if (lastDraft) openDraft(lastDraft.blog_id);
  } catch (err) {
    if (err.status === 404) {
      toast("That chat no longer exists. Starting a new one.");
      newChat();
    } else {
      toast(err.message, "error", 7000);
    }
  }
}

function resetThread() {
  state.lastEventId = 0;
  state.lastBlogId = null;
  state.draftDetails.clear();
  state.contentPending = false;
  timeline.reset();
}

function newChat() {
  closeStream();
  window.clearTimeout(state.statusTimer);
  state.sessionId = null;
  resetThread();
  editor.close();
  els.emptyState.hidden = false;
  showComposerError("");
  showWorkflowStatus(null);
  history.replaceState(null, "", "/app");
  els.input.focus();
}

// ---------------------------------------------------------------------------
// Composer
// ---------------------------------------------------------------------------

function showComposerError(message, action) {
  els.composerError.replaceChildren();
  if (message) {
    els.composerError.append(message);
    if (action) els.composerError.append(" ", action);
  }
  els.composerError.hidden = !message;
  els.composerHint.hidden = Boolean(message);
}

function autosize() {
  els.input.style.height = "auto";
  els.input.style.height = `${Math.min(els.input.scrollHeight, 200)}px`;
  const length = els.input.value.length;
  els.count.hidden = length < MAX_MESSAGE - 200;
  els.count.textContent = `${length} / ${MAX_MESSAGE}`;
  els.count.classList.toggle("is-over", length > MAX_MESSAGE);
}

function setSending(sending) {
  state.sending = sending;
  els.send.disabled = sending;
  els.send.textContent = sending ? "Sending" : "Send";
  els.input.setAttribute("aria-busy", String(sending));
}

async function send(text) {
  const message = (text ?? els.input.value).trim();
  if (state.sending) return;
  if (message.length < 2) {
    showComposerError("Type a request first, for example: Write me a blog about Agentic AI.");
    els.input.focus();
    return;
  }
  if (message.length > MAX_MESSAGE) {
    showComposerError(`Keep the message under ${MAX_MESSAGE} characters.`);
    return;
  }

  showComposerError("");
  setSending(true);
  try {
    // Feedback typed here revises the draft as saved, so save the editor's edits first.
    if (editor.hasUnsavedChanges && !(await editor.saveChanges())) {
      showComposerError("Your edits in the editor couldn't be saved. Fix them there, then send again.");
      return;
    }
    const { session } = await api.sendMessage(message, state.sessionId);
    if (session.id !== state.sessionId) {
      state.sessionId = session.id;
      resetThread();
      history.replaceState(null, "", `/app?session=${session.id}`);
    }
    if (!state.stream) openStream();
    if (text === undefined) {
      els.input.value = "";
      autosize();
    }
    showWorkflowStatus({ workflow_status: session.status, running: true });
    refreshStatusSoon();
  } catch (err) {
    if (err.code === "onboarding_required") {
      showComposerError(err.message, h("a", { href: "/onboarding" }, "Set it up"));
    } else if (err.code === "session_not_found") {
      newChat();
      showComposerError("That chat was removed. Your message is still here; send it again to start a new chat.");
      els.input.value = message;
      autosize();
    } else {
      showComposerError(err.message);
    }
  } finally {
    setSending(false);
  }
}

// ---------------------------------------------------------------------------
// Approvals and drafts
// ---------------------------------------------------------------------------

async function decide(stage, decision, feedback) {
  timeline.setApprovalBusy(stage, true);
  timeline.setApprovalError(stage, "");
  try {
    const call = stage === "research" ? api.decideResearch : api.decideContent;
    await call(state.sessionId, decision, feedback);
    refreshStatusSoon();
  } catch (err) {
    timeline.setApprovalError(stage, err.message);
  } finally {
    timeline.setApprovalBusy(stage, false);
  }
}

async function retry() {
  if (!state.sessionId) return;
  try {
    await api.retrySession(state.sessionId);
    refreshStatusSoon();
  } catch (err) {
    toast(err.message, "error");
  }
}

async function openDraft(blogId) {
  try {
    const { blog } = await api.getBlog(blogId);
    editor.open(blog, state.draftDetails.get(blogId) || null,
                { current: blogId === state.lastBlogId, locked: !state.contentPending });
  } catch (err) {
    toast(err.message, "error");
  }
}

// ---------------------------------------------------------------------------
// Context panel
// ---------------------------------------------------------------------------

function lowerFirst(text) {
  return /^[A-Z][a-z]/.test(text) ? text[0].toLowerCase() + text.slice(1) : text;
}

function renderSuggestions(context) {
  const { company, competitors } = context;
  const ideas = ["Write me a blog about Agentic AI"];
  if (company.target_audience?.[0]) ideas.push(`Write a practical how-to guide for ${lowerFirst(company.target_audience[0])}`);
  if (competitors?.[0]) ideas.push(`Write a post on what ${company.name} does differently from ${competitors[0].name}`);

  els.suggestions.replaceChildren(...ideas.map((idea) =>
    h("li", {}, h("button", { type: "button", class: "suggestion", onclick: () => {
      els.input.value = idea;
      autosize();
      els.input.focus();
    } }, idea))));
}

async function loadContext() {
  try {
    const [context, health] = await Promise.all([
      api.getContext(),
      api.getHealth().catch(() => null),
    ]);
    if (!context.onboarded) {
      window.location.assign("/onboarding");
      return;
    }
    renderContext(els.contextBody, context, health);
    els.workspaceName.textContent = context.company.name;
    els.emptyTitle.textContent = `What should we write for ${context.company.name}?`;
    renderSuggestions(context);
  } catch (err) {
    renderContextError(els.contextBody, err.message, loadContext);
  }
}

// ---------------------------------------------------------------------------
// Drawers on narrow screens
// ---------------------------------------------------------------------------

const narrowEditor = window.matchMedia("(max-width: 1180px)");
const narrowContext = window.matchMedia("(max-width: 900px)");

function setContextOpen(open) {
  els.workspace.dataset.context = open ? "open" : "closed";
  els.contextToggle.setAttribute("aria-expanded", String(open));
  if (open) els.contextPanel.focus({ preventScroll: true });
  updateScrim();
}

function updateScrim() {
  const editorOverlay = narrowEditor.matches && editor.isOpen;
  const contextOverlay = narrowContext.matches && els.workspace.dataset.context === "open";
  els.scrim.hidden = !(editorOverlay || contextOverlay);
}

// ---------------------------------------------------------------------------
// Wire up
// ---------------------------------------------------------------------------

els.composer.addEventListener("submit", (e) => {
  e.preventDefault();
  send();
});

els.input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
});

els.input.addEventListener("input", () => {
  autosize();
  if (!els.composerError.hidden) showComposerError("");
});

els.threadScroll.addEventListener("scroll", () => { followNewEvents = isNearBottom(); }, { passive: true });
els.newChat.addEventListener("click", newChat);
els.contextToggle.addEventListener("click", () => setContextOpen(els.workspace.dataset.context !== "open"));
els.scrim.addEventListener("click", () => {
  setContextOpen(false);
  if (narrowEditor.matches) editor.close();
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (els.workspace.dataset.context === "open") setContextOpen(false);
  else if (narrowEditor.matches && editor.isOpen) editor.close();
});
narrowEditor.addEventListener("change", updateScrim);
narrowContext.addEventListener("change", updateScrim);

loadContext();
const initialSession = new URLSearchParams(window.location.search).get("session");
if (initialSession) loadSession(initialSession);
else showWorkflowStatus(null);
