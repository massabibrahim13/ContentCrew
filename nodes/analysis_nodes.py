"""
Analysis nodes: the Analysis Agent's research, one step per node.

    prepare_research -> discover_competitors -> search_competitor_content
      -> scrape_content --(enough sources?)--> analyze_competitors
                        --(too few: search again, at most once)--> search_competitor_content
      -> analyze_keywords -> identify_content_gaps -> create_research_summary
      -> [research approval]

Each node: report node_started, ask the agent to decide or act, report each tool
call, return only the state fields it changed. Tool problems (search not set up,
a page that won't load) are recorded and the research continues with what it
has. A language model that is configured but fails stops the step with a
retryable error, so a bad key is noticed early.
"""

from __future__ import annotations

from typing import Literal

from langgraph.runtime import Runtime

from agents.analysis_agent import (
    MAX_PAGES, AnalysisAgent, default_queries, domain_of, fallback_structure,
)
from graph.context import WorkflowContext, llm_step_failed
from graph.state import ContentCrewState, WorkflowStatus
from services.events import new_id
from storage.repositories import ResearchRepository

AGENT = "analysis"
RESEARCHING = WorkflowStatus.RESEARCHING.value
ANALYZING = WorkflowStatus.ANALYZING.value


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def _agent(ctx: WorkflowContext) -> AnalysisAgent:
    return AnalysisAgent(ctx.settings, ctx.optional_llm())


