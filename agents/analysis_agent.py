"""
Analysis Agent: research and marketing intelligence.

The agent has two parts:

TOOLS (its hands)            search() read() compare() rank_keywords()
    Thin wrappers over tools/. Calling a tool always goes through the agent, so
    "the Analysis Agent used Google Search" is literally true in the code.

DECISIONS (its judgement)    discover_competitors() plan_queries() select_sources()
                             assess_coverage() build_strategy()
    Each decision uses the language model when one is set up and falls back to a
    transparent rule when not, and the nodes say which happened. Decisions are
    bounded by the graph: the agent can choose what to search and read and whether
    to search again, but it can't skip the human approval or publish anything.

The nodes in nodes/analysis_nodes.py call these, one step per node, and the
LangGraph edges decide the order (including the "search again?" loop).
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.policy import UNTRUSTED_CONTENT_RULES, as_search_result, as_source, escape
from config import Settings
from llm.structured import ask_structured
from tools import ToolResult, competitor_analysis, google_search, keyword_analysis, web_scraper
from tools.text import tokens

SYSTEM_PROMPT = f"""You are the Analysis Agent in ContentCrew, a marketing research specialist.
You study competitor content and recommend a content strategy for one blog post.

Base your answers on the sources and the company context given. Do not invent statistics,
rankings, search volumes, traffic numbers or quotes. Answer only in the requested structure.

