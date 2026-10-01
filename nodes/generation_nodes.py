"""
Generation nodes: the Generation Agent's writing, one step per node.

First draft, after the research approval:

    prepare_generation -> build_outline -> generate_blog -> optimize_blog
      -> prepare_blog_review -> [content approval: Publish, or ask for changes]

After the human asks for changes, plan_revision decides how far back to go, so
only what the feedback is about gets redone (the research is never repeated):

    plan_revision --"sections"--> revise_blog     only the parts the feedback names
                  --"draft"-----> generate_blog   same outline, new wording
                  --"outline"---> build_outline   new structure, then a new draft
      -> optimize_blog -> prepare_blog_review -> [content approval]

Each node: report node_started, ask the agent to decide or act, report each
tool call, return only the state fields it changed.

Writing needs a language model. Without one, prepare_generation stops with a
retryable error that says how to set it up; after adding the key and restarting,
Retry continues from that step (the approved research is kept). Missing or
unapproved research stops with a non-retryable error instead.
"""

from __future__ import annotations

from typing import Literal

from langgraph.runtime import Runtime

from agents.generation_agent import GenerationAgent, GenerationBrief, GenerationProblem, ResearchNotReady
from graph.context import StepFailed, WorkflowContext, llm_step_failed
from graph.state import ContentCrewState, WorkflowStatus
from services.events import blog_ready_event, new_id
from storage.repositories import BlogRepository
from tools.seo_analysis import plain_text, words_of
from tools.text import split_sections

AGENT = "generation"
WHO = "Generation Agent"
GENERATING = WorkflowStatus.GENERATING.value


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _brief(state: ContentCrewState, feedback: str | None = None) -> GenerationBrief:
    try:
        return GenerationBrief.from_state(state, feedback)
    except ResearchNotReady as missing:
        raise StepFailed(missing.message, retryable=False)


def _agent(ctx: WorkflowContext) -> GenerationAgent:
    return GenerationAgent(ctx.require_llm(WHO), ctx.settings)


def _ask(fn, *args):
    """Run an agent step that uses the model; turn any failure into a plain, retryable message."""
    try:
        return fn(*args)
    except StepFailed:
        raise
    except GenerationProblem as problem:
        raise StepFailed(f"{WHO}: {problem} Select Retry to try again.", retryable=True)
    except Exception as exc:
        raise llm_step_failed(exc, WHO)


def _editor_version(ctx: WorkflowContext, state: ContentCrewState) -> dict:
    """The draft as it is in the editor now, including any edits the user saved."""
    blog = BlogRepository(ctx.db).get(state["blog_id"]) if state.get("blog_id") else None
    if blog and blog["status"] == "draft":
        return {"title": blog["title"], "content": blog["content"]}
    return {"title": state.get("blog_title"), "content": state.get("blog_draft") or ""}


# ---------------------------------------------------------------------------
# First draft
# ---------------------------------------------------------------------------

