"""
The LangGraph workflow: which node runs after which.

    START
      -> create_plan -> route_task --(Supervisor decides)--> ...

    route_task --analysis-->   prepare_research -> discover_competitors
                               -> search_competitor_content -> scrape_content
                                     enough sources -> analyze_competitors
                                     too few        -> search_competitor_content (once)
                               -> analyze_keywords
                               -> identify_content_gaps -> create_research_summary
                               -> request_research_approval   [PAUSE for the human]
                                     approve -> route_task
                                     modify  -> prepare_research
                                     reject  -> route_task -> END   ("Cancel request")

    route_task --generation--> prepare_generation -> build_outline -> generate_blog
                               -> optimize_blog -> prepare_blog_review
                               -> request_content_approval    [PAUSE for the human]
                                     approve (Publish) -> route_task
                                     regenerate        -> plan_revision

    plan_revision --(Generation Agent decides how far back to go)-->
                     sections -> revise_blog   -> optimize_blog
                     draft    -> generate_blog -> optimize_blog
                     outline  -> build_outline -> generate_blog -> optimize_blog

    route_task --publish-->    publish_blog -> END
    route_task --end-->        END

All work returns to the Supervisor (`route_task`) between agents, so the
Supervisor is always the one handing work from one agent to the next. A
revision stays inside the Generation Agent: the approved research is kept and
never rerun.

Run `python -m graph.workflow` to print this graph as a Mermaid diagram
generated from the code itself.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from graph.context import WorkflowContext
from graph.state import ContentCrewState
from nodes import analysis_nodes, approval_nodes, generation_nodes, publish_nodes, supervisor_nodes

ANALYSIS_STEPS = [
    ("prepare_research", analysis_nodes.prepare_research),
    ("discover_competitors", analysis_nodes.discover_competitors),
    ("search_competitor_content", analysis_nodes.search_competitor_content),
    ("scrape_content", analysis_nodes.scrape_content),
    ("analyze_competitors", analysis_nodes.analyze_competitors),
    ("analyze_keywords", analysis_nodes.analyze_keywords),
    ("identify_content_gaps", analysis_nodes.identify_content_gaps),
    ("create_research_summary", analysis_nodes.create_research_summary),
]

GENERATION_STEPS = [
    ("prepare_generation", generation_nodes.prepare_generation),
    ("build_outline", generation_nodes.build_outline),
    ("generate_blog", generation_nodes.generate_blog),
    ("optimize_blog", generation_nodes.optimize_blog),
    ("prepare_blog_review", generation_nodes.prepare_blog_review),
]


def _chain(builder: StateGraph, steps: list) -> None:
    for name, fn in steps:
        builder.add_node(name, fn)
    for (first, _), (second, _) in zip(steps, steps[1:]):
        builder.add_edge(first, second)


def route_after_supervisor(state: ContentCrewState) -> str:
    return state.get("next_step", "end")


def build_workflow(checkpointer=None):
    builder = StateGraph(ContentCrewState, context_schema=WorkflowContext)

    # Supervisor
    builder.add_node("create_plan", supervisor_nodes.create_plan)
    builder.add_node("route_task", supervisor_nodes.route_task)
    builder.add_edge(START, "create_plan")
    builder.add_edge("create_plan", "route_task")
    builder.add_conditional_edges("route_task", route_after_supervisor, {
        "analysis": "prepare_research",
        "generation": "prepare_generation",
        "publish": "publish_blog",
        "end": END,
    })

    # Analysis Agent, then the research approval checkpoint.
    # After scraping, the agent decides whether it has enough sources; if not it
    # searches again (at most one extra round, see AnalysisAgent.assess_coverage).
    _chain(builder, ANALYSIS_STEPS[:4])                      # prepare -> discover -> search -> scrape
    _chain(builder, ANALYSIS_STEPS[4:])                      # analyze competitors -> ... -> summary
    builder.add_conditional_edges("scrape_content", analysis_nodes.after_scraping, {
        "enough": "analyze_competitors",
        "search_again": "search_competitor_content",
    })
    builder.add_node("request_research_approval", approval_nodes.request_research_approval)
    builder.add_edge("create_research_summary", "request_research_approval")
    builder.add_conditional_edges("request_research_approval", approval_nodes.after_research_approval, {
        "approved": "route_task",
        "modify": "prepare_research",
        "rejected": "route_task",              # the Supervisor sees the cancellation and ends the run
    })

    # Generation Agent, then the content approval checkpoint
    _chain(builder, GENERATION_STEPS)
    builder.add_node("request_content_approval", approval_nodes.request_content_approval)
    builder.add_edge("prepare_blog_review", "request_content_approval")
    builder.add_conditional_edges("request_content_approval", approval_nodes.after_content_approval, {
        "approved": "route_task",
        "regenerate": "plan_revision",
    })

    # Regeneration: the agent picks the step to return to, so nothing is redone without reason.
    builder.add_node("plan_revision", generation_nodes.plan_revision)
    builder.add_node("revise_blog", generation_nodes.revise_blog)
    builder.add_conditional_edges("plan_revision", generation_nodes.after_revision_plan, {
        "sections": "revise_blog",
        "draft": "generate_blog",
        "outline": "build_outline",
    })
    builder.add_edge("revise_blog", "optimize_blog")

    # Publishing
    builder.add_node("publish_blog", publish_nodes.publish_blog)
    builder.add_edge("publish_blog", END)

    return builder.compile(checkpointer=checkpointer)


if __name__ == "__main__":
    print(build_workflow().get_graph().draw_mermaid())
