"""
Human-in-the-loop decisions.

POST /api/approval/research   {session_id, decision: "approve" | "modify" | "reject", feedback?}
                              reject = "Cancel request": the run ends, nothing is written.
POST /api/approval/content    {session_id, decision: "approve" | "regenerate", feedback?}
                              approve = Publish. regenerate = ask the Generation Agent for changes;
                              feedback like "make the introduction more concise" lets it redo
                              only that part (sending the same text in the chat does the same).

The server only accepts a decision when the workflow is actually paused at that
checkpoint. The approval is recorded on the server; publishing checks the
server-side flag, never anything the browser claims.
"""

from __future__ import annotations

from flask import Blueprint, jsonify

from api import deps
from api.errors import ApiError
from api.validation import Validator, json_body
from services.workflow_runner import NoPendingApproval, WorkflowBusyError, WorkflowEngineUnavailable

bp = Blueprint("approval", __name__)

DECISIONS = {
    "research": ("approve", "modify", "reject"),     # reject = "Cancel request"
    "content": ("approve", "regenerate"),
}
NEEDS_FEEDBACK = {"modify"}


def _decide(stage: str):
    data = json_body()
    v = Validator()
    session_id = v.identifier(data.get("session_id"), "session_id", "the session id")
    decision = v.choice(data.get("decision"), "decision", DECISIONS[stage])
    feedback = v.text(data.get("feedback"), "feedback", "what should change",
                      required=decision in NEEDS_FEEDBACK, max_len=1000, multiline=True)
    v.raise_if_errors()

    session = deps.session_or_404(session_id)
    if session["approval_required"] != stage:
        raise ApiError(409, "no_pending_approval", f"There's no {stage} approval waiting in this chat.")

    try:
        deps.runner().resume(session_id, stage, decision, feedback=feedback)
    except NoPendingApproval:
        raise ApiError(409, "no_pending_approval", f"There's no {stage} approval waiting in this chat.")
    except WorkflowEngineUnavailable:
        raise ApiError(503, "workflow_unavailable", "The agent workflow isn't connected yet.")
    except WorkflowBusyError:
        raise ApiError(409, "workflow_busy", "The agents are still working on this chat.", retryable=True)

    return jsonify({"session": deps.session_repo().get(session_id)}), 202


@bp.post("/approval/research")
def approve_research():
    return _decide("research")


@bp.post("/approval/content")
def approve_content():
    return _decide("content")
