"""
Approval nodes: where the workflow stops for a human.

`interrupt()` is LangGraph's built-in pause. It saves a checkpoint and returns
control to the app, which shows the approval card. Nothing else runs until the
user decides; the server can even restart in between. When the user answers,
the app resumes the graph with `Command(resume={"decision": ..., "feedback": ...})`
and `interrupt()` returns that value right here.

Note: on resume LangGraph runs the node again from the top, so nothing before
`interrupt()` may have side effects (no events, no database writes).

The conditional edges after these nodes (graph/workflow.py) use
`after_research_approval` and `after_content_approval` to pick the next node.
"""

from __future__ import annotations

from typing import Literal

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from graph.context import StepFailed, WorkflowContext
from graph.state import ContentCrewState
from services.events import approval_resolved_event
from supervisor.supervisor_agent import Supervisor


def _read_decision(value, allowed: set[str]) -> tuple[str, str | None]:
    if not isinstance(value, dict) or value.get("decision") not in allowed:
        raise StepFailed("The approval decision couldn't be read. Try again.", retryable=False)
    feedback = value.get("feedback")
    return value["decision"], (feedback.strip() if isinstance(feedback, str) and feedback.strip() else None)


def _refresh_plan(state: ContentCrewState, ctx: WorkflowContext, active: str) -> list:
    plan = Supervisor.progress(state.get("plan", []), active)
    ctx.emit.plan_updated(state.get("plan_run_id", ""), plan)
    return plan


# ---------------------------------------------------------------------------
# Approval 1: research and content strategy
# ---------------------------------------------------------------------------

def request_research_approval(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    value = interrupt({"stage": "research", "summary": state.get("research_summary") or {}})

    ctx = runtime.context
    decision, feedback = _read_decision(value, {"approve", "modify"})
    approved = decision == "approve"
    ctx.emit.publish(approval_resolved_event("research", decision, feedback))
    ctx.emit.update_session(research_approved=approved, approval_required=None)

    update = {"research_approved": approved, "research_feedback": None if approved else feedback,
              "current_agent": "supervisor", "current_node": "request_research_approval"}
    if not approved:
        update["plan"] = _refresh_plan(state, ctx, "research")
    return update


def after_research_approval(state: ContentCrewState) -> Literal["approved", "modify"]:
    return "approved" if state.get("research_approved") else "modify"


# ---------------------------------------------------------------------------
# Approval 2: the generated draft
# ---------------------------------------------------------------------------

REVISION_WORDS = {
    "sections": lambda plan: "changed only " + ", ".join(f'"{s}"' for s in plan["sections"])
                             + "; the rest is as it was",
    "draft": lambda plan: "rewrote the draft from the same approved outline",
    "outline": lambda plan: "rebuilt the outline, then wrote a new draft",
}


def draft_review_card(state: ContentCrewState) -> dict:
    """What the content approval card shows: measured facts about the draft, and what to check."""
    report = state.get("seo_report") or {}
    outline = state.get("blog_outline") or {}
    pk = report.get("primary_keyword") or {}
    readability = report.get("readability") or {}
    plan = state.get("revision_plan")
    sections = []

    if plan:
        asked = f' You asked: "{plan["feedback"]}"' if plan.get("feedback") else ""
        sections.append({"title": "This revision",
                         "items": [f"Revision {plan['number']}: {REVISION_WORDS[plan['scope']](plan)}.{asked}"]})
    if outline.get("sections"):
        sections.append({"title": "Outline", "items": [s["heading"] for s in outline["sections"]]})
    if pk:
        places = [name for name, ok in (("title", pk["in_title"]), ("introduction", pk["in_introduction"]),
                                        (f"{pk['in_headings']} heading{'s' if pk['in_headings'] != 1 else ''}",
                                         pk["in_headings"] > 0)) if ok]
        items = [f'Main keyword "{pk["term"]}": used {pk["count"]} times ({pk["per_100_words"]} per 100 words)'
                 + (f", in the {', '.join(places)}" if places else "")]
        used = [k["term"] for k in report.get("secondary_keywords", []) if k["found"]]
        unused = [k["term"] for k in report.get("secondary_keywords", []) if not k["found"]]
        if used:
            items.append(f"Other keywords used: {', '.join(used)}")
        if unused:
            items.append(f"Not used: {', '.join(unused)}")
        sections.append({"title": "Keywords", "items": items,
                         "note": "Counted in the draft. No search-volume or ranking data is used."})
    if readability:
        items = [f"Sentences average {readability['average_sentence_words']} words"]
        if readability.get("reading_ease") is not None:
            items.append(f"Reading ease {readability['reading_ease']:g} ({readability['reading_ease_label']})")
        if readability.get("long_paragraphs"):
            items.append(f"{readability['long_paragraphs']} long paragraph(s)")
        sections.append({"title": "Readability", "items": items, "note": readability.get("note", "")})
    gaps = (state.get("research_output") or {}).get("content_gaps") or []
    if gaps:
        covered = outline.get("gaps_addressed", [])
        sections.append({"title": "Content gaps covered", "items": covered,
                         "note": f"{len(covered)} of {len(gaps)} approved gaps are planned in the outline."})
    checks = list(dict.fromkeys(report.get("issues", []) + outline.get("notes", [])))
    sections.append({"title": "For you to check",
                     "items": checks or ["Nothing flagged: no unsupported figures and no phrases copied from "
                                         "competitor pages."]})
    optimization = report.get("optimization") or {}
    if optimization:
        items = list(optimization.get("fixed", [])) or [optimization.get("note", "")]
        sections.append({"title": "Optimization", "items": [i for i in items if i]})

    return {
        "headline": f'Draft ready: "{state.get("blog_title")}", {report.get("word_count", 0):,} words, '
                    f'{report.get("sections", 0)} sections.',
        "sections": sections,
    }


def request_content_approval(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """
    Pause with the draft open in the editor. The human chooses:
        approve     ("Publish")  -> the Supervisor publishes it
        regenerate  (with optional feedback, e.g. "make the introduction more concise")
                                 -> plan_revision decides which generation step to go back to
    Publishing never happens without this explicit decision.
    """
    value = interrupt({"stage": "content", "summary": draft_review_card(state), "blog_id": state.get("blog_id")})

    ctx = runtime.context
    decision, feedback = _read_decision(value, {"approve", "regenerate"})
    approved = decision == "approve"
    ctx.emit.publish(approval_resolved_event("content", decision, feedback))
    # The server-side flag that publishing checks. Only this node, after a real
    # decision from the approval endpoint, ever sets it.
    ctx.emit.update_session(content_approved=approved, approval_required=None,
                            blog_status="ready" if approved else "drafting")

    update = {"content_approved": approved, "content_feedback": None if approved else feedback,
              "content_approval_required": False,
              "generation_status": "approved" if approved else "revising",
              "current_agent": "supervisor", "current_node": "request_content_approval"}
    if not approved:
        update["plan"] = _refresh_plan(state, ctx, "write")
    return update


def after_content_approval(state: ContentCrewState) -> Literal["approved", "regenerate"]:
    return "approved" if state.get("content_approved") else "regenerate"
