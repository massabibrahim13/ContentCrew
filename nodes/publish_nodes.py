"""
Publish node: the workflow's one external action.

Only reachable through the Supervisor after the content approval, and the
Publish Blog tool checks that approval again on the server before acting.
The published version is the draft as it is in the editor now, including any
edits the user made.
"""

from __future__ import annotations

from langgraph.runtime import Runtime

from graph.context import StepFailed, WorkflowContext
from graph.state import ContentCrewState, WorkflowStatus
from services.events import blog_published_event, new_id
from supervisor.supervisor_agent import Supervisor
from tools import publish_blog as publish_tool

AGENT = "supervisor"


def publish_blog(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = new_id("run")
    ctx.emit.agent_started(AGENT, run_id, "Publishing the approved draft")

    with ctx.emit.node(AGENT, run_id, "publish_blog", WorkflowStatus.PUBLISHING.value, "Publishing") as step:
        with ctx.emit.tool("publish_blog", "Sending the post to the blog API") as call:
            result = publish_tool.publish(ctx.db, ctx.session_id, state["blog_id"])
            if result.ok:
                call.complete(result.message)
            else:
                call.fail(result.message)
        if not result.ok:
            raise StepFailed(f"Publishing was refused: {result.message}", retryable=False)
        blog = result.data
        ctx.emit.update_session(publish_status="published", blog_status="published")
        ctx.emit.publish(blog_published_event(blog["id"], blog["title"], blog["slug"]))
        step.complete(f"Published at /blog/{blog['slug']}")

    ctx.emit.agent_completed(AGENT, run_id, "Published")
    plan = Supervisor.progress(state.get("plan", []), None)
    ctx.emit.plan_updated(state.get("plan_run_id", ""), plan)
    return {"publish_status": "published", "published_slug": blog["slug"], "blog_status": "published",
            "generation_status": "published", "plan": plan, "tool_events": [call.record],
            "current_node": "publish_blog"}