def prepare_generation(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Check the approved research is there and usable, then hand it to the writer."""
    ctx = runtime.context
    run_id = new_id("run")
    ctx.emit.agent_started(AGENT, run_id, f'Writing a post on "{state.get("topic")}" from the approved research')

    with ctx.emit.node(AGENT, run_id, "prepare_generation", GENERATING, "Reading the approved research") as step:
        brief = _brief(state)
        ctx.require_llm(WHO)                        # fail early (and retryably) if there's no model
        parts = [_plural(len(brief.sources), "competitor source"), f'main keyword "{brief.primary_keyword}"']
        if brief.secondary_keywords:
            parts.append(_plural(len(brief.secondary_keywords), "other keyword"))
        parts.append(_plural(len(brief.gaps), "content gap"))
        message = "Working from the approved research: " + ", ".join(parts)
        step.complete(message + (". " + " ".join(brief.warnings) if brief.warnings else ""))

    return {
        "current_run_id": run_id, "current_agent": AGENT, "current_node": "prepare_generation",
        "generation_status": "outlining", "blog_status": "drafting",
        "target_keyword": brief.primary_keyword, "secondary_keywords": brief.secondary_keywords,
        "extra_research": [], "content_feedback": None,
        "revision_count": 0, "revision_plan": None,
        "content_approved": False, "content_approval_required": False,
    }


def build_outline(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = state["current_run_id"]
    with ctx.emit.node(AGENT, run_id, "build_outline", GENERATING, "Building the outline") as step:
        brief = _brief(state, state.get("content_feedback"))
        agent = _agent(ctx)
        outline = _ask(agent.build_outline, brief)

        # Limited extra research: one search, only if the outline asks for a missing fact,
        # and at most once per post. The Analysis Agent remains the research agent.
        question = outline.pop("research_question", "")
        extra, records = list(state.get("extra_research") or []), []
        if question and agent.can_search and not extra:
            with ctx.emit.tool("google_search", f'Looking up one detail for the post: "{question}"') as call:
                result = agent.look_up(question)
                if result.ok:
                    call.complete(result.message)
                elif result.status == "empty":
                    call.complete("No results")
                else:
                    call.fail(result.message)
            records.append(call.record)
            keep = ("title", "url", "domain", "snippet", "query")
            extra = [{k: r.get(k, "") for k in keep} for r in (result.data or [])[:4]] if result.ok else []

        message = (f'Outline created: {_plural(len(outline["sections"]), "section")} for "{outline["title"]}", '
                   f'main keyword "{outline["target_keyword"]}"')
        if brief.gaps:
            message += f", covers {len(outline['gaps_addressed'])} of {_plural(len(brief.gaps), 'content gap')}"
        if outline["revised_after_review"]:
            message += "; the agent checked its first outline and fixed it"
        if outline["notes"]:
            message += f"; {_plural(len(outline['notes']), 'point')} left for your review"
        step.complete(message)

    return {
        "blog_outline": outline, "blog_title": outline["title"],
        "target_keyword": outline["target_keyword"],
        "secondary_keywords": [k for k in brief.approved_keywords if k != outline["target_keyword"]],
        "extra_research": extra, "generation_status": "writing",
        "tool_events": records, "current_node": "build_outline",
    }


def generate_blog(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    with ctx.emit.node(AGENT, state["current_run_id"], "generate_blog", GENERATING,
                       "Blog generation started: writing from the outline") as step:
        brief = _brief(state, state.get("content_feedback"))
        markdown = _ask(_agent(ctx).write_blog, brief, state["blog_outline"])
        sections = sum(1 for part in split_sections(markdown) if part["heading"])
        words = len(words_of(plain_text(markdown)))
        step.complete(f"Blog draft completed: {words:,} words in {_plural(sections, 'section')}")
    return {"blog_draft": markdown, "blog_title": state.get("blog_title") or state["blog_outline"]["title"],
            "generation_status": "optimizing", "current_node": "generate_blog"}


FIXED_LABELS = {
    "intro_keyword": "Main keyword added to the introduction",
    "heading_keyword": "Main keyword added to a section heading",
    "stuffing": "Keyword repetition reduced",
    "missing_keywords": "Approved keywords added where they fit",
    "unsupported_figures": "Unsupported figures removed",
    "shared_phrases": "Phrases matching competitor pages reworded",
    "too_short": "Thin sections expanded",
    "long_sentences": "Long sentences shortened",
    "long_paragraphs": "Long paragraphs split",
    "topics_missing": "More of the topics competitors share are covered",
}


def _problem_score(report: dict) -> int:
    return (len(report["issues"]) + len(report["keywords_missing"]) + len(report["unsupported_figures"])
            + report["originality"]["shared_phrases"] + report["readability"]["long_paragraphs"])


def _improved(before: dict, after: dict) -> bool:
    """Keep the optimized draft only if it fixed something without losing content or adding problems."""
    return (after["word_count"] >= before["word_count"] * 0.75
            and after["sections"] >= before["sections"]
            and not set(after["issue_codes"]) - set(before["issue_codes"])
            and _problem_score(after) < _problem_score(before))


def _optimization_summary(report: dict, total_keywords: int) -> str:
    pk = report["primary_keyword"]
    places = [name for name, ok in (("title", pk["in_title"]), ("introduction", pk["in_introduction"]),
                                    ("a heading", pk["in_headings"] > 0)) if ok]
    parts = [f"main keyword in the {', '.join(places)}" if places else "main keyword placement needs review",
             f"{len(report['keywords_found'])} of {total_keywords} approved keywords used"]
    ease = report["readability"]["reading_ease"]
    if ease is not None:
        parts.append(f"reading ease {ease:g} ({report['readability']['reading_ease_label']}, approximate)")
    if report["optimization"]["applied"]:
        parts.append(f"fixed {_plural(len(report['optimization']['fixed']), 'issue')}")
    left = len(report["issues"])
    parts.append(f"{_plural(left, 'point')} left for your review" if left else "nothing left to fix")
    return "Optimization completed: " + ", ".join(parts)


def optimize_blog(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Measure the draft, fix only what was measured as a problem, and keep the fix only if it's better."""
    ctx = runtime.context
    plan = state.get("revision_plan") or {}
    pages = state.get("scraped_content", [])
    title, markdown = state["blog_title"], state["blog_draft"]

    with ctx.emit.node(AGENT, state["current_run_id"], "optimize_blog", GENERATING,
                       "Optimizing for search and readability") as step:
        brief = _brief(state, state.get("content_feedback"))
        records = []
        with ctx.emit.tool("seo_analysis", "Measuring keywords, headings and readability") as call:
            before = GenerationAgent.measure(brief, title, markdown, pages)
            call.complete(before.message)
        records.append(call.record)
        report = before.data
        optimization = {"applied": False, "fixed": [], "issues_before": len(report["issues"]), "note": ""}

        if plan.get("scope") == "sections":
            # A targeted revision promised to leave the other sections alone, so only measure.
            optimization["note"] = ("Measured only: this revision changed specific sections, so the rest of "
                                    "the draft was left exactly as it was.")
        elif report["fixable"]:
            revised = _ask(_agent(ctx).optimize, brief, markdown, report["fixable"])
            with ctx.emit.tool("seo_analysis", "Checking the optimized draft") as call:
                after = GenerationAgent.measure(brief, title, revised, pages)
                call.complete(after.message)
            records.append(call.record)
            if after.ok and _improved(before.data, after.data):
                fixed = [FIXED_LABELS.get(code, code) for code in before.data["issue_codes"]
                         if code not in after.data["issue_codes"]]
                markdown, report = revised, after.data
                optimization.update(applied=True, fixed=fixed,
                                    note="Kept the optimized version: it fixed problems without losing content.")
            else:
                optimization["note"] = "The optimized version was discarded: it lost content or didn't fix anything."
        else:
            optimization["note"] = "No changes needed."
        optimization["issues_after"] = len(report["issues"])
        report = {**report, "optimization": optimization}
        step.complete(_optimization_summary(report, len(brief.approved_keywords)))

    return {"blog_draft": markdown, "seo_report": report, "generation_status": "awaiting_review",
            "tool_events": records, "current_node": "optimize_blog"}


def prepare_blog_review(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Save the draft so the editor can open it, then hand over to the human."""
    ctx = runtime.context
    run_id = state["current_run_id"]
    with ctx.emit.node(AGENT, run_id, "prepare_blog_review", GENERATING, "Saving the draft for review") as step:
        blog = BlogRepository(ctx.db).create_draft(ctx.session_id, state["blog_title"], state["blog_draft"])
        ctx.emit.update_session(blog_status="ready")
        ctx.emit.publish(blog_ready_event(blog["id"], blog["title"]))
        number = state.get("revision_count", 0)
        step.complete("Draft saved and opened in the editor" + (f" (revision {number})" if number else ""))
    ctx.emit.agent_completed(AGENT, run_id, "Draft ready for your review")
    return {"blog_id": blog["id"], "blog_status": "ready", "generation_status": "awaiting_review",
            "content_approval_required": True, "content_approved": False, "current_node": "prepare_blog_review"}


# ---------------------------------------------------------------------------
# Regeneration
# ---------------------------------------------------------------------------

def plan_revision(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """After "Regenerate" or feedback: decide which generation step to go back to."""
    ctx = runtime.context
    run_id = new_id("run")
    feedback = state.get("content_feedback")
    number = state.get("revision_count", 0) + 1
    ctx.emit.agent_started(AGENT, run_id, "Revising the draft based on your feedback" if feedback
                           else "Writing a new version of the draft")

    with ctx.emit.node(AGENT, run_id, "plan_revision", GENERATING, "Deciding what to change") as step:
        _brief(state, feedback)                     # the approved research must still be there
        agent = _agent(ctx)
        current = _editor_version(ctx, state)
        plan = _ask(agent.plan_revision, feedback, current["content"])
        record = {"number": number, "feedback": feedback, **plan}
        step.complete(f"Revision {number}: {plan['reason']}")

    return {
        "current_run_id": run_id, "current_agent": AGENT, "current_node": "plan_revision",
        "revision_count": number, "revision_plan": record, "revision_history": [record],
        "generation_status": "revising", "blog_status": "drafting",
        # Start from the editor's version, so the user's own edits are kept where nothing changes.
        "blog_draft": current["content"], "blog_title": current["title"],
        "content_approved": False, "content_approval_required": False,
    }


def after_revision_plan(state: ContentCrewState) -> Literal["sections", "draft", "outline"]:
    """The agent's revision decision, as a conditional edge."""
    return (state.get("revision_plan") or {}).get("scope", "draft")


def revise_blog(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    """Rewrite only the sections the feedback is about; everything else stays exactly as it was."""
    ctx = runtime.context
    plan = state["revision_plan"]
    names = ", ".join(f'"{name}"' for name in plan["sections"])
    with ctx.emit.node(AGENT, state["current_run_id"], "revise_blog", GENERATING, f"Revising {names}") as step:
        brief = _brief(state, plan["feedback"])
        markdown = _ask(_agent(ctx).revise_sections, brief, state["blog_draft"], plan["section_ids"],
                        plan["feedback"] or "")
        step.complete(f"Revised {names}; the rest of the draft is unchanged")
    return {"blog_draft": markdown, "generation_status": "optimizing", "current_node": "revise_blog"}
