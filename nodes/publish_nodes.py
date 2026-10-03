"""
Publish node: the workflow's one external action.

Only reachable through the Supervisor after the content approval. The Publish
Blog tool sends POST /blog over HTTP, and the blog API checks the approval, the
workflow state and the draft again before acting. The published version is the
current draft as it is in the editor, including any edits the user made.

If the API can't be reached, the step fails with Retry; if it refuses, the
step fails without Retry, because trying again wouldn't change the answer.
"""

from __future__ import annotations

from langgraph.runtime import Runtime

from graph.context import StepFailed, WorkflowContext
from graph.state import ContentCrewState, WorkflowStatus
from services.events import blog_published_event, new_id
from storage.repositories import BlogRepository
from supervisor.supervisor_agent import Supervisor
from tools import publish_blog as publish_tool

AGENT = "supervisor"


def publish_blog(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = new_id("run")
    ctx.emit.agent_started(AGENT, run_id, "Publishing the approved draft")

    with ctx.emit.node(AGENT, run_id, "publish_blog", WorkflowStatus.PUBLISHING.value, "Publishing") as step:
        with ctx.emit.tool("publish_blog", f"POST {publish_tool.endpoint(ctx.settings)}") as call:
            result = publish_tool.publish(ctx.settings, ctx.session_id, state["blog_id"])
            if result.ok:
                call.complete(result.message)
            else:
                call.fail(result.message)
        if not result.ok:
            retryable = bool((result.data or {}).get("retryable"))
            raise StepFailed(f"Publishing failed. {result.message}" + (" Select Retry to try again." if retryable else ""),
                             retryable=retryable)
        # A retry whose first attempt already went through gets no post back; read it (read-only).
        blog = result.data if result.data.get("slug") else BlogRepository(ctx.db).get(state["blog_id"])
        ctx.emit.update_session(publish_status="published", blog_status="published")
        ctx.emit.publish(blog_published_event(blog["id"], blog["title"], blog["slug"], run_id=run_id))
        step.complete(f"Published at /blog/{blog['slug']}")

    ctx.emit.agent_completed(AGENT, run_id, "Published")
    plan = Supervisor.progress(state.get("plan", []), None)
    ctx.emit.plan_updated(state.get("plan_run_id", ""), plan)
    return {"publish_status": "published", "published_slug": blog["slug"], "blog_status": "published",
            "generation_status": "published", "plan": plan, "tool_events": [call.record],
            "current_node": "publish_blog"}
