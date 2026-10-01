"""
Workflow status and app health.

GET /api/status?session_id=...   where a workflow is: agent, node, approval, generation, blog, publishing
GET /api/health                  is the app working, and which integrations are configured
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from api import deps
from api.errors import ValidationError

log = logging.getLogger(__name__)
bp = Blueprint("status", __name__)


@bp.get("/status")
def status():
    session_id = (request.args.get("session_id") or "").strip()
    if not session_id:
        raise ValidationError({"session_id": "Pass session_id as a query parameter."})
    session = deps.session_or_404(session_id)

    events = deps.event_repo().list_after(session_id, 0, limit=2000)
    tool_activity = [e for e in events if e["type"] in ("tool_completed", "tool")][-5:]

    return jsonify({
        "session_id": session_id,
        "workflow_status": session["status"],
        "current_agent": session["current_agent"],
        "current_node": session["current_node"],
        "approval_required": session["approval_required"],
        "blog_status": session["blog_status"],
        "publish_status": session["publish_status"],
        "generation": deps.runner().generation_of(session_id),   # outline/draft/revision progress, or null
        "running": deps.runner().is_running(session_id),
        "tool_activity": tool_activity,
        "last_event_id": events[-1]["id"] if events else 0,
        "last_error": session["last_error"],
    })


@bp.get("/health")
def health():
    database = "ok"
    try:
        with deps.db().connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception:
        log.exception("Database health check failed")
        database = "error"

    return jsonify({
        "status": "ok" if database == "ok" else "degraded",
        "database": database,
        "integrations": deps.settings().integrations,
        "workflow_engine": "connected" if deps.runner().engine_connected else "not_connected",
    })
