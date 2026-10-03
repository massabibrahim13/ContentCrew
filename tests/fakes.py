"""
Test doubles. These exist only in tests so the full workflow can be exercised
without an API key or internet access. The app itself never uses them.
"""

from __future__ import annotations

import re

from langchain_core.messages import AIMessage

from agents.analysis_agent import CompetitorList, ContentStrategy, ResearchQueries, SourcePicks
from agents.generation_agent import OutlineModel, RevisionDecision
from tools import ToolResult

BLOG_MARKDOWN = """## Introduction

Agentic AI is changing how marketing teams plan and publish content. Instead of answering one prompt at a time, an agent works toward a goal. It breaks the goal into steps, uses tools and checks its own results. For a product team, that means less copying between tabs and more time for decisions.

This guide explains what the approach means in practice, how a typical workflow runs, and where people stay in charge. It is written for retail and e-commerce teams that manage a lot of product content.

## What agentic AI means for product teams

An agent is software that can plan, act and review. It reads the request, decides which step comes next and picks a tool for that step. A research step might search the web, while a writing step drafts text from approved notes.

The difference from a chatbot is the loop. The agent keeps going until the goal is met or a person needs to decide something. That makes it useful for work with many small steps, such as keeping product descriptions accurate across channels.

## How an agentic workflow works

Most setups use a supervisor and a few specialists. The supervisor understands the request and hands work to the right agent. One agent researches competitors and keywords. Another turns the approved research into a draft.

Each step leaves a record. You can see which tool ran, what it returned and what happens next. When something fails, the workflow stops at that step and can continue from there once the problem is fixed.

## Keeping humans in the loop

Approval checkpoints keep people in charge. The workflow pauses after research, so a marketer can check the strategy before any writing starts. It pauses again when the draft is ready, so nothing is published without a clear yes.

These pauses are not a formality. They are where brand knowledge, legal checks and common sense come in. A good agent makes those reviews faster because the evidence is laid out in one place.

## Getting started

Start with one content type and one clear goal. Write down your audience, your competitors and the tone you want. Then let the agents handle the repetitive steps while your team reviews the results."""

NEW_INTRO = ("## Introduction\n\nAgentic AI helps product teams plan, write and check content in fewer steps. "
             "This guide shows how it works and where people stay in charge.")

OUTLINE = dict(
    title="Agentic AI for Product Teams: A Practical Guide",
    target_keyword="agentic ai",
    sections=[
        {"heading": "Introduction", "purpose": "Why this matters now", "keywords": ["agentic ai"],
         "points": ["What changes for product teams"]},
        {"heading": "What agentic AI means for product teams", "purpose": "A plain definition",
         "keywords": ["agentic ai", "not-approved keyword"], "points": ["Goals, steps and tools"]},
        {"heading": "How an agentic workflow works", "purpose": "The moving parts",
         "keywords": ["data quality"], "points": ["Supervisor and specialists"]},
        {"heading": "Keeping humans in the loop", "purpose": "Where approval fits",
         "points": ["Approval checkpoints"]},
        {"heading": "Getting started", "purpose": "First steps", "points": ["Start small"]},
    ],
    gaps_addressed=["How human approval works in practice"],
)


class AuthenticationError(Exception):
    """Same class name as the OpenAI SDK's, so error descriptions can be tested."""


class RateLimitError(Exception):
    """Same class name as the Groq/OpenAI SDKs' rate-limit error."""


def _post_in(prompt: str) -> str:
    match = re.search(r"<post>\n(.*?)\n</post>", prompt, flags=re.DOTALL)
    return match.group(1) if match else BLOG_MARKDOWN


def _revision_decision(messages) -> RevisionDecision:
    """Answers like a model would: intro edits touch one section, structure changes redo the outline."""
    asked = re.search(r'asked for a change: "(.*?)"', messages[-1].content, flags=re.DOTALL).group(1).lower()
    if "introduction" in asked:
        return RevisionDecision(scope="sections", sections=[0])
    if "section about" in asked:
        return RevisionDecision(scope="outline")
    return RevisionDecision(scope="draft")


class _Structured:
    def __init__(self, llm: "FakeLLM", schema):
        self.llm, self.schema = llm, schema

    def invoke(self, messages):
        name = self.schema.__name__
        if self.llm.fail_with and (not self.llm.fail_on or name in self.llm.fail_on):
            raise self.llm.fail_with
        self.llm.calls.append((name, messages))
        answer = self.llm.answers[name]
        return answer(messages) if callable(answer) else answer.pop(0)   # a list = answers in order


