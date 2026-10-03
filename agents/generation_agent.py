"""
Generation Agent: content creation.

Turns the research a human approved into an original, SEO-informed blog post.
Like the Analysis Agent it has tools and decisions; the nodes in
nodes/generation_nodes.py call them one step at a time and report each one.

TOOLS (its hands)          look_up()   one optional web search, snippets only
                           measure()   the SEO Analysis tool (counts, not estimates)

DECISIONS (its judgement)  build_outline()    structure the post around the approved strategy,
                                              check the outline, and fix it once if needed
                           write_blog()       the full draft from the outline
                           optimize()         fix only the problems the SEO Analysis measured
                           plan_revision()    after feedback: which step to go back to
                           revise_sections()  rewrite only the parts the feedback is about

What it reads: `GenerationBrief.from_state()` builds the brief from
`research_output` (exactly what the human approved), the company context and the
user's request, and refuses if the research isn't approved. The writer never sees
competitor page text, only the analysis of it, so there's nothing to copy;
competitor headings are shown only so it can avoid reproducing them.

Code keeps the agent inside the approved strategy: the target keyword must be one
of the approved keywords, section keywords are filtered to the approved list, and
"content gaps covered" must match approved gaps.

Writing needs a language model, so unlike the Analysis Agent there's no
rule-based fallback for writing: the nodes stop with a retryable message instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agents.policy import UNTRUSTED_CONTENT_RULES, as_search_result, escape
from config import Settings
from llm.structured import ask_structured
from tools import ToolResult, google_search, seo_analysis
from tools.seo_analysis import count_term, plain_text, words_of
from tools.text import STOPWORDS, heading_key, join_sections, split_sections, tokens

SYSTEM_PROMPT = """You are the Generation Agent in ContentCrew, a senior content writer.
You turn a content strategy that a person has approved into an original, SEO-informed blog post.

Rules:
- Follow the approved strategy: its main keyword, search intent, content gaps and structure.
- Write original content. Never copy sentences, and never reuse a competitor's headings or structure;
  competitor headings are shown to you only so you can do better than them.
- Do not invent statistics, percentages, survey results, quotes, customer names, prices, dates or
  sources. Use a figure only if it appears in the company context or the research notes. Where a
  point would need data you don't have, make it without numbers.
- Write for the target audience in clear, plain language: short paragraphs, mostly short sentences.
- Use keywords only where they read naturally. Never repeat a keyword just to raise its count.
- Mention the company where it helps the reader, two or three times at most. This is an article,
  not an advert.

""" + UNTRUSTED_CONTENT_RULES

MIN_DRAFT_WORDS = 250          # anything shorter is a failed generation, not a draft
MAX_SECTIONS = 9
MAX_REVISED_SECTIONS = 3       # more than this and a full rewrite is the honest choice
SCOPES = ("sections", "draft", "outline")
GENERIC_HEADINGS = {"introduction", "conclusion", "summary", "overview", "faq", "faqs", "next steps",
                    "final thoughts", "key takeaways", "frequently asked questions", "getting started"}


class GenerationProblem(ValueError):
    """The model answered, but the answer can't be used (too short, incomplete). Retryable."""


