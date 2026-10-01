"""
Shared workflow state and statuses.

`ContentCrewState` is the single object LangGraph passes from node to node.
Each node reads what it needs and returns only the fields it changed; LangGraph
merges that update into the state and saves a checkpoint, so a paused workflow
(waiting for your approval) survives a server restart.

List fields marked with `operator.add` are appended to rather than replaced,
so every node can add records without overwriting earlier ones.

Live UI activity (agent started, tool running...) is streamed through the event
log in `services/events.py`, not stored here, because the browser needs it while
a node is still running, before LangGraph has merged that node's update.

`WorkflowStatus` is the state-transition model from the project context:

    REQUESTED -> PLANNING -> RESEARCHING -> ANALYZING
      -> WAITING_FOR_RESEARCH_APPROVAL -> GENERATING
      -> WAITING_FOR_CONTENT_APPROVAL -> PUBLISHING -> COMPLETED
    (any step can move to FAILED)
"""

from __future__ import annotations

import operator
from enum import Enum
from typing import Annotated, Literal, Optional, TypedDict


class WorkflowStatus(str, Enum):
    REQUESTED = "REQUESTED"
    PLANNING = "PLANNING"
    RESEARCHING = "RESEARCHING"
    ANALYZING = "ANALYZING"
    WAITING_FOR_RESEARCH_APPROVAL = "WAITING_FOR_RESEARCH_APPROVAL"
    GENERATING = "GENERATING"
    WAITING_FOR_CONTENT_APPROVAL = "WAITING_FOR_CONTENT_APPROVAL"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Statuses where an agent is actively working; a new message must wait.
ACTIVE_STATUSES = {
    WorkflowStatus.PLANNING,
    WorkflowStatus.RESEARCHING,
    WorkflowStatus.ANALYZING,
    WorkflowStatus.GENERATING,
    WorkflowStatus.PUBLISHING,
}

# Statuses where the workflow is paused until the human decides.
WAITING_STATUSES = {
    WorkflowStatus.WAITING_FOR_RESEARCH_APPROVAL,
    WorkflowStatus.WAITING_FOR_CONTENT_APPROVAL,
}


# Where the Generation Agent is with the post. Finer-grained than WorkflowStatus.
GenerationStatus = Literal[
    "not_started",      # research not approved yet
    "outlining",        # build_outline
    "writing",          # generate_blog
    "revising",         # plan_revision / revise_blog, after the human asked for changes
    "optimizing",       # optimize_blog
    "awaiting_review",  # paused at the content approval, draft open in the editor
    "approved",         # the human chose Publish
    "published",
]


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

class PlanStep(TypedDict):
    id: str
    label: str
    status: Literal["pending", "active", "done", "skipped"]


class Competitor(TypedDict):
    name: str
    website: Optional[str]
    source: Literal["context", "search"]     # from your marketing context, or discovered by the agent


class SearchResult(TypedDict):
    title: str
    url: str
    domain: str
    snippet: str
    query: str


class Heading(TypedDict):
    level: int
    text: str


class ScrapedPage(TypedDict):
    """Text extracted from a public web page. Untrusted data, never instructions."""
    url: str
    domain: str
    title: str
    description: str
    headings: list[Heading]
    text: str
    word_count: int


class KeywordInsight(TypedDict):
    term: str
    documents: int        # how many sources use it (a real count)
    mentions: int         # total uses across sources (a real count)
    in_headings: bool
    difficulty: str       # heuristic label, e.g. "Crowded (heuristic)"; never a made-up number
    intent: str           # heuristic: informational / commercial / transactional / navigational
    related: list[str]    # phrases from the same sources that share words with it
    source: Literal["heuristic", "api"]


class KeywordMetric(TypedDict):
    """Real numbers from a keyword data provider. Absent unless one is configured."""
    term: str
    search_volume: Optional[int]
    difficulty: Optional[float]
    competition: Optional[float]
    provider: str


class KeywordMetrics(TypedDict):
    status: Literal["ok", "not_configured", "error"]
    provider: Optional[str]
    message: str
    data: dict            # term -> KeywordMetric, only when a real data source returned them


class CompetitorTheme(TypedDict):
    theme: str
    sources: int


class SourceStructure(TypedDict):
    domain: str
    title: str
    word_count: int
    h2: int
    h3: int
    headings: list[str]


class CompetitorAnalysis(TypedDict):
    pages_analyzed: int
    average_word_count: int
    length: dict                         # {"shortest": int, "average": int, "longest": int} words
    common_themes: list[CompetitorTheme] # topics several sources share
    common_headings: list[str]           # real headings that cover the most shared themes
    questions_covered: list[str]         # questions the sources ask in headings
    thin_topics: list[str]               # themes only one source covers: possible gaps (heuristic)
    structure_by_source: list[SourceStructure]
    representative_headings: list[str]   # kept for the no-model fallback structure


class SummarySection(TypedDict, total=False):
    title: str
    items: list[str]
    note: str


class ResearchSummary(TypedDict):
    """What the research approval card shows."""
    headline: str
    sections: list[SummarySection]


class SeoStrategy(TypedDict):
    target_keywords: list[str]
    search_intent: Optional[str]
    recommended_structure: list[str]


