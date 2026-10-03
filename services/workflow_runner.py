"""
The workflow runner: the single boundary between Flask and LangGraph.

Routes never touch the graph. They call:

    runner.submit_message(session_id, message)        POST /api/chat (a new request, or feedback on a draft)
    runner.resume(session_id, stage, decision, ...)   POST /api/approval/research | /content
    runner.retry(session_id)                          POST /api/sessions/<id>/retry

Each call runs the graph on a background thread so the HTTP request returns at
once; the browser follows along through the live event stream.

How a pause works
-----------------
The approval nodes call LangGraph's `interrupt()`. `graph.invoke()` then returns
early, the checkpointer has saved the full state to data/checkpoints.db, and
`_settle()` sees the pending interrupt, shows the approval card and marks the
chat "waiting". When the user decides, `resume()` invokes the graph again with
`Command(resume=...)` and it continues from the approval node. Because state is
in SQLite, this works even if the server restarted in between.

Each run of a chat gets its own LangGraph thread id, so a follow-up request
starts clean instead of inheriting the previous run's research.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import uuid
from typing import Any, Callable, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from config import Settings
from graph.context import StepFailed, WorkflowContext
from graph.state import ACTIVE_STATUSES, WAITING_STATUSES, WorkflowStatus
from graph.workflow import build_workflow
from llm.provider import get_chat_model
from services.events import WorkflowEmitter, approval_required_event, error_event, message_event
from storage.database import Database
from storage.repositories import CompanyRepository, EventRepository, SessionRepository
from supervisor.supervisor_agent import Supervisor

log = logging.getLogger(__name__)

RESUME_STATUS = {
    ("research", "approve"): WorkflowStatus.GENERATING,
    ("research", "modify"): WorkflowStatus.RESEARCHING,
    ("research", "reject"): WorkflowStatus.PLANNING,        # the Supervisor closes the request

    ("content", "approve"): WorkflowStatus.PUBLISHING,
    ("content", "regenerate"): WorkflowStatus.GENERATING,
}
WAITING_FOR = {
    "research": WorkflowStatus.WAITING_FOR_RESEARCH_APPROVAL,
    "content": WorkflowStatus.WAITING_FOR_CONTENT_APPROVAL,
}
GENERIC_FAILURE = "The workflow stopped unexpectedly. Details were written to the server log."
APPROVAL_MESSAGES = {
    "research": "Research completed. Review the strategy before content generation.",
    "content": "Content approval required. Review the draft in the editor, then publish it or ask for changes.",
}


class WorkflowBusyError(Exception):
    """The chat already has work in progress."""


class WorkflowEngineUnavailable(Exception):
    """Kept for API compatibility; the engine is connected from Phase 3."""


class NothingToRetry(Exception):
    """The chat isn't in a failed state, so there's nothing to retry."""


class NoPendingApproval(Exception):
    """The chat isn't waiting for that approval."""


class ApprovalPending(Exception):
    """A new request arrived while the chat waits for an approval the user hasn't answered."""

    def __init__(self, stage: str):
        super().__init__(stage)
        self.stage = stage