class ResearchNotReady(Exception):
    """There's no approved research to write from. Not retryable: the research has to be redone."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ---------------------------------------------------------------------------
# Structured answers from the model. Only the essentials are required, so a
# free model that leaves out an optional field doesn't fail the step.
# ---------------------------------------------------------------------------

class OutlineSectionModel(BaseModel):
    heading: str = Field(description="Section heading in your own words, sentence case")
    purpose: str = Field(default="", description="One sentence: what the reader gets from this section")
    keywords: list[str] = Field(default_factory=list, description="Approved keywords this section should use")
    points: list[str] = Field(default_factory=list, description="2 to 4 points the section makes")


class OutlineModel(BaseModel):
    title: str = Field(description="Post title under 70 characters that includes the main keyword")
    target_keyword: str = Field(default="", description="The main keyword, chosen from the approved keywords")
    sections: list[OutlineSectionModel] = Field(
        description="5 to 7 sections, starting with an introduction and ending with a conclusion")
    gaps_addressed: list[str] = Field(
        default_factory=list, description="The approved content gaps this outline covers, copied word for word")
    research_question: str = Field(
        default="", description="Empty, unless one specific fact the post needs is missing from the research")


class RevisionDecision(BaseModel):
    scope: str = Field(description='"sections", "draft" or "outline"')
    sections: list[int] = Field(default_factory=list,
                                description='For "sections": the numbers of the sections to change')


def _one_line(text, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _clean_terms(items, limit: int) -> list[str]:
    seen, out = set(), []
    for item in items or []:
        term = _one_line(item, 80)
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out[:limit]


def target_length(average_competitor_words: Optional[int]) -> tuple[int, int]:
    """Aim near what competitors publish, within 800-1,800 words."""
    if not average_competitor_words:
        return 800, 1200
    low = min(1400, max(800, int(round(average_competitor_words * 0.9, -2))))
    return low, low + 400


# ---------------------------------------------------------------------------
# The brief: everything the writer gets, taken from the approved research
# ---------------------------------------------------------------------------

@dataclass
class GenerationBrief:
    request: str
    topic: str
    company: dict
    primary_keyword: str
    secondary_keywords: list[str] = field(default_factory=list)
    keyword_notes: list[str] = field(default_factory=list)
    search_intent: Optional[str] = None
    structure: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    common_topics: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    competitor_headings: list[str] = field(default_factory=list)
    competitor_structures: list[str] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    research_summary: str = ""
    target_words: tuple[int, int] = (800, 1200)
    average_competitor_words: Optional[int] = None
    extra_research: list[dict] = field(default_factory=list)
    feedback: Optional[str] = None
    warnings: list[str] = field(default_factory=list)

    @property
    def approved_keywords(self) -> list[str]:
        return [self.primary_keyword, *self.secondary_keywords]

    @classmethod
    def from_state(cls, state: dict, feedback: Optional[str] = None) -> "GenerationBrief":
        """Build the brief from the approved research. Raises ResearchNotReady if there isn't any."""
        if not state.get("research_approved"):
            raise ResearchNotReady("The research hasn't been approved, so there's no approved strategy to write from.")
        output = state.get("research_output")
        if not isinstance(output, dict) or not output.get("topic"):
            raise ResearchNotReady("The approved research is missing, so there's nothing to write from. "
                                   "Send the request again to start new research.")
        company = state.get("company_context") or {}
        if not company.get("name"):
            raise ResearchNotReady("The company context is missing. Complete onboarding, then send the request again.")

        warnings = []
        strategy = output.get("seo_strategy") or {}
        approved = _clean_terms(strategy.get("target_keywords"), 8) or \
            _clean_terms([k["term"] for k in output.get("keywords", [])], 6)
        if not approved:
            approved = [output["topic"].lower()]
            warnings.append("No keywords were approved, so the topic is used as the main keyword.")
        # After the outline step the state says which approved keyword the post targets.
        primary = state.get("target_keyword") if state.get("target_keyword") in approved else approved[0]
        secondary = [k for k in approved if k != primary][:7]

        sources = output.get("sources") or []
        if not sources:
            warnings.append("No competitor pages were read, so the post relies on the company context.")
        counts = [s["word_count"] for s in sources if s.get("word_count")]
        average = round(sum(counts) / len(counts)) if counts else None

        details = {k["term"].lower(): k for k in output.get("keywords", [])}
        notes = []
        for term in approved:
            k = details.get(term.lower())
            if k:
                notes.append(f"{term}: used by {k['documents']} of {len(sources)} sources, "
                             f"{k['difficulty']}, {k['intent']}")

        # The approved competitor analysis (older runs kept it outside research_output).
        analysis = output.get("competitor_analysis") or state.get("competitor_analysis") or {}
        structures, headings = [], []
        for s in analysis.get("structure_by_source", [])[:5]:
            structures.append(f"{s['domain']}: {s.get('h2', 0)} sections, {s.get('word_count', 0):,} words")
            headings.extend(s.get("headings", [])[:10])

        return cls(
            request=state.get("user_request", ""),
            topic=output["topic"],
            company=company,
            primary_keyword=primary,
            secondary_keywords=secondary,
            keyword_notes=notes,
            search_intent=output.get("search_intent"),
            structure=list(output.get("recommended_structure") or []),
            gaps=list(output.get("content_gaps") or []),
            common_topics=list(output.get("common_topics") or [])[:8],
            questions=list(analysis.get("questions_covered") or [])[:6],
            competitor_headings=list(dict.fromkeys(headings))[:30],
            competitor_structures=structures,
            sources=sources,
            research_summary=output.get("research_summary") or "",
            target_words=target_length(average),
            average_competitor_words=average,
            extra_research=list(state.get("extra_research") or []),
            feedback=feedback,
            warnings=warnings,
        )

    # -- prompt text --------------------------------------------------------------

    def _company_lines(self) -> list[str]:
        c = self.company
        lines = [f"Company: {c.get('name')}: {c.get('description') or ''}".strip()]
        if c.get("industry"):
            lines.append(f"Industry: {c['industry']}")
        lines.append(f"Audience: {', '.join(c.get('target_audience', [])) or 'not given'}")
        if c.get("products"):
            lines.append(f"Products: {', '.join(c['products'])}")
        return lines

    def research_notes(self) -> str:
        """The approved research, as data. It was derived from web pages, so it's escaped."""
        notes = [
            f"Research summary: {self.research_summary}" if self.research_summary else "",
            f"Search intent: {self.search_intent}" if self.search_intent else "",
            f"Main keyword: {self.primary_keyword}",
            f"Other approved keywords: {', '.join(self.secondary_keywords)}" if self.secondary_keywords else "",
            ("Keyword notes (heuristic, from competitor content): " + "; ".join(self.keyword_notes))
            if self.keyword_notes else "",
            f"Approved structure: {' | '.join(self.structure)}" if self.structure else "",
            f"Content gaps to cover (competitors miss these): {'; '.join(self.gaps)}" if self.gaps else "",
            f"Topics most competitors cover: {', '.join(self.common_topics)}" if self.common_topics else "",
            f"Questions competitors answer: {'; '.join(self.questions)}" if self.questions else "",
            f"Competitor structures: {'; '.join(self.competitor_structures)}" if self.competitor_structures else "",
            (f"Competitor headings (reference only, do not reuse them): {' | '.join(self.competitor_headings)}"
             if self.competitor_headings else ""),
            ("Sources analyzed: " + "; ".join(f"{s.get('title', '')} ({s.get('domain', '')})" for s in self.sources[:6]))
            if self.sources else "Sources analyzed: none (no competitor pages could be read)",
        ]
        return "<research_notes>\n" + escape("\n".join(n for n in notes if n)) + "\n</research_notes>"

    def describe(self) -> str:
        lines = [f"Request: {escape(self.request)}", f"Topic: {escape(self.topic)}", *self._company_lines()]
        average = (f" (competitor articles average {self.average_competitor_words:,} words)"
                   if self.average_competitor_words else "")
        lines.append(f"Length: {self.target_words[0]:,} to {self.target_words[1]:,} words{average}.")
        lines.append(self.research_notes())
        if self.extra_research:
            lines.append("Extra lookup (search snippets; use only to check a fact, never copy):\n"
                         + "\n".join(as_search_result(i, r) for i, r in enumerate(self.extra_research)))
        if self.feedback:
            lines.append(f"The user asked for these changes to the previous draft (they take priority "
                         f"over the length guide): {escape(self.feedback)}")
        return "\n".join(lines)

    def describe_short(self) -> str:
        return "\n".join([*self._company_lines(), f"Main keyword: {escape(self.primary_keyword)}",
                          f"Search intent: {escape(self.search_intent or 'not given')}"])

    def known_text(self) -> str:
        """Everything the writer was given. A figure in the draft that isn't in here is unsupported."""
        c = self.company
        return "\n".join([self.request, self.topic, str(c.get("description", "")), str(c.get("name", "")),
                          " ".join(c.get("products", [])), " ".join(c.get("target_audience", [])),
                          self.research_notes(), self.feedback or "",
                          " ".join(f"{r.get('title', '')} {r.get('snippet', '')}" for r in self.extra_research)])