class FakeLLM:
    """
    Same interface the agents use (`with_structured_output`, `invoke`), canned answers.
    fail_with + fail_on: raise that error, only for those kinds of call ("OutlineModel", "text", ...).
    """

    def __init__(self, fail_with: Exception | None = None, fail_on: set[str] | None = None):
        self.fail_with = fail_with
        self.fail_on = set(fail_on or ())
        self.calls: list[tuple[str, list]] = []
        self.drafts_written = 0
        self.answers = {
            "ResearchQueries": lambda m: ResearchQueries(queries=["agentic ai marketing", "agentic ai examples"]),
            "ContentStrategy": lambda m: ContentStrategy(
                search_intent="Understand what agentic AI is and how a team can start using it.",
                content_gaps=["How human approval works in practice", "What agents cost to run"],
                recommended_structure=["Introduction", "What is agentic AI?", "How it works",
                                       "Where humans stay in the loop"],
                target_keywords=["agentic ai", "data quality"],
                summary="Competitors explain agentic AI well but skip how approval works in practice.",
            ),
            "CompetitorList": lambda m: CompetitorList(competitors=[
                {"name": "Akeneo", "website": "akeneo.com"},
                {"name": "Invented Corp", "website": "not-in-results.example"},   # must be rejected
            ]),
            "SourcePicks": lambda m: SourcePicks(ids=[2, 0, 99]),
            "OutlineModel": lambda m: OutlineModel(**OUTLINE),
            "RevisionDecision": _revision_decision,
        }

    def with_structured_output(self, schema, **_method):
        return _Structured(self, schema)

    def invoke(self, messages):
        if self.fail_with and (not self.fail_on or "text" in self.fail_on):
            raise self.fail_with
        self.calls.append(("text", messages))
        prompt = messages[-1].content
        if "Rewrite only this section" in prompt:
            return AIMessage(content=NEW_INTRO)
        if "Improve this blog post" in prompt:
            return AIMessage(content=_post_in(prompt) + "\n\nGood data quality is what makes agents useful.")
        self.drafts_written += 1
        return AIMessage(content=f"# Title to strip\n\n{BLOG_MARKDOWN}\n\nDraft {self.drafts_written}.")

    def prompts(self, kind: str) -> list[str]:
        return [m[-1].content for k, m in self.calls if k == kind]


def page(domain: str, headings: list[str], extra_text: str = "") -> dict:
    body = " ".join(
        f"{h}. Product information management keeps product data accurate for retail teams. "
        f"Agentic AI automates product content and data quality checks."
        for h in headings
    ) * 3 + " " + extra_text
    return {"url": f"https://www.{domain}/", "domain": domain, "title": f"{domain} blog",
            "headings": [{"level": 2, "text": h} for h in headings], "text": body,
            "word_count": len(body.split())}


PAGES = {
    "akeneo.com": page("akeneo.com", ["What is product information management", "How agentic AI changes product content"]),
    "salsify.com": page("salsify.com", ["Agentic AI for commerce teams", "Data quality at scale"]),
    "plytix.com": page("plytix.com", ["Why product information management matters", "Data quality checklist"]),
    "example-blog.com": page("example-blog.com", ["What is agentic AI?", "Agentic AI examples"]),
}


UNREADABLE = set()   # domains whose pages "fail to load" in a test


def fake_fetch_page(url: str, settings) -> ToolResult:
    domain = url.split("//", 1)[1].split("/", 1)[0].removeprefix("www.")
    if domain in PAGES and domain not in UNREADABLE:
        return ToolResult.success("Read the page", {**PAGES[domain], "url": url})
    return ToolResult.error("Couldn't load the page.")


class FakeSearch:
    """Stands in for the search API: returns articles on the fixture domains."""

    def __init__(self, domains=("akeneo.com", "salsify.com", "plytix.com", "example-blog.com"), fail=False):
        self.domains = list(domains)
        self.fail = fail
        self.calls: list[tuple[str, list]] = []

    def __call__(self, query, settings, max_results=8, include_domains=None):
        self.calls.append((query, include_domains or []))
        if self.fail:
            return ToolResult.error("Search failed (HTTP 500).")
        domains = include_domains or self.domains
        slug = "-".join(query.lower().split())[:40]
        results = [{"title": f"{query.title()} | {d}", "url": f"https://www.{d}/blog/{slug}",
                    "domain": d, "snippet": f"About {query}", "query": query} for d in domains]
        return ToolResult.success(f"{len(results)} results", results)
