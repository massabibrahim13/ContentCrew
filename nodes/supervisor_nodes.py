"""
Supervisor nodes: create_plan and route_task.

A node is one step in the graph. It reads the state, does its part (here:
asking the Supervisor for a decision), reports what happened, and returns only
the fields it changed. LangGraph merges that into the shared state.
"""

from __future__ import annotations

from langgraph.runtime import Runtime

from graph.context import WorkflowContext
from graph.state import ContentCrewState, WorkflowStatus
from services.events import new_id
from supervisor.supervisor_agent import CANCELLED_REPLY, ROUTE_MESSAGES, UNSUPPORTED_REPLY, Supervisor

AGENT = "supervisor"
ROUTE_STATUS = {
    "analysis": WorkflowStatus.RESEARCHING.value,
    "generation": WorkflowStatus.GENERATING.value,
    "publish": WorkflowStatus.PUBLISHING.value,
}


def create_plan(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Understand the request and lay out the steps."""
    emit = runtime.context.emit
    run_id = new_id("run")
    emit.agent_started(AGENT, run_id)

    with emit.node(AGENT, run_id, "create_plan", WorkflowStatus.PLANNING.value, "Reading the request") as step:
        request_type, topic = Supervisor.understand_request(state["user_request"])
        base = {"request_type": request_type, "topic": topic, "plan_run_id": run_id,
                "current_agent": AGENT, "current_node": "create_plan"}
        if request_type == "unsupported":
            step.complete("Not a writing request, so no research or writing is needed")
            emit.agent_completed(AGENT, run_id)
            emit.message(AGENT, UNSUPPORTED_REPLY)
            return {**base, "plan": []}

        company = state.get("company_context") or {}
        plan = Supervisor.progress(Supervisor.create_plan(), "research")
        emit.plan_updated(run_id, plan)
        step.complete(f'Planned a blog post on "{topic}" for {company.get("name", "your company")}')
    emit.agent_completed(AGENT, run_id)
    return {**base, "plan": plan}


def route_task(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Decide which agent works next. The conditional edge after this node reads `next_step`."""
    emit = runtime.context.emit
    next_step = Supervisor.decide_next(state)
    update = {"next_step": next_step, "current_agent": AGENT, "current_node": "route_task"}
    if next_step == "end":
        if state.get("cancelled"):
            plan = Supervisor.cancel(state.get("plan", []))
            emit.plan_updated(state.get("plan_run_id", ""), plan)
            emit.message(AGENT, CANCELLED_REPLY)
            update["plan"] = plan
        return update

    run_id = new_id("run")
    emit.agent_started(AGENT, run_id)
    with emit.node(AGENT, run_id, "route_task", ROUTE_STATUS[next_step], "Deciding the next step") as step:
        plan = Supervisor.progress(state.get("plan", []), Supervisor.plan_step_for(next_step))
        emit.plan_updated(state.get("plan_run_id", ""), plan)
        step.complete(ROUTE_MESSAGES[next_step])
    emit.agent_completed(AGENT, run_id)
    return {**update, "plan": plan}