# ---------------------------------------------------------------------------
# Checks the agent runs on its own work (plain code, so they're testable)
# ---------------------------------------------------------------------------

def _content_words(text: str) -> set[str]:
    return {w for w in tokens(text) if w not in STOPWORDS}


def matches(claim: str, target: str, overlap: float = 0.6) -> bool:
    """Loose match: same heading key, or most of the target's words appear in the claim."""
    if heading_key(claim) == heading_key(target):
        return True
    wanted = _content_words(target)
    return bool(wanted) and len(wanted & _content_words(claim)) >= overlap * len(wanted)


def copied_headings(outline: dict, competitor_headings: list[str]) -> list[str]:
    theirs = {heading_key(h) for h in competitor_headings}
    return [s["heading"] for s in outline["sections"]
            if heading_key(s["heading"]) in theirs and heading_key(s["heading"]) not in GENERIC_HEADINGS]


def structure_followed(outline: dict, structure: list[str]) -> int:
    """How many items of the approved structure the outline's headings reflect."""
    headings = [s["heading"] for s in outline["sections"]]
    return sum(1 for item in structure if any(matches(h, item, 0.5) for h in headings))


def review_outline(outline: dict, brief: GenerationBrief) -> list[str]:
    """Problems with an outline, as instructions the model can act on. Empty = good to go."""
    problems = []
    copied = copied_headings(outline, brief.competitor_headings)
    if len(copied) >= 2:
        problems.append("These headings copy competitor headings word for word; write your own: " + "; ".join(copied))
    if not count_term(outline["target_keyword"], outline["title"]):
        problems.append(f'The title should include the main keyword "{outline["target_keyword"]}".')
    if brief.gaps and not outline["gaps_addressed"]:
        problems.append("The outline should cover at least one approved content gap: " + "; ".join(brief.gaps))
    if brief.structure and structure_followed(outline, brief.structure) < len(brief.structure) / 2:
        problems.append("Follow the approved structure more closely (in your own words): " + " | ".join(brief.structure))
    return problems