class WorkflowRunner:
    engine_connected = True

    def __init__(self, db: Database, settings: Settings, llm_factory: Optional[Callable[[], Any]] = None):
        self.db = db
        self.settings = settings
        self.sessions = SessionRepository(db)
        self.company = CompanyRepository(db)
        self.events = EventRepository(db)
        self.llm_factory = llm_factory or (lambda: get_chat_model(settings))

        settings.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        self._checkpoints = sqlite3.connect(str(settings.checkpoint_path), check_same_thread=False)
        self.graph = build_workflow(SqliteSaver(self._checkpoints))

        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._recover_interrupted_runs()

    # -- public API ---------------------------------------------------------

    def is_running(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active

    def submit_message(self, session_id: str, message: str) -> None:
        """
        Record the user's message and act on it:
        - while a draft waits for review, a change request ("Make the introduction more
          concise") is passed to the Generation Agent as regeneration feedback;
        - otherwise it's a new request, which starts a new workflow run.
        """
        session = self._session(session_id)
        status = WorkflowStatus(session["status"])
        if status in ACTIVE_STATUSES:
            raise WorkflowBusyError(session_id)
        if status in WAITING_STATUSES:
            stage = session["approval_required"]
            if stage != "content" or Supervisor.understand_follow_up(message) == "new_request":
                raise ApprovalPending(stage or "")
            if self.is_running(session_id):
                raise WorkflowBusyError(session_id)
            self.events.add(session_id, message_event("user", message))
            self.resume(session_id, "content", "regenerate", feedback=message[:1000])
            return
        self._claim(session_id)
        try:
            self.events.add(session_id, message_event("user", message))
            self.sessions.update(
                session_id, status=WorkflowStatus.REQUESTED.value, current_agent=None, current_node="intake",
                approval_required=None, research_approved=False, content_approved=False,
                blog_status="none", publish_status="none", last_error=None, current_blog_id=None,
                graph_thread_id=f"{session_id}-{uuid.uuid4().hex[:8]}",
            )
        except Exception:
            self._release(session_id)
            raise
        self._start(self._run_new_request, session_id, message)

    def resume(self, session_id: str, stage: str, decision: str,
               feedback: Optional[str] = None, edits: Optional[dict] = None) -> None:
        """Continue a workflow paused at an approval checkpoint."""
        session = self._session(session_id)
        if session["approval_required"] != stage:
            raise NoPendingApproval(stage)
        self._claim(session_id)
        try:
            # The session row and the graph must agree on what's being approved. Otherwise
            # "approve" meant for one checkpoint could be delivered to another.
            pending = self._pending_stage(session)
            if pending != stage:
                self._resync(session_id, pending)
                raise NoPendingApproval(stage)
            self.sessions.update(session_id, approval_required=None,
                                 status=RESUME_STATUS[(stage, decision)].value)
        except Exception:
            self._release(session_id)
            raise
        self._start(self._invoke, session_id, Command(resume={"decision": decision, "feedback": feedback}))

    def retry(self, session_id: str) -> None:
        """Run the failed step again (the steps before it are not repeated)."""
        session = self._session(session_id)
        if session["status"] != WorkflowStatus.FAILED.value:
            raise NothingToRetry(session_id)
        snapshot = self._snapshot(session)
        self._claim(session_id)
        try:
            emitter = self._emitter(session)
            if snapshot is not None and snapshot.next:
                emitter.workflow(WorkflowStatus.REQUESTED.value, f"Retrying from {snapshot.next[0]}")
                values = snapshot.values
                if values.get("current_run_id") and values.get("current_agent") in ("analysis", "generation"):
                    # Re-open the agent's card so it shows as working again.
                    emitter.agent_started(values["current_agent"], values["current_run_id"], "Retrying")
                self.sessions.update(session_id, status=WorkflowStatus.REQUESTED.value, last_error=None)
                target, args = self._invoke, (session_id, None)
            else:
                message = self._last_user_message(session_id)
                if not message:
                    raise NothingToRetry(session_id)
                emitter.workflow(WorkflowStatus.REQUESTED.value, "Retrying the request")
                self.sessions.update(session_id, status=WorkflowStatus.REQUESTED.value, last_error=None)
                target, args = self._run_new_request, (session_id, message)
        except Exception:
            self._release(session_id)
            raise
        self._start(target, *args)

    def state_of(self, session_id: str) -> Optional[dict]:
        """The LangGraph state of the chat's current run (for debugging and tests)."""
        snapshot = self._snapshot(self._session(session_id))
        return dict(snapshot.values) if snapshot else None

    def generation_of(self, session_id: str) -> Optional[dict]:
        """The Generation Agent's progress, for /api/status. None before any writing started."""
        state = self.state_of(session_id) or {}
        if not state.get("generation_status"):
            return None
        return {
            "status": state["generation_status"],
            "content_approval_required": bool(state.get("content_approval_required")),
            "content_approved": bool(state.get("content_approved")),
            "revision_count": state.get("revision_count", 0),
            "target_keyword": state.get("target_keyword"),
            "secondary_keywords": state.get("secondary_keywords", []),
            "blog_title": state.get("blog_title"),
            "blog_id": state.get("blog_id"),
        }

    # -- running ------------------------------------------------------------

    def _start(self, target: Callable, *args) -> None:
        session_id = args[0]

        def run():
            try:
                target(*args)
            except Exception:
                log.exception("Workflow run crashed for session %s", session_id)
                self._fail(session_id, WorkflowEmitter(self.db, session_id), GENERIC_FAILURE, True)
            finally:
                self._release(session_id)

        if self.settings.run_workflow_synchronously:
            run()
        else:
            threading.Thread(target=run, name=f"workflow-{session_id[:8]}", daemon=True).start()

    def _run_new_request(self, session_id: str, message: str) -> None:
        emitter = self._emitter(self._session(session_id))
        emitter.workflow(WorkflowStatus.REQUESTED.value, "Request received")

        context = self.company.get_context()
        company = context["company"]
        if company is None:
            self._fail(session_id, emitter, "Set up your marketing context before starting a chat.", False)
            return
        competitors = [{"name": c["name"], "website": c["website"]} for c in context["competitors"]]
        emitter.workflow(
            WorkflowStatus.REQUESTED.value,
            f"Loaded marketing context for {company['name']}: "
            f"{_count(len(company.get('target_audience', [])), 'audience')}, "
            f"{_count(len(competitors), 'competitor')}",
        )

        initial_state = {
            "session_id": session_id,
            "user_request": message,
            "company_context": company,
            "competitor_context": competitors,
            "research_approved": False,
            "content_approved": False,
            "blog_status": "none",
            "publish_status": "none",
        }
        self._invoke(session_id, initial_state)

    def _invoke(self, session_id: str, payload: Any) -> None:
        """Run the graph until it finishes, pauses for approval, or fails."""
        session = self._session(session_id)
        emitter = self._emitter(session)
        context = WorkflowContext(session_id=session_id, settings=self.settings, db=self.db,
                                  emit=emitter, llm_factory=self.llm_factory)
        config = self._config(session)
        try:
            self.graph.invoke(payload, config, context=context)
        except StepFailed as failure:
            self._fail(session_id, emitter, failure.message, failure.retryable)
            return
        except Exception:
            log.exception("Workflow step failed for session %s", session_id)
            self._fail(session_id, emitter, GENERIC_FAILURE, True)
            return
        self._settle(session_id, config, emitter)

    def _settle(self, session_id: str, config: dict, emitter: WorkflowEmitter) -> None:
        snapshot = self.graph.get_state(config)
        if snapshot.interrupts:
            request = snapshot.interrupts[0].value
            stage = request["stage"]
            values = snapshot.values
            emitter.plan_updated(values.get("plan_run_id", ""),
                                 Supervisor.progress(values.get("plan", []), f"approve_{stage}"))
            emitter.publish(approval_required_event(stage, request.get("summary") or {}, APPROVAL_MESSAGES[stage]))
            self.sessions.update(session_id, status=WAITING_FOR[stage].value, approval_required=stage,
                                 current_agent="supervisor", current_node=f"request_{stage}_approval")
            return

        if snapshot.next:
            log.warning("Session %s stopped with pending nodes %s and no interrupt", session_id, snapshot.next)
        values = snapshot.values
        final = WorkflowStatus.CANCELLED if values.get("cancelled") else WorkflowStatus.COMPLETED
        self.sessions.update(session_id, status=final.value, current_agent=None, current_node=None)
        if values.get("publish_status") == "published":
            emitter.workflow(WorkflowStatus.COMPLETED.value, "Blog published successfully")

    def _fail(self, session_id: str, emitter: WorkflowEmitter, message: str, retryable: bool) -> None:
        try:
            emitter.fail_open_runs("Stopped before finishing")
            emitter.publish(error_event(message, stage="workflow", retryable=retryable))
            self.sessions.update(session_id, status=WorkflowStatus.FAILED.value, current_agent=None,
                                 last_error={"message": message, "retryable": retryable})
        except Exception:
            log.exception("Could not record the failure for session %s", session_id)

    # -- helpers ------------------------------------------------------------

    def _session(self, session_id: str) -> dict:
        session = self.sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def _emitter(self, session: dict) -> WorkflowEmitter:
        """Every event of a run carries its workflow id (the LangGraph thread), so a run can be traced."""
        return WorkflowEmitter(self.db, session["id"], session.get("graph_thread_id"))

    def _pending_stage(self, session: dict) -> Optional[str]:
        """Which approval the graph itself is paused at (None if it isn't paused at one)."""
        snapshot = self._snapshot(session)
        if snapshot is None:
            return None
        if snapshot.interrupts:
            return (snapshot.interrupts[0].value or {}).get("stage")
        # About to enter an approval node (e.g. after a manual state update): same thing.
        return {"request_research_approval": "research",
                "request_content_approval": "content"}.get((snapshot.next or (None,))[0])

    def _resync(self, session_id: str, pending: Optional[str]) -> None:
        """The session row disagreed with the graph: make the row match the graph."""
        session = self._session(session_id)
        if pending in WAITING_FOR:
            log.warning("Session %s said %s approval, graph waits for %s: resynced",
                        session_id, session["approval_required"], pending)
            self.sessions.update(session_id, status=WAITING_FOR[pending].value, approval_required=pending)
        else:
            log.warning("Session %s waited for approval but the graph has no pause", session_id)
            self._fail(session_id, self._emitter(session),
                       "This approval can't continue because the saved workflow state is missing. "
                       "Send your request again to start over.", retryable=False)

    def _config(self, session: dict) -> dict:
        return {"configurable": {"thread_id": session["graph_thread_id"] or session["id"]},
                "recursion_limit": 80}

    def _snapshot(self, session: dict):
        if not session.get("graph_thread_id"):
            return None
        snapshot = self.graph.get_state(self._config(session))
        return snapshot if snapshot.values else None

    def _last_user_message(self, session_id: str) -> Optional[str]:
        messages = [e for e in self.events.list_after(session_id, 0, limit=5000)
                    if e["type"] == "message" and e.get("role") == "user"]
        return messages[-1]["content"] if messages else None

    def _claim(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._active:
                raise WorkflowBusyError(session_id)
            self._active.add(session_id)

    def _release(self, session_id: str) -> None:
        with self._lock:
            self._active.discard(session_id)

    def _recover_interrupted_runs(self) -> None:
        """A run that was mid-step when the server stopped can't still be running: mark it retryable."""
        for session_id in self.sessions.ids_with_status([s.value for s in ACTIVE_STATUSES]):
            log.warning("Session %s was interrupted by a restart", session_id)
            self._fail(session_id, WorkflowEmitter(self.db, session_id),
                       "The server restarted while this step was running. Select Retry to continue.", True)


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" + ("" if n == 1 else "s")