def _ask_model(fn, *args, **kwargs):
    """Run an agent decision that uses the model; a failing model stops the step (retryable)."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        raise llm_step_failed(exc, "Analysis Agent")


# ---------------------------------------------------------------------------

def prepare_research(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = new_id("run")
    topic, feedback = state["topic"], state.get("research_feedback")
    ctx.emit.agent_started(AGENT, run_id, f'Revising the research on "{topic}" based on your feedback'
                           if feedback else f'Researching "{topic}"')

    with ctx.emit.node(AGENT, run_id, "prepare_research", RESEARCHING, "Planning the research") as step:
        agent = _agent(ctx)
        company = state.get("company_context") or {}
        competitors = state.get("competitor_context") or []
        if agent.uses_model:
            queries = _ask_model(agent.plan_queries, topic, company, competitors, feedback)
            how = "with the language model"
        else:
            queries = default_queries(topic, company, competitors, feedback)
            how = "from the topic (no language model is set up)"
        step.complete(f"Planned {_plural(len(queries), 'search query', 'search queries')} {how}")

    # A fresh pass: clear the previous round's findings (matters after "Modify").
    return {
        "current_run_id": run_id, "current_agent": AGENT, "current_node": "prepare_research",
        "research_queries": queries, "research_round": 1, "tried_queries": [], "tried_urls": [],
        "search_results": [], "scraped_content": [], "coverage_note": None, "research_approved": False,
    }


def discover_competitors(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = state["current_run_id"]
    with ctx.emit.node(AGENT, run_id, "discover_competitors", RESEARCHING, "Identifying competitors") as step:
        agent = _agent(ctx)
        company = state.get("company_context") or {}
        known = [{**c, "source": "context"} for c in (state.get("competitor_context") or []) if c.get("name")]
        records = []

        if known:
            step.complete(f"Using {_plural(len(known), 'competitor')} from your marketing context: "
                          f"{', '.join(c['name'] for c in known)}")
            return {"competitors": known, "current_node": "discover_competitors"}

        if not agent.can_search:
            step.complete("No competitors in your context, and web search isn't set up to find them")
            return {"competitors": [], "current_node": "discover_competitors"}

        query = f"{company.get('name', '')} alternatives {company.get('industry') or state['topic']}".strip()
        with ctx.emit.tool("google_search", f'Searching for companies like {company.get("name", "yours")}') as call:
            result = agent.search(query)
            if result.ok:
                call.complete(result.message)
            else:
                call.fail(result.message) if result.status == "error" else call.skip(result.message)
        records.append(call.record)
        found = _ask_model(agent.discover_competitors, company, result.data or []) if result.ok else []
        step.complete(f"Found {_plural(len(found), 'competitor')} through search: {', '.join(c['name'] for c in found)}"
                      if found else "No competitors could be identified from search results")
        return {"competitors": found, "tool_events": records, "current_node": "discover_competitors"}


def search_competitor_content(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = state["current_run_id"]
    round_number = state.get("research_round", 1)
    label = "Searching for competitor articles" if round_number == 1 else "Searching for more sources"
    with ctx.emit.node(AGENT, run_id, "search_competitor_content", RESEARCHING, label) as step:
        agent = _agent(ctx)
        if not agent.can_search:
            with ctx.emit.tool("google_search", "Searching for competitor articles") as call:
                call.skip(agent.search(state["research_queries"][0]).message)
            step.complete("Search isn't set up, so the agent will read competitor websites directly")
            return {"used_search": False, "tool_events": [call.record], "current_node": "search_competitor_content"}

        tried = list(state.get("tried_queries", []))
        planned = [(q, None) for q in state.get("research_queries", []) if q not in tried][:3]
        competitor_domains = [domain_of(c["website"]) for c in state.get("competitors", []) if c.get("website")]
        if round_number == 1 and competitor_domains:
            planned.append((state["topic"], competitor_domains))   # the competitors' own articles

        own = domain_of((state.get("company_context") or {}).get("website"))
        results = list(state.get("search_results", []))
        seen = {r["url"] for r in results}
        records = []
        for query, domains in planned:
            where = f" on {', '.join(domains)}" if domains else ""
            with ctx.emit.tool("google_search", f'Searching "{query}"{where}') as call:
                result = agent.search(query, include_domains=domains)
                if result.ok:
                    call.complete(result.message)
                elif result.status == "empty":
                    call.complete("No results")
                else:
                    call.fail(result.message)
            records.append(call.record)
            tried.append(query if not domains else f"{query} [competitor sites]")
            for item in result.data or []:
                if item["url"] not in seen and item["domain"] != own:
                    seen.add(item["url"])
                    results.append(item)

        new = len(results) - len(state.get("search_results", []))
        step.complete(f"Found {_plural(new, 'candidate article')} from "
                      f"{_plural(len({r['domain'] for r in results}), 'site')}")
        return {"search_results": results, "used_search": True, "tried_queries": tried,
                "tool_events": records, "current_node": "search_competitor_content"}


def scrape_content(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = state["current_run_id"]
    with ctx.emit.node(AGENT, run_id, "scrape_content", RESEARCHING, "Choosing which pages to read") as step:
        agent = _agent(ctx)
        pages = list(state.get("scraped_content", []))
        tried = set(state.get("tried_urls", []))
        competitors = state.get("competitors", [])
        round_number = state.get("research_round", 1)

        # Decide what to read: the best search results, or competitors' own sites when there's no search.
        if state.get("search_results"):
            picks, how = (_ask_model(agent.select_sources, state["topic"], state["search_results"], competitors,
                                     MAX_PAGES - len(pages), tried)
                          if agent.uses_model else
                          agent.select_sources(state["topic"], state["search_results"], competitors,
                                               MAX_PAGES - len(pages), tried))
            targets = [[p["url"]] for p in picks]
        else:
            how = "competitor websites from your context"
            targets = [[f"{c['website'].rstrip('/')}/blog", c["website"]]       # blog first, then homepage
                       for c in competitors if c.get("website")][:MAX_PAGES]
            targets = [[u for u in options if u not in tried] for options in targets]
            targets = [options for options in targets if options]

        records, errors, attempted = [], [], 0
        for options in targets:
            for url in options:
                attempted += 1
                tried.add(url)
                with ctx.emit.tool("web_scraper", f"Reading {domain_of(url)}{'/blog' if url.endswith('/blog') else ''}") as call:
                    result = agent.read(url)
                    if result.ok:
                        call.complete(result.message)
                    else:
                        call.fail(result.message)
                records.append(call.record)
                if result.ok:
                    pages.append(result.data)
                    break                                  # got this source; skip its fallback
                errors.append({"node": "scrape_content", "message": f"{url}: {result.message}"})

        untried = any(r["url"] not in tried for r in state.get("search_results", []))
        enough, note = agent.assess_coverage(pages, round_number, untried)
        update = {"scraped_content": pages, "source_urls": [p["url"] for p in pages], "tried_urls": sorted(tried),
                  "coverage_ok": enough, "coverage_note": note, "tool_events": records, "errors": errors,
                  "current_node": "scrape_content"}

        read_now = sum(1 for r in records if r["status"] == "completed")
        summary = (f"Read {read_now} of {_plural(len(targets), 'source')} ({how}); {note}" if targets
                   else f"No new pages to read; {note}")
        if not enough:
            company = state.get("company_context") or {}
            more = (_ask_model(agent.plan_queries, state["topic"], company, competitors, None,
                               tuple(state.get("tried_queries", [])))
                    if agent.uses_model else
                    default_queries(state["topic"], company, competitors, None, tuple(state.get("tried_queries", []))))
            update.update(research_round=round_number + 1, research_queries=more)
        step.complete(summary)
        return update


def after_scraping(state: ContentCrewState) -> Literal["enough", "search_again"]:
    """The agent's coverage decision, as a conditional edge."""
    return "enough" if state.get("coverage_ok", True) else "search_again"


