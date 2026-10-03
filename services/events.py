"""
The UI event model.

Everything the chat timeline shows is an event created here, stored in the
`events` table and streamed to the browser. Nodes never build event dicts by
hand; they use `WorkflowEmitter`, so the shape stays consistent.

Event types
-----------
message            {role: "user" | "assistant", content, agent?}
workflow           {status, message, tone}             a workflow-level step or status change
agent_started      {agent, run_id, message, plan?}     an agent picked up work
agent_completed    {agent, run_id, status, message}    status: "completed" | "failed"
plan_updated       {run_id, plan}                      the Supervisor's plan moved on
node_started       {agent, run_id, node, message}      a LangGraph node began
node_completed     {agent, run_id, node, status, message}
tool_started       {tool, agent, run_id, node, call_id, message}
tool_completed     {tool, agent, run_id, node, call_id, status, message, result}
                   status: "completed" | "skipped" (not configured, nothing invented) | "failed"
approval_required  {stage: "research" | "content", agent, node, message, summary}
approval_resolved  {stage, agent, node, decision, feedback?}      decision "reject" = Cancel request
blog_ready         {agent, node, run_id, blog_id, title}
blog_published     {agent, node, run_id, blog_id, title, slug}
error              {message, stage?, retryable}

Every event a workflow run emits also carries `workflow_id` (its LangGraph
thread), and the events table adds the session id and a timestamp, so a run can
be reconstructed step by step (GET /api/sessions/<id>/trace).

Only observable actions are recorded: what ran, with what result. Hidden model
reasoning is never stored or shown.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from typing import Any, Iterable, Iterator, Optional

from storage.database import Database
from storage.repositories import EventRepository, SessionRepository

log = logging.getLogger(__name__)

AGENTS = {"supervisor", "analysis", "generation"}
RUN_STATUSES = {"completed", "failed"}
TOOL_STATUSES = {"completed", "skipped", "failed"}
PLAN_STATUSES = {"pending", "active", "done", "skipped"}
APPROVAL_STAGES = {"research", "content"}


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _check(value: str, allowed: Iterable[str], name: str) -> str:
    if value not in allowed:
        raise ValueError(f"Invalid {name} '{value}'. Expected one of: {sorted(allowed)}")
    return value


def _check_plan(plan: list[dict]) -> list[dict]:
    for step in plan:
        _check(step.get("status", "pending"), PLAN_STATUSES, "plan step status")
    return plan


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def message_event(role: str, content: str, agent: Optional[str] = None) -> dict:
    _check(role, {"user", "assistant"}, "role")
    event = {"type": "message", "role": role, "content": content}
    if agent:
        event["agent"] = _check(agent, AGENTS, "agent")
    return event


def workflow_event(status: str, message: str, tone: str = "neutral") -> dict:
    """`tone` is "neutral" for routine steps, "notice" for something the user should read."""
    return {"type": "workflow", "status": status, "message": message,
            "tone": _check(tone, {"neutral", "notice"}, "tone")}


def agent_started_event(agent: str, run_id: str, message: str = "", plan: Optional[list] = None) -> dict:
    event = {"type": "agent_started", "agent": _check(agent, AGENTS, "agent"), "run_id": run_id,
             "message": message}
    if plan:
        event["plan"] = _check_plan(plan)
    return event


def agent_completed_event(agent: str, run_id: str, message: str = "", status: str = "completed") -> dict:
    return {"type": "agent_completed", "agent": _check(agent, AGENTS, "agent"), "run_id": run_id,
            "status": _check(status, RUN_STATUSES, "status"), "message": message}


def plan_updated_event(run_id: str, plan: list[dict]) -> dict:
    return {"type": "plan_updated", "run_id": run_id, "plan": _check_plan(plan)}


def node_started_event(agent: str, run_id: str, node: str, message: str) -> dict:
    return {"type": "node_started", "agent": _check(agent, AGENTS, "agent"), "run_id": run_id,
            "node": node, "message": message}


def node_completed_event(agent: str, run_id: str, node: str, message: str, status: str = "completed") -> dict:
    return {"type": "node_completed", "agent": _check(agent, AGENTS, "agent"), "run_id": run_id,
            "node": node, "status": _check(status, RUN_STATUSES, "status"), "message": message}


def tool_started_event(tool: str, agent: str, run_id: str, node: Optional[str], call_id: str,
                       message: str) -> dict:
    return {"type": "tool_started", "tool": tool, "agent": _check(agent, AGENTS, "agent"),
            "run_id": run_id, "node": node, "call_id": call_id, "message": message}


def tool_completed_event(tool: str, agent: str, run_id: str, node: Optional[str], call_id: str,
                         status: str, message: str, result: str) -> dict:
    return {"type": "tool_completed", "tool": tool, "agent": _check(agent, AGENTS, "agent"),
            "run_id": run_id, "node": node, "call_id": call_id,
            "status": _check(status, TOOL_STATUSES, "status"), "message": message, "result": result}


def approval_required_event(stage: str, summary: dict[str, Any], message: str = "",
                            node: Optional[str] = None) -> dict:
    """
    `summary` is rendered by the approval card:
        {"headline": str,
         "stats": [{"label": str, "value": str | int}, ...]?,          counts shown at the top
         "sections": [{"title": str, "items": [str, ...], "note": str?,
                       "open": bool?}, ...]}                          open = visible without expanding
    """
    return {"type": "approval_required", "stage": _check(stage, APPROVAL_STAGES, "stage"),
            "agent": "supervisor", "node": node or f"request_{stage}_approval",
            "message": message, "summary": summary}


def approval_resolved_event(stage: str, decision: str, feedback: Optional[str] = None) -> dict:
    event = {"type": "approval_resolved", "stage": _check(stage, APPROVAL_STAGES, "stage"),
             "agent": "supervisor", "node": f"request_{stage}_approval", "decision": decision}
    if feedback:
        event["feedback"] = feedback
    return event


def blog_ready_event(blog_id: str, title: str, run_id: Optional[str] = None, node: Optional[str] = None) -> dict:
    event = {"type": "blog_ready", "agent": "generation", "blog_id": blog_id, "title": title}
    if run_id:
        event["run_id"] = run_id
    if node:
        event["node"] = node
    return event


def blog_published_event(blog_id: str, title: str, slug: str, run_id: Optional[str] = None) -> dict:
    event = {"type": "blog_published", "agent": "supervisor", "node": "publish_blog",
             "blog_id": blog_id, "title": title, "slug": slug}
    if run_id:
        event["run_id"] = run_id
    return event


def error_event(message: str, stage: Optional[str] = None, retryable: bool = False) -> dict:
    event = {"type": "error", "message": message, "retryable": retryable}
    if stage:
        event["stage"] = stage
    return event


class EventPublisher:
    """Stores events so the live stream (and a page reload) can pick them up."""

    def __init__(self, db: Database):
        self.events = EventRepository(db)

    def publish(self, session_id: str, event: dict) -> dict:
        return self.events.add(session_id, event)


# ---------------------------------------------------------------------------
# The emitter nodes use while the workflow runs
# ---------------------------------------------------------------------------

class ToolCall:
    """One tool call: tool_started now, tool_completed when the node reports the outcome."""

    def __init__(self, emitter: "WorkflowEmitter", tool: str, message: str):
        agent, run_id, node = emitter.current
        self.emitter, self.tool, self.message = emitter, tool, message
        self.agent, self.run_id, self.node = agent, run_id, node
        self.call_id = new_id("call")
        self.record: Optional[dict] = None
        emitter.publish(tool_started_event(tool, agent, run_id, node, self.call_id, message))

    def complete(self, result: str, message: Optional[str] = None) -> None:
        self._finish("completed", result, message)

    def skip(self, reason: str) -> None:
        self._finish("skipped", reason, None)

    def fail(self, reason: str) -> None:
        self._finish("failed", reason, None)

    def _finish(self, status: str, result: str, message: Optional[str]) -> None:
        if self.record is None:
            self.emitter.publish(tool_completed_event(self.tool, self.agent, self.run_id, self.node,
                                                      self.call_id, status, message or self.message, result))
            self.record = {"tool": self.tool, "status": status, "summary": result}


class NodeStep:
    def __init__(self, emitter: "WorkflowEmitter", agent: str, run_id: str, node: str):
        self.emitter, self.agent, self.run_id, self.node = emitter, agent, run_id, node
        self.done = False

    def complete(self, message: str) -> None:
        if not self.done:
            self.done = True
            self.emitter.publish(node_completed_event(self.agent, self.run_id, self.node, message))


class WorkflowEmitter:
    """
    What nodes use to report progress. Each call stores an event (streamed to the
    browser) and, where it matters, updates the session row /api/status reads.
    """

    def __init__(self, db: Database, session_id: str, workflow_id: Optional[str] = None):
        self.session_id = session_id
        self.workflow_id = workflow_id           # the LangGraph thread this run's state lives in
        self.events = EventRepository(db)
        self.sessions = SessionRepository(db)
        self.open_runs: dict[str, str] = {}      # run_id -> agent, for runs still working
        self.current: tuple[str, str, Optional[str]] = ("supervisor", "", None)

    def publish(self, event: dict) -> dict:
        if self.workflow_id:
            event = {**event, "workflow_id": self.workflow_id}
        return self.events.add(self.session_id, event)

    def update_session(self, **changes) -> None:
        self.sessions.update(self.session_id, **changes)

    # -- workflow-level -----------------------------------------------------

    def workflow(self, status: str, message: str, tone: str = "neutral") -> None:
        self.publish(workflow_event(status, message, tone))

    def message(self, agent: str, content: str) -> None:
        self.publish(message_event("assistant", content, agent=agent))

    # -- agents ---------------------------------------------------------------

    def agent_started(self, agent: str, run_id: str, message: str = "", plan: Optional[list] = None) -> None:
        self.open_runs[run_id] = agent
        self.publish(agent_started_event(agent, run_id, message, plan))

    def agent_completed(self, agent: str, run_id: str, message: str = "", status: str = "completed") -> None:
        self.open_runs.pop(run_id, None)
        self.publish(agent_completed_event(agent, run_id, message, status))

    def plan_updated(self, run_id: str, plan: list[dict]) -> None:
        if run_id:
            self.publish(plan_updated_event(run_id, plan))

    # -- nodes and tools ------------------------------------------------------

    @contextmanager
    def node(self, agent: str, run_id: str, node: str, status: str, message: str) -> Iterator[NodeStep]:
        """
        with emit.node("analysis", run_id, "scrape_content", "RESEARCHING", "Reading pages") as step:
            ...
            step.complete("Read 4 of 5 pages")

        Emits node_started, records the active agent/node for /api/status, and emits
        node_completed (or a failed node_completed if the block raises).
        """
        self.current = (agent, run_id, node)
        self.sessions.update(self.session_id, current_agent=agent, current_node=node, status=status)
        self.publish(node_started_event(agent, run_id, node, message))
        step = NodeStep(self, agent, run_id, node)
        try:
            yield step
        except Exception:
            if not step.done:
                step.done = True
                self.publish(node_completed_event(agent, run_id, node, "Stopped before finishing", "failed"))
            raise
        step.complete(message)

    @contextmanager
    def tool(self, tool: str, message: str) -> Iterator[ToolCall]:
        """
        with emit.tool("google_search", "Searching...") as call:
            result = google_search.search(...)
            call.complete("8 results")

        Belongs to the node currently running. If the block raises, the call is marked failed.
        """
        call = ToolCall(self, tool, message)
        try:
            yield call
        except Exception:
            call.fail("The tool stopped unexpectedly.")
            raise
        if call.record is None:
            call.complete("Done")

    def fail_open_runs(self, message: str) -> None:
        """Mark any agent still shown as working as failed (used when a step errors)."""
        for run_id, agent in list(self.open_runs.items()):
            self.agent_completed(agent, run_id, message, status="failed")
