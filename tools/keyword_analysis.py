"""
Keyword Analysis tool.

Two kinds of output, never mixed up:

REAL API METRICS (search volume, difficulty, competition)
    Only from a keyword data provider. They are all paid and this project is
    free, so none is connected: `metrics.status` is "not_configured" and no
    number is shown. `_METRIC_PROVIDERS` is where one would plug in.

HEURISTIC ANALYSIS (always available, always labelled)
    Computed from the competitor pages the agent actually read:
    - documents / mentions: real counts of how many sources use a term
    - difficulty: "Crowded / Contested / Open (heuristic)" from how many sources
      target the term, e.g. "Keyword difficulty: heuristic assessment"
    - intent: informational / commercial / transactional / navigational, from
      the words in the phrase (e.g. "how to", "best", "pricing")
    - related: other phrases from the same sources that share words with it
"""

from __future__ import annotations

import math
import re
from collections import Counter

from config import Settings
from tools import ToolResult
from tools.text import phrases, tokens

_METRIC_PROVIDERS: dict = {}     # name -> function(terms, settings) -> ToolResult; none are free

_INTENT_RULES = [
    ("transactional", re.compile(r"\b(buy|price|pricing|cost|discount|deal|order|subscribe|trial)\b")),
    ("commercial", re.compile(r"\b(best|top|vs|versus|review|reviews|compare|comparison|alternative|alternatives)\b")),
    ("informational", re.compile(r"\b(how|what|why|when|guide|tutorial|examples?|tips|ideas|explained|definition|meaning)\b")),
]


def heuristic_intent(term: str, brand_terms: set[str]) -> str:
    words = set(term.split())
    if words & brand_terms:
        return "navigational (heuristic)"
    for intent, pattern in _INTENT_RULES:
        if pattern.search(term):
            return f"{intent} (heuristic)"
    return "informational (heuristic)"


def heuristic_difficulty(documents: int, total_sources: int, in_headings: bool) -> str:
    """How crowded the term is among the sources read. A label, not a score."""
    share = documents / total_sources if total_sources else 0
    if share >= 0.67 and in_headings:
        return "Crowded (heuristic)"
    if share >= 0.34:
        return "Contested (heuristic)"
    return "Open (heuristic)"


def _metrics_status(settings: Settings) -> dict:
    return {"status": "not_configured", "provider": None,
            "message": "No search-volume or difficulty data: that needs a paid keyword data source.",
            "data": {}}


def analyze(topic: str, pages: list[dict], settings: Settings, limit: int = 15,
            brand_names: list[str] | None = None) -> ToolResult:
    """Rank terms competitor pages use. Returns {"keywords": [...], "metrics": {...}}."""
    metrics = _metrics_status(settings)
    if not pages:
        return ToolResult.empty("No competitor text to analyze.", {"keywords": [], "metrics": metrics})

    document_freq: Counter[str] = Counter()
    mentions: Counter[str] = Counter()
    in_headings: set[str] = set()
    for page in pages:
        heading_text = " ".join(h["text"] for h in page.get("headings", []))
        body = f"{page.get('title', '')}\n{heading_text}\n{page.get('text', '')}"
        page_counts = Counter(phrases(tokens(body)))
        mentions.update(page_counts)
        document_freq.update(page_counts.keys())
        in_headings.update(phrases(tokens(heading_text)))

    many_sources = len(pages) >= 2
    candidates = []
    for term, count in mentions.items():
        docs = document_freq[term]
        if (many_sources and docs < 2) or count < 2:
            continue
        words = term.count(" ") + 1
        score = docs * 3 + (2 if term in in_headings else 0) + math.log(count + 1) + (words - 1) * 0.8
        candidates.append((score, term))
    candidates.sort(reverse=True)

    chosen: list[str] = []
    for _, term in candidates:
        if " " not in term and any(term in c.split() for c in chosen):
            continue   # a stronger phrase already contains this word
        chosen.append(term)
        if len(chosen) >= limit:
            break
    topic_term = " ".join(tokens(topic))
    if topic_term and topic_term not in chosen:
        chosen = [topic_term] + chosen[:limit - 1]

    brand_terms = {w for name in (brand_names or []) for w in tokens(name)}
    multiword = [c for c in chosen if " " in c]
    keywords = []
    for term in chosen:
        words = set(term.split())
        related = [c for c in multiword if c != term and words & set(c.split())][:4]
        docs = document_freq.get(term, 0)
        keywords.append({
            "term": term,
            "documents": docs,
            "mentions": mentions.get(term, 0),
            "in_headings": term in in_headings,
            "difficulty": heuristic_difficulty(docs, len(pages), term in in_headings),
            "intent": heuristic_intent(term, brand_terms),
            "related": related,
            "source": "heuristic",
        })

    return ToolResult.success(
        f"Ranked {len(keywords)} keywords from {len(pages)} sources (heuristic; no search-volume data)",
        {"keywords": keywords, "metrics": metrics},
    )