def analyze_competitors(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    with ctx.emit.node(AGENT, state["current_run_id"], "analyze_competitors", ANALYZING,
                       "Comparing competitor content") as step:
        with ctx.emit.tool("competitor_analysis", "Comparing headings, topics, questions and length") as call:
            result = _agent(ctx).compare(state.get("scraped_content", []))
            call.complete(result.message) if result.ok else call.skip(result.message)
        data = result.data
        step.complete(f"{_plural(len(data['common_themes']), 'shared theme')}, "
                      f"{_plural(len(data['questions_covered']), 'question')} covered, "
                      f"{_plural(len(data['thin_topics']), 'thinly covered topic')}"
                      if result.ok else "Nothing to compare: no competitor pages were read")
    return {"competitor_analysis": data, "tool_events": [call.record], "current_node": "analyze_competitors"}


def analyze_keywords(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    with ctx.emit.node(AGENT, state["current_run_id"], "analyze_keywords", ANALYZING, "Analyzing keywords") as step:
        brands = [c["name"] for c in state.get("competitors", [])] + [(state.get("company_context") or {}).get("name", "")]
        with ctx.emit.tool("keyword_analysis", "Comparing keyword coverage across sources") as call:
            result = _agent(ctx).rank_keywords(state["topic"], state.get("scraped_content", []), brands)
            call.complete(result.message) if result.ok else call.skip(result.message)
        step.complete(f"Ranked {_plural(len(result.data['keywords']), 'keyword')} "
                      "(heuristic; no search-volume data)")
    return {"keywords": result.data["keywords"], "keyword_metrics": result.data["metrics"],
            "tool_events": [call.record], "current_node": "analyze_keywords"}


def identify_content_gaps(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    keywords = state.get("keywords", [])
    analysis = state.get("competitor_analysis") or {}
    with ctx.emit.node(AGENT, state["current_run_id"], "identify_content_gaps", ANALYZING,
                       "Looking for gaps in what competitors cover") as step:
        agent = _agent(ctx)
        if not agent.uses_model:
            step.complete("Skipped gap analysis: it needs a language model, which isn't set up. "
                          "Topics only one source covers are listed instead (heuristic)")
            return {
                "content_gaps": [],
                "content_gaps_note": "Content-gap analysis needs a language model, which isn't set up.",
                "search_intent": None,
                "research_summary_text": None,
                "seo_strategy": {"target_keywords": [k["term"] for k in keywords[:6]], "search_intent": None,
                                 "recommended_structure": fallback_structure(analysis)},
                "current_node": "identify_content_gaps",
            }

        strategy = _ask_model(agent.build_strategy, state["topic"], state.get("company_context") or {},
                              state.get("scraped_content", []), analysis, keywords, state.get("research_feedback"))
        step.complete(f"Found {_plural(len(strategy.content_gaps), 'content gap')} and a "
                      f"{len(strategy.recommended_structure)}-section structure")
    return {
        "content_gaps": strategy.content_gaps,
        "content_gaps_note": None if strategy.content_gaps else "No clear gaps stood out in the sources.",
        "search_intent": strategy.search_intent,
        "research_summary_text": strategy.summary,
        "seo_strategy": {"target_keywords": strategy.target_keywords or [k["term"] for k in keywords[:6]],
                         "search_intent": strategy.search_intent,
                         "recommended_structure": strategy.recommended_structure},
        "current_node": "identify_content_gaps",
    }


# ---------------------------------------------------------------------------
# The finished research: a typed ResearchOutput, plus the approval card
# ---------------------------------------------------------------------------

def _research_output(state: ContentCrewState) -> dict:
    pages = state.get("scraped_content", [])
    analysis = state.get("competitor_analysis") or {}
    strategy = state.get("seo_strategy") or {}
    keywords = state.get("keywords", [])
    text = state.get("research_summary_text") or (
        f'Read {_plural(len(pages), "competitor source")} on "{state["topic"]}". '
        + (f"They commonly cover {', '.join(t['theme'] for t in analysis.get('common_themes', [])[:3])}. "
           if analysis.get("common_themes") else "")
        + "Keyword rankings are heuristic; gap analysis needs a language model."
    )
    return {
        "topic": state["topic"],
        "competitors": state.get("competitors", []),
        "sources": [{"title": p["title"], "url": p["url"], "domain": p["domain"], "word_count": p["word_count"]}
                    for p in pages],
        "keywords": keywords,
        "keyword_metrics": state.get("keyword_metrics") or {},
        "common_topics": [t["theme"] for t in analysis.get("common_themes", [])],
        "common_headings": analysis.get("common_headings", []),
        "competitor_analysis": analysis,
        "content_gaps": state.get("content_gaps", []),
        "search_intent": strategy.get("search_intent"),
        "recommended_structure": strategy.get("recommended_structure", []),
        "seo_strategy": strategy,
        "research_summary": text,
        "used_search": bool(state.get("used_search")),
        "used_model": bool(state.get("research_summary_text")),
    }


def _approval_card(state: ContentCrewState, output: dict) -> dict:
    analysis = state.get("competitor_analysis") or {}
    metrics = state.get("keyword_metrics") or {}
    counts = {k["term"]: k for k in output["keywords"]}
    total = len(output["sources"])

    sources = {"title": "Sources analyzed", "items": [f"{s['title']} ({s['domain']}, {s['word_count']:,} words)"
                                                      for s in output["sources"][:6]]}
    if not total:
        sources["note"] = "No competitor pages could be read, so the post would rely on your company context alone."
    elif not output["used_search"]:
        sources["note"] = "Web search isn't set up, so these are pages from the competitor websites in your context."
    elif state.get("coverage_note"):
        sources["note"] = f"Coverage: {state['coverage_note']}."

    keyword_items = []
    for term in output["seo_strategy"].get("target_keywords", []):
        k = counts.get(term)
        keyword_items.append(f"{term}: in {k['documents']} of {total} sources, {k['difficulty']}, {k['intent']}"
                             if k else term)
    sections = [
        {"title": "Summary", "items": [output["research_summary"]], "open": True},   # shown without expanding
        {"title": "Competitors", "items": [f"{c['name']} ({'found by search' if c.get('source') == 'search' else 'from your context'})"
                                           for c in output["competitors"]]},
        sources,
        {"title": "Recommended keywords", "items": keyword_items,
         "note": f"Difficulty and intent are heuristic assessments from competitor content. {metrics.get('message', '')}".strip()},
    ]
    if output["search_intent"]:
        sections.append({"title": "Search intent", "items": [output["search_intent"]]})
    if output["common_topics"]:
        sections.append({"title": "Topics competitors share",
                         "items": [f"{t['theme']} ({_plural(t['sources'], 'source')})" for t in analysis["common_themes"][:6]]})
    if analysis.get("questions_covered"):
        sections.append({"title": "Questions competitors answer", "items": analysis["questions_covered"][:5]})
    gaps = {"title": "Content gaps", "items": output["content_gaps"]}
    if state.get("content_gaps_note"):
        gaps["note"] = state["content_gaps_note"]
        if analysis.get("thin_topics"):
            gaps["items"] = [f"Only one source covers: {t} (heuristic)" for t in analysis["thin_topics"][:4]]
    sections.append(gaps)
    structure = {"title": "Recommended structure", "items": output["recommended_structure"]}
    if not structure["items"]:
        structure["note"] = "Not enough material to suggest a structure. The Generation Agent will propose one."
    sections.append(structure)

    return {
        "headline": f'Research for "{output["topic"]}": {_plural(total, "source")} analyzed, '
                    f'{_plural(len(output["keywords"]), "keyword")} ranked.',
        # The counts at the top of the card; the sections open with "Review strategy".
        "stats": [
            {"label": "competitors", "value": len(output["competitors"])},
            {"label": "sources analyzed", "value": total},
            {"label": "keywords", "value": len(output["keywords"])},
            {"label": "content gaps", "value": len(output["content_gaps"])},
        ],
        "sections": sections,
    }


def create_research_summary(state: ContentCrewState, runtime: Runtime[WorkflowContext]) -> dict:
    ctx = runtime.context
    run_id = state["current_run_id"]
    with ctx.emit.node(AGENT, run_id, "create_research_summary", ANALYZING, "Writing up the research") as step:
        output = _research_output(state)
        card = _approval_card(state, output)
        ResearchRepository(ctx.db).add_run(ctx.session_id, len(output["sources"]), len(output["keywords"]),
                                           topic=output["topic"], summary=output["research_summary"])
        step.complete("Research summary ready for your review")
    ctx.emit.agent_completed(AGENT, run_id, "Research complete")
    return {"research_output": output, "research_summary": card, "current_node": "create_research_summary"}