{UNTRUSTED_CONTENT_RULES}"""

MIN_SOURCES = 3          # fewer distinct sites than this and the agent searches again (if it can)
PER_SITE = 2             # read at most this many pages from one site, so sources stay varied
MAX_ROUNDS = 2           # at most one extra search round, to stay inside the free search quota
MAX_PAGES = 5

# Sites that are rarely a competitor's article (directories, social, video, encyclopaedias).
NOT_ARTICLES = {
    "youtube.com", "facebook.com", "linkedin.com", "twitter.com", "x.com", "instagram.com",
    "tiktok.com", "reddit.com", "quora.com", "pinterest.com", "wikipedia.org", "en.wikipedia.org",
    "g2.com", "capterra.com", "trustpilot.com", "amazon.com", "play.google.com", "apps.apple.com",
}


# ---------------------------------------------------------------------------
# Structured answers from the model. List lengths aren't enforced here on
# purpose: a free model sometimes returns one item too many, and that shouldn't
# fail the step. The code trims instead.
# ---------------------------------------------------------------------------

class ResearchQueries(BaseModel):
    queries: list[str] = Field(description="3 to 4 web search queries that would find strong articles on the topic")


class FoundCompetitor(BaseModel):
    name: str = Field(description="Company name")
    website: str = Field(description="The company's website domain, e.g. example.com")


class CompetitorList(BaseModel):
    competitors: list[FoundCompetitor] = Field(
        description="Up to 4 companies in the results that compete with the company described")


class SourcePicks(BaseModel):
    ids: list[int] = Field(description="The ids of the most useful articles to read, best first")


class ContentStrategy(BaseModel):
    search_intent: str = Field(description="One sentence: what someone searching this topic wants to learn or do")
    content_gaps: list[str] = Field(description="Up to 5 specific points the sources cover poorly or not at all")
    recommended_structure: list[str] = Field(description="5 to 8 section headings for an original post")
    target_keywords: list[str] = Field(description="Up to 8 keywords to target, chosen from the keyword list provided")
    summary: str = Field(description="2 or 3 plain sentences summarising the research for a marketer")


def _clean_list(items, limit: int, max_len: int = 150) -> list[str]:
    seen, out = set(), []
    for item in items or []:
        text = " ".join(str(item).split())[:max_len]
        if text and text.lower() not in seen:
            seen.add(text.lower())
            out.append(text)
    return out[:limit]


def domain_of(url: Optional[str]) -> str:
    """'https://www.akeneo.com/blog' and 'akeneo.com' both give 'akeneo.com'."""
    value = (url or "").strip()
    if value and "//" not in value:
        value = f"https://{value}"
    return (urlparse(value).hostname or "").removeprefix("www.")


def _sources(n: int) -> str:
    return f"{n} usable source{'' if n == 1 else 's'}"


def _is_homepage(url: str) -> bool:
    return urlparse(url).path.strip("/") == ""


# ---------------------------------------------------------------------------
# Rules used when there's no model (and as a safety net when there is)
# ---------------------------------------------------------------------------

def default_queries(topic: str, company: dict, competitors: list[dict], feedback: Optional[str] = None,
                    avoid: tuple[str, ...] = ()) -> list[str]:
    queries = [topic, f"{topic} guide", f"what is {topic}", f"{topic} examples", f"{topic} best practices"]
    if company.get("industry"):
        queries.insert(2, f"{topic} {company['industry']}")
    if feedback:
        queries.insert(0, f"{topic} {feedback[:60]}")
    tried = {q.lower() for q in avoid}
    return [q for q in _clean_list(queries, 8) if q.lower() not in tried][:4]


def fallback_structure(competitor_analysis_data: dict) -> list[str]:
    """Without a model: the competitor headings that cover the most shared themes."""
    headings = (competitor_analysis_data.get("common_headings")
                or competitor_analysis_data.get("representative_headings") or [])[:5]
    return ["Introduction", *headings, "Conclusion"] if headings else []


def rank_sources(topic: str, results: list[dict], competitor_domains: set[str]) -> list[dict]:
    """Relevance rule: competitor articles first, then titles that mention the topic, articles over homepages."""
    topic_words = {w for w in tokens(topic) if len(w) > 2}

    def score(result: dict) -> float:
        title_words = set(tokens(result["title"]))
        value = 3.0 if result["domain"] in competitor_domains else 0.0
        value += 1.5 * len(topic_words & title_words)
        value += -2.0 if _is_homepage(result["url"]) else 0.5
        return value

    return sorted(results, key=score, reverse=True)


class AnalysisAgent:
    name = "analysis"
    tools = ("google_search", "web_scraper", "competitor_analysis", "keyword_analysis")

    def __init__(self, settings: Settings, llm=None):
        self.settings = settings
        self.llm = llm

    @property
    def uses_model(self) -> bool:
        return self.llm is not None

    @property
    def can_search(self) -> bool:
        return google_search.is_configured(self.settings)

    # -- tools ----------------------------------------------------------------

    def search(self, query: str, include_domains: Optional[list[str]] = None, max_results: int = 6) -> ToolResult:
        return google_search.search(query, self.settings, max_results=max_results, include_domains=include_domains)

    def read(self, url: str) -> ToolResult:
        return web_scraper.fetch_page(url, self.settings)

    def compare(self, pages: list[dict]) -> ToolResult:
        return competitor_analysis.compare(pages)

    def rank_keywords(self, topic: str, pages: list[dict], brand_names: list[str]) -> ToolResult:
        return keyword_analysis.analyze(topic, pages, self.settings, brand_names=brand_names)

    # -- decisions ------------------------------------------------------------

    def _ask(self, schema, prompt: str):
        return ask_structured(self.llm, schema, [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)])

    def plan_queries(self, topic: str, company: dict, competitors: list[dict],
                     feedback: Optional[str] = None, avoid: tuple[str, ...] = ()) -> list[str]:
        fallback = default_queries(topic, company, competitors, feedback, avoid)
        if self.llm is None:
            return fallback
        prompt = (
            f"Topic: {escape(topic)}\n"
            f"Company: {escape(company.get('name'))} ({escape(company.get('industry') or 'industry not given')})\n"
            f"Audience: {escape(', '.join(company.get('target_audience', [])))}\n"
            f"Known competitors: {escape(', '.join(c['name'] for c in competitors)) or 'none'}\n"
            + (f"Already searched (use different queries): {escape('; '.join(avoid))}\n" if avoid else "")
            + (f"The user asked for these changes to earlier research: {escape(feedback)}\n" if feedback else "")
            + "Write the search queries."
        )
        tried = {q.lower() for q in avoid}
        queries = [q for q in _clean_list(self._ask(ResearchQueries, prompt).queries, 6) if q.lower() not in tried]
        return queries[:4] or fallback

    def discover_competitors(self, company: dict, results: list[dict]) -> list[dict]:
        """Pick competing companies out of search results (only when the context names none)."""
        own = domain_of(company.get("website"))
        candidates = [r for r in results if r["domain"] != own and r["domain"] not in NOT_ARTICLES]
        if not candidates:
            return []
        if self.llm is not None:
            listing = "\n".join(as_search_result(i, r) for i, r in enumerate(candidates[:10]))
            answer = self._ask(CompetitorList, (
                f"Company: {escape(company.get('name'))}: {escape(company.get('description'))}\n"
                f"Search results:\n{listing}\n\n"
                "Which companies in these results compete with this company? Use only companies that appear above."
            ))
            known = {r["domain"] for r in candidates}
            found = []
            for item in answer.competitors[:4]:
                site = domain_of(item.website if "//" in item.website else f"https://{item.website}")
                if site in known and site != own:      # must come from the actual results
                    found.append({"name": item.name.strip()[:80], "website": f"https://{site}", "source": "search"})
            if found:
                return found
        # Rule: distinct domains in the results, named after the domain.
        seen, found = set(), []
        for result in candidates:
            if result["domain"] not in seen:
                seen.add(result["domain"])
                name = result["domain"].split(".")[0].replace("-", " ").title()
                found.append({"name": name, "website": f"https://{result['domain']}", "source": "search"})
        return found[:3]

    def select_sources(self, topic: str, results: list[dict], competitors: list[dict], limit: int,
                       avoid: set[str]) -> tuple[list[dict], str]:
        """Choose which results to read. Returns (picks, how the choice was made)."""
        competitor_domains = {domain_of(c.get("website")) for c in competitors if c.get("website")}
        candidates = [r for r in results if r["url"] not in avoid and r["domain"] not in NOT_ARTICLES
                      and not r["url"].lower().endswith(".pdf")]
        ranked, per_site = [], {}
        for r in rank_sources(topic, candidates, competitor_domains):
            if per_site.get(r["domain"], 0) < PER_SITE:
                per_site[r["domain"]] = per_site.get(r["domain"], 0) + 1
                ranked.append(r)
        if self.llm is None or len(ranked) <= limit:
            return ranked[:limit], "ranked by relevance"

        listing = "\n".join(as_search_result(i, r) for i, r in enumerate(ranked[:12]))
        picks = self._ask(SourcePicks, (
            f"Topic: {escape(topic)}\nCompetitor sites: {escape(', '.join(sorted(competitor_domains))) or 'none'}\n"
            f"Search results:\n{listing}\n\n"
            f"Pick the {limit} results most likely to be in-depth articles on the topic, preferring competitors."
        )).ids
        chosen, seen = [], set()
        for i in picks:
            if isinstance(i, int) and 0 <= i < min(len(ranked), 12) and i not in seen:
                seen.add(i)
                chosen.append(ranked[i])
        for result in ranked:                       # top up with the rule if the model picked too few
            if len(chosen) >= limit:
                break
            if result not in chosen:
                chosen.append(result)
        return chosen[:limit], "chosen by the agent"

    def assess_coverage(self, pages: list[dict], round_number: int, untried_left: bool) -> tuple[bool, str]:
        """Enough material to analyze? Counts distinct sites. If not, the graph loops back to search."""
        sites = len({p.get("domain") for p in pages})
        if sites >= MIN_SOURCES:
            return True, f"{_sources(sites)} from different sites"
        if not self.can_search:
            return True, "web search isn't set up, so this is everything available"
        if round_number >= MAX_ROUNDS:
            return True, f"only {_sources(sites)} after {round_number} search rounds"
        return False, f"only {_sources(sites)}, so searching again"

    def build_strategy(self, topic: str, company: dict, pages: list[dict], analysis: dict,
                       keywords: list[dict], feedback: Optional[str] = None) -> ContentStrategy:
        sources = "\n\n".join(as_source(p) for p in pages[:4]) or "(No competitor pages could be read.)"
        keyword_list = ", ".join(f"{k['term']} ({k['documents']} sources)" for k in keywords[:15]) or "none"
        themes = ", ".join(f"{t['theme']} ({t['sources']})" for t in analysis.get("common_themes", [])) or "none"
        questions = "; ".join(analysis.get("questions_covered", [])) or "none"
        thin = ", ".join(analysis.get("thin_topics", [])) or "none"
        prompt = (
            f"Topic: {escape(topic)}\n"
            f"Company: {escape(company.get('name'))}: {escape(company.get('description'))}\n"
            f"Audience: {escape(', '.join(company.get('target_audience', [])))}\n"
            f"Keywords found in competitor content: {escape(keyword_list)}\n"
            f"Themes several competitors share: {escape(themes)}\n"
            f"Questions competitors answer: {escape(questions)}\n"
            f"Themes only one competitor covers: {escape(thin)}\n"
            + (f"The user asked for these changes to earlier research: {escape(feedback)}\n" if feedback else "")
            + f"\nCompetitor sources:\n{sources}\n\n"
            "Recommend the strategy for an original post that serves this audience better than these sources."
        )
        strategy = self._ask(ContentStrategy, prompt)
        strategy.content_gaps = _clean_list(strategy.content_gaps, 5, 200)
        strategy.recommended_structure = _clean_list(strategy.recommended_structure, 8, 120)
        strategy.target_keywords = _clean_list(strategy.target_keywords, 8, 80)
        strategy.search_intent = " ".join(strategy.search_intent.split())[:300]
        strategy.summary = " ".join(strategy.summary.split())[:600]
        return strategy