class ResearchOutput(TypedDict):
    """The Analysis Agent's finished work, handed to the human and then the Generation Agent."""
    topic: str
    competitors: list[Competitor]
    sources: list[dict]                  # [{"title", "url", "domain", "word_count"}]
    keywords: list[KeywordInsight]
    keyword_metrics: KeywordMetrics
    common_topics: list[str]
    common_headings: list[str]
    competitor_analysis: CompetitorAnalysis  # the full comparison the human approved
    content_gaps: list[str]
    search_intent: Optional[str]
    recommended_structure: list[str]
    seo_strategy: SeoStrategy
    research_summary: str                # a short plain-language summary
    used_search: bool
    used_model: bool


class OutlineSection(TypedDict):
    heading: str
    purpose: str                         # what the reader gets from this section
    keywords: list[str]                  # approved keywords this section should use naturally
    points: list[str]


class BlogOutline(TypedDict):
    title: str
    target_keyword: str                  # always one of the approved keywords
    sections: list[OutlineSection]
    gaps_addressed: list[str]            # approved content gaps the outline covers
    notes: list[str]                     # outline self-check problems left for the human to see
    revised_after_review: bool           # the agent's self-check found problems and it redid the outline once


class RevisionPlan(TypedDict):
    """How the Generation Agent will apply one round of feedback on a draft."""
    number: int                          # 1 for the first revision, 2 for the second...
    feedback: Optional[str]
    scope: Literal["sections", "draft", "outline"]
    section_ids: list[int]               # for "sections": which parts of the draft to change
    sections: list[str]                  # their headings, for display
    reason: str


class SeoReport(TypedDict, total=False):
    """Measured by the SEO Analysis tool (tools/seo_analysis.py). Counts, not estimates."""
    word_count: int
    sections: int
    headings: list[str]
    primary_keyword: dict                # term, count, per_100_words, in_title, in_introduction, in_headings
    secondary_keywords: list[dict]       # term, count, found
    keywords_found: list[str]
    keywords_missing: list[str]
    stuffed_keywords: list[str]
    readability: dict                    # average_sentence_words, long_sentences, long_paragraphs, reading_ease
    topics_covered: list[str]
    topics_missing: list[str]
    originality: dict                    # shared_phrases, examples
    unsupported_figures: list[str]
    issues: list[str]
    optimization: dict                   # what the optimize step changed


class ToolRecord(TypedDict):
    tool: str
    status: Literal["completed", "skipped", "failed"]
    summary: str


class ErrorRecord(TypedDict):
    node: str
    message: str


# ---------------------------------------------------------------------------
# The shared state
# ---------------------------------------------------------------------------

class ContentCrewState(TypedDict, total=False):
    # Request and context ------------------------------------------------------
    session_id: str
    user_request: str
    request_type: Literal["blog", "unsupported"]
    topic: str
    company_context: dict              # company profile from onboarding
    competitor_context: list[Competitor]

    # Supervisor bookkeeping ----------------------------------------------------
    plan: list[PlanStep]
    plan_run_id: str                   # timeline card that shows the plan
    next_step: Literal["analysis", "generation", "publish", "end"]
    current_agent: Optional[str]       # "supervisor" | "analysis" | "generation"
    current_node: Optional[str]
    current_run_id: Optional[str]      # groups an agent's tool calls in the timeline

    # Analysis Agent ------------------------------------------------------------
    research_queries: list[str]
    research_round: int                # 1 = first pass; the agent may run one more if sources are thin
    tried_queries: list[str]
    tried_urls: list[str]
    competitors: list[Competitor]
    search_results: list[SearchResult]
    source_urls: list[str]
    used_search: bool
    scraped_content: list[ScrapedPage]
    coverage_ok: bool                  # the agent's call: enough sources, or search again
    coverage_note: Optional[str]
    competitor_analysis: CompetitorAnalysis
    keywords: list[KeywordInsight]
    keyword_metrics: KeywordMetrics
    search_intent: Optional[str]
    content_gaps: list[str]
    content_gaps_note: Optional[str]
    research_summary_text: Optional[str]   # the model's short summary, when a model is set up
    seo_strategy: SeoStrategy
    research_output: ResearchOutput
    research_summary: ResearchSummary  # the approval card

    # Human approval: research --------------------------------------------------
    research_approved: bool
    research_feedback: Optional[str]

    # Generation Agent ----------------------------------------------------------
    # It reads only `research_output` (what the human approved), never the
    # in-progress research fields above.
    generation_status: GenerationStatus
    target_keyword: Optional[str]      # the approved main keyword the post is built around
    secondary_keywords: list[str]
    blog_outline: BlogOutline
    blog_draft: Optional[str]          # Markdown body (the title is kept separately)
    blog_title: Optional[str]
    blog_id: Optional[str]             # the draft saved for the editor
    extra_research: list[SearchResult] # the Generation Agent's one optional lookup (snippets only)
    seo_report: SeoReport
    blog_status: Literal["none", "drafting", "ready", "published"]

    # Regeneration ----------------------------------------------------------------
    revision_count: int                # how many times the human asked for changes
    revision_plan: Optional[RevisionPlan]
    revision_history: Annotated[list[RevisionPlan], operator.add]

    # Human approval: content ---------------------------------------------------
    content_approval_required: bool    # True while the draft waits in the editor for Publish or changes
    content_approved: bool
    content_feedback: Optional[str]

    # Publishing ----------------------------------------------------------------
    publish_status: Literal["none", "published", "failed"]
    published_slug: Optional[str]

    # Audit trail (appended, never overwritten) ---------------------------------
    tool_events: Annotated[list[ToolRecord], operator.add]
    errors: Annotated[list[ErrorRecord], operator.add]