def rule_revision_scope(feedback: str, parts: list[dict]) -> tuple[str, list[int]]:
    """Fallback when the model's revision decision can't be used. Transparent keyword rules."""
    text = feedback.lower()
    if re.search(r"\b(add|remove|drop|delete|new|another|extra|merge)\b.{0,25}\b(section|part|heading|chapter)s?\b"
                 r"|\b(outline|structure|reorder|restructure|title|headline|angle)\b", text):
        return "outline", []
    if not parts:
        return "draft", []
    ids = set()
    if re.search(r"\b(intro|introduction|opening|first paragraph|beginning)\b", text):
        ids.add(0)
    if re.search(r"\b(conclusion|ending|outro|closing|last section|final section)\b", text):
        ids.add(len(parts) - 1)
    if not ids and not re.search(r"\b(whole|entire|overall|everything|throughout|all sections)\b", text):
        said = _content_words(text)
        for i, part in enumerate(parts):
            words = _content_words(part["heading"] or "")
            if words and len(words & said) >= max(1, round(len(words) * 0.6)):
                ids.add(i)
    return ("sections", sorted(ids)) if ids else ("draft", [])


def _strip_wrapping(markdown: str) -> str:
    """Remove what models sometimes add around a post: code fences, a preface, the title, our tags."""
    text = re.sub(r"<think>.*?</think>", "", markdown or "", flags=re.DOTALL).strip()   # reasoning some models inline
    fenced = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*?)\n```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    text = re.sub(r"\A(?:here(?:'s| is)[^\n]{0,80}:)\s*\n+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:post|section)>", "", text).strip()
    return re.sub(r"\A\s*#\s+[^\n]+\n+", "", text).strip()      # the title lives in its own field


def _section_label(part: dict) -> str:
    return part["heading"] or "Introduction"


# ---------------------------------------------------------------------------
# The agent
# ---------------------------------------------------------------------------

class GenerationAgent:
    name = "generation"
    tools = ("google_search", "seo_analysis")

    def __init__(self, llm, settings: Optional[Settings] = None):
        self.llm = llm
        self.settings = settings

    @property
    def can_search(self) -> bool:
        return self.settings is not None and google_search.is_configured(self.settings)

    # -- tools ------------------------------------------------------------------

    def look_up(self, question: str) -> ToolResult:
        """Limited extra research: one search, snippets only. The Analysis Agent does the real research."""
        return google_search.search(question, self.settings, max_results=4)

    @staticmethod
    def measure(brief: GenerationBrief, title: str, markdown: str, pages: list[dict]) -> ToolResult:
        return seo_analysis.analyze(title, markdown, brief.primary_keyword, brief.secondary_keywords,
                                    brief.common_topics, pages, brief.known_text(), brief.target_words)

    # -- model calls ------------------------------------------------------------

    def _ask(self, schema, prompt: str):
        return ask_structured(self.llm, schema, [SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)])

    def _write(self, prompt: str) -> str:
        response = self.llm.invoke([SystemMessage(SYSTEM_PROMPT), HumanMessage(prompt)])
        content = getattr(response, "content", response)
        if isinstance(content, list):
            content = "".join(part.get("text", "") if isinstance(part, dict) else str(part) for part in content)
        return _strip_wrapping(str(content))

    # -- decisions --------------------------------------------------------------

    def build_outline(self, brief: GenerationBrief) -> dict:
        prompt = (
            f"{brief.describe()}\n\n"
            "Create the outline for this post.\n"
            f'- title: under 70 characters, including the main keyword "{escape(brief.primary_keyword)}".\n'
            f'- target_keyword: "{escape(brief.primary_keyword)}", unless another approved keyword fits the '
            "request clearly better.\n"
            "- sections: 5 to 7. Use the approved structure as the plan, but write every heading in your own "
            "words. For each section give a purpose, the approved keywords it should use, and 2 to 4 points.\n"
            "- Start with an introduction and end with a conclusion or next steps.\n"
            "- Cover the approved content gaps, and list the ones you cover in gaps_addressed.\n"
            "- research_question: leave it empty unless one specific fact the post needs is missing."
        )
        outline = self._clean_outline(self._ask(OutlineModel, prompt), brief)
        problems = review_outline(outline, brief)
        if problems:                                    # the agent fixes its own outline, once
            retry = prompt + "\n\nYour previous outline had these problems. Fix them:\n" + \
                "\n".join(f"- {escape(p)}" for p in problems)
            outline = self._clean_outline(self._ask(OutlineModel, retry), brief)
            outline["notes"] = review_outline(outline, brief)
            outline["revised_after_review"] = True
        return outline

    def _clean_outline(self, answer: OutlineModel, brief: GenerationBrief) -> dict:
        approved = {k.lower(): k for k in brief.approved_keywords}
        sections = []
        for s in answer.sections[:MAX_SECTIONS]:
            heading = _one_line(s.heading, 120).lstrip("#").strip()
            if heading:
                sections.append({
                    "heading": heading,
                    "purpose": _one_line(s.purpose, 240),
                    "keywords": list(dict.fromkeys(approved[k.strip().lower()] for k in s.keywords
                                                   if k.strip().lower() in approved))[:4],
                    "points": [_one_line(p, 200) for p in s.points if str(p).strip()][:5],
                })
        title = _one_line(answer.title, 120).strip('"').strip()
        if len(sections) < 3 or not title:
            raise GenerationProblem("The outline came back incomplete.")
        return {
            "title": title,
            # The strategy is binding: anything but an approved keyword falls back to the main one.
            "target_keyword": approved.get(answer.target_keyword.strip().lower(), brief.primary_keyword),
            "sections": sections,
            "gaps_addressed": [g for g in brief.gaps if any(matches(claim, g) for claim in answer.gaps_addressed)],
            "notes": [],
            "revised_after_review": False,
            "research_question": _one_line(answer.research_question, 200),
        }

    def write_blog(self, brief: GenerationBrief, outline: dict) -> str:
        # .get(): outlines saved by older versions of the app have only headings and points.
        plan = "\n\n".join(
            f"## {s['heading']}\nPurpose: {s.get('purpose') or 'not given'}\n"
            f"Keywords: {', '.join(s.get('keywords', [])) or 'none'}\n" + "\n".join(f"- {p}" for p in s.get("points", []))
            for s in outline["sections"]
        )
        low, high = brief.target_words
        markdown = self._write(
            f"{brief.describe()}\n\nTitle: {outline['title']}\n"
            f"Main keyword: {outline.get('target_keyword') or brief.primary_keyword}\n\n"
            f"Outline:\n{plan}\n\n"
            f"Write the full post in Markdown, {low:,} to {high:,} words.\n"
            "- Use the outline's headings as ## headings, in the same order. Don't repeat the title as a heading.\n"
            "- Use the main keyword in the first paragraph and keep keyword use natural.\n"
            "- Short paragraphs of 2 to 4 sentences; use a list where it helps the reader.\n"
            "- Return only the post."
        )
        if len(words_of(plain_text(markdown))) < MIN_DRAFT_WORDS:
            raise GenerationProblem("The draft came back too short.")
        return markdown

    def optimize(self, brief: GenerationBrief, markdown: str, fixes: list[str]) -> str:
        """One revision that fixes only the measured problems. The node keeps it only if it's better."""
        others = ", ".join(brief.secondary_keywords) or "none"
        return self._write(
            f"Main keyword: {escape(brief.primary_keyword)}\nOther approved keywords: {escape(others)}\n"
            f"Audience: {escape(', '.join(brief.company.get('target_audience', [])))}\n\n"
            "Improve this blog post for search and for readers by fixing only these problems:\n"
            + "\n".join(f"- {escape(f)}" for f in fixes) + "\n\n"
            "Rules:\n"
            "- Keep the same sections in the same order. Reword a heading only if a problem above asks for it.\n"
            "- Keep the meaning and the facts, and roughly the same length unless a problem asks for more.\n"
            "- Don't add statistics, figures, quotes or claims.\n"
            "- Use keywords only where they read naturally; never repeat one just to raise its count.\n"
            "Return only the revised post in Markdown, without the title.\n\n"
            f"<post>\n{markdown}\n</post>"
        )

    def plan_revision(self, feedback: Optional[str], markdown: str) -> dict:
        """Decide the smallest step to go back to: edit some sections, rewrite the draft, or redo the outline."""
        parts = split_sections(markdown)
        if not feedback:
            return {"scope": "draft", "section_ids": [], "sections": [],
                    "reason": "No specific change was requested, so the agent writes a fresh draft "
                              "from the approved outline."}
        listing = "\n".join(f"{i}. {'## ' + p['heading'] if p['heading'] else 'Introduction (text before the first heading)'}"
                            for i, p in enumerate(parts))
        decision = self._ask(RevisionDecision, (
            f'A user reviewed this blog draft and asked for a change: "{escape(feedback)}"\n\n'
            f"Sections of the draft:\n{escape(listing)}\n\n"
            "Choose the smallest change that does what the user asked:\n"
            '- "sections": the request is about specific sections (for example the introduction). List their numbers.\n'
            '- "draft": the request is about the whole post\'s wording, tone or length, keeping the same outline.\n'
            '- "outline": the request changes the structure, the title, the angle or what the post covers.'
        ))
        scope = (decision.scope or "").strip().lower().strip('"')
        ids = sorted({i for i in decision.sections if isinstance(i, int) and 0 <= i < len(parts)})
        how = "decided by the agent"
        if scope not in SCOPES or (scope == "sections" and not ids):
            scope, ids = rule_revision_scope(feedback, parts)
            how = "decided by rule"
        if scope == "sections" and len(ids) > min(MAX_REVISED_SECTIONS, max(1, int(len(parts) * 0.6))):
            scope, ids = "draft", []                     # most of the post: a rewrite is more honest
        names = [_section_label(parts[i]) for i in ids] if scope == "sections" else []
        quoted = ", ".join(f'"{n}"' for n in names)
        reason = {
            "sections": f"Only {quoted} {'needs' if len(names) == 1 else 'need'} to change, so the rest "
                        "of the draft stays as it is",
            "draft": "The change affects the whole post, so the agent rewrites the draft from the same outline",
            "outline": "The change affects the structure, so the agent rebuilds the outline and then the draft",
        }[scope]
        return {"scope": scope, "section_ids": ids if scope == "sections" else [], "sections": names,
                "reason": f"{reason} ({how})."}

    def revise_sections(self, brief: GenerationBrief, markdown: str, section_ids: list[int], feedback: str) -> str:
        """Rewrite only the chosen sections; every other section is kept exactly as it was."""
        parts = split_sections(markdown)
        for i in section_ids:
            part = parts[i]
            rewritten = self._write(
                f"{brief.describe_short()}\n\nThe full post, for context:\n<post>\n{markdown}\n</post>\n\n"
                f'The user asked: "{escape(feedback)}"\n\n'
                "Rewrite only this section of the post to do what the user asked:\n"
                f"<section>\n{part['markdown']}\n</section>\n\n"
                "Keep the heading line exactly as it is unless the request is about the heading. Keep the facts "
                "and don't add statistics. Return only the rewritten section in Markdown."
            )
            parts[i] = {"heading": part["heading"], "markdown": self._fit_section(rewritten, part)}
        return join_sections(parts)

    @staticmethod
    def _fit_section(rewritten: str, original: dict) -> str:
        """Make sure a rewritten section is still exactly one section, with its heading."""
        pieces = split_sections(rewritten)
        if original["heading"]:
            with_heading = [p for p in pieces if p["heading"]]
            text = with_heading[0]["markdown"] if with_heading else f"## {original['heading']}\n\n{rewritten.strip()}"
        elif not pieces:
            text = ""
        elif pieces[0]["heading"] is None:
            text = pieces[0]["markdown"]
        else:                                           # an intro with no heading: drop the one the model added
            text = "\n".join(pieces[0]["markdown"].splitlines()[1:]).strip()
        if len(words_of(plain_text(text))) < 10:
            raise GenerationProblem("A revised section came back empty.")
        return text
