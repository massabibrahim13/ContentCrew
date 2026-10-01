"""
Chat and the live event stream.

POST /api/chat                        send a message; starts a workflow, or (while a draft
                                      waits for review) asks the Generation Agent for changes
GET  /api/sessions/<id>               session state + every event so far (for page reloads)
GET  /api/sessions/<id>/events        Server-Sent Events stream of new events
POST /api/sessions/<id>/retry         run the failed step again

Why Server-Sent Events? The browser only needs to *receive* updates, SSE works
over plain HTTP with Flask's built-in server, and the browser reconnects on its
own (resuming from the last event id). No WebSocket server or message queue needed.
"""

from __future__ import annotations

import json
import time

from flask import Blueprint, Response, jsonify, request, stream_with_context

from api import deps
from api.errors import ApiError
from api.validation import Validator, json_body
from graph.state import WorkflowStatus
from services.workflow_runner import ApprovalPending, NothingToRetry, WorkflowBusyError

bp = Blueprint("chat", __name__)

APPROVAL_PENDING = {
    "research": "This chat is waiting for your approval of the research. Approve it or choose Modify first.",
    "content": "This chat has a draft waiting for review. Publish it, ask for changes, or start a new chat "
               "for a different post.",
    "": "This chat is waiting for your approval. Respond to it before sending a new message.",
}

STREAM_POLL_SECONDS = 0.5      # how often the stream checks for new events
STREAM_MAX_SECONDS = 55        # then the browser reconnects automatically
STREAM_HEARTBEAT_SECONDS = 15  # keeps proxies from closing an idle stream


def _title_from(message: str) -> str:
    first_line = message.strip().splitlines()[0]
    return first_line if len(first_line) <= 70 else first_line[:67].rstrip() + "..."


@bp.post("/chat")
def chat():
    data = json_body()
    v = Validator()
    message = v.text(data.get("message"), "message", "a message", required=True,
                     min_len=2, max_len=2000, multiline=True)
    session_id = data.get("session_id")
    if session_id is not None:
        v.identifier(session_id, "session_id", "the session id")
    v.raise_if_errors()

    if not deps.company_repo().is_onboarded():
        raise ApiError(409, "onboarding_required",
                       "Set up your marketing context before starting a chat.")

    sessions = deps.session_repo()
    if session_id:
        session = deps.session_or_404(session_id)
    else:
        session = sessions.create(title=_title_from(message), status=WorkflowStatus.REQUESTED.value)

    # While a draft waits for review, the runner treats a message like "Make the
    # introduction more concise" as feedback for the Generation Agent.
    try:
        deps.runner().submit_message(session["id"], message)
    except ApprovalPending as pending:
        raise ApiError(409, "approval_pending", APPROVAL_PENDING.get(pending.stage, APPROVAL_PENDING[""]))
    except WorkflowBusyError:
        raise ApiError(409, "workflow_busy",
                       "The agents are still working on this chat. Wait for them to finish.",
                       retryable=True)

    return jsonify({"session": sessions.get(session["id"])}), 202


@bp.get("/sessions/<session_id>")
def get_session(session_id: str):
    session = deps.session_or_404(session_id)
    events = deps.event_repo().list_after(session_id, 0, limit=2000)
    return jsonify({"session": session, "events": events})


@bp.post("/sessions/<session_id>/retry")
def retry_session(session_id: str):
    deps.session_or_404(session_id)
    try:
        deps.runner().retry(session_id)
    except NothingToRetry:
        raise ApiError(409, "nothing_to_retry", "This chat hasn't failed, so there's nothing to retry.")
    except WorkflowBusyError:
        raise ApiError(409, "workflow_busy", "The agents are already working on this chat.", retryable=True)
    return jsonify({"session": deps.session_repo().get(session_id)}), 202


@bp.get("/sessions/<session_id>/events")
def stream_events(session_id: str):
    deps.session_or_404(session_id)
    events = deps.event_repo()

    # On reconnect the browser sends Last-Event-ID; first connection uses ?after=.
    raw_after = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
    try:
        after = max(0, int(raw_after))
    except ValueError:
        after = 0
    once = request.args.get("once") == "1"  # return immediately after flushing (tests, debugging)

    def generate():
        last_id = after
        started = last_beat = time.monotonic()
        yield "retry: 2000\n\n"
        while True:
            batch = events.list_after(session_id, last_id)
            for event in batch:
                last_id = event["id"]
                yield f"id: {last_id}\ndata: {json.dumps(event)}\n\n"
            now = time.monotonic()
            if once or now - started > STREAM_MAX_SECONDS:
                return
            if not batch and now - last_beat > STREAM_HEARTBEAT_SECONDS:
                last_beat = now
                yield ": heartbeat\n\n"
            time.sleep(STREAM_POLL_SECONDS)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
