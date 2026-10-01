"""
Competitor Analysis tool: compare what competitor pages cover.

Deterministic and transparent, so every number can be traced back to the pages:

    common_themes        topics (heading terms) that several sources share
    common_headings      real headings that cover the most shared themes
    questions_covered    questions the sources answer in their headings
    length               shortest / average / longest article, in words
    structure_by_source  how each source is organised (H2/H3 counts, length)
    thin_topics          themes only one source covers: possible gaps (heuristic)

It records structure and topics only. It never copies competitor text into the
output beyond headings, which are short labels used for comparison. Deciding
what's actually *missing* needs judgement, so the Analysis Agent does that with
the model and labels it as analysis.
"""

from __future__ import annotations

from collections import Counter

from tools import ToolResult
from tools.text import phrases, tokens

# Verbs that join heading words without being a topic ("How AI *changes* product content").
HEADING_VERBS = set("""
changes change matters matter explained explains works work working makes make made get gets getting
started start choose choosing need needs helps help using use used build building improve improves
""".split())

EMPTY = {
    "pages_analyzed": 0, "average_word_count": 0,
    "length": {"shortest": 0, "average": 0, "longest": 0},
    "common_themes": [], "common_headings": [], "questions_covered": [], "thin_topics": [],
    "structure_by_source": [], "representative_headings": [],
}


def _themes(theme_sources: Counter, needed: int, limit: int) -> list[tuple[str, int]]:
    """Phrases first (more specific), then single words not already inside a chosen phrase."""
    shared = [(theme, count) for theme, count in theme_sources.items() if count >= needed]
    shared.sort(key=lambda item: (-item[1], -item[0].count(" "), item[0]))
    chosen: list[tuple[str, int]] = []
    for theme, count in [s for s in shared if " " in s[0]] + [s for s in shared if " " not in s[0]]:
        if " " not in theme and any(theme in c.split() for c, _ in chosen):
            continue
        chosen.append((theme, count))
    chosen.sort(key=lambda item: (-item[1], -item[0].count(" ")))
    return chosen[:limit]


def compare(pages: list[dict], max_themes: int = 10) -> ToolResult:
    if not pages:
        return ToolResult.empty("No competitor pages to compare.", dict(EMPTY))

    theme_sources: Counter[str] = Counter()
    structure, questions, seen_questions = [], [], set()
    for page in pages:
        headings = page.get("headings", [])
        section_headings = [h["text"] for h in headings if h.get("level", 2) in (2, 3)][:15]
        structure.append({
            "domain": page["domain"],
            "title": page["title"],
            "word_count": page.get("word_count", 0),
            "h2": sum(1 for h in headings if h.get("level") == 2),
            "h3": sum(1 for h in headings if h.get("level") == 3),
            "headings": section_headings,
        })
        terms = set()
        for heading in section_headings:
            terms.update(p for p in phrases(tokens(heading), max_n=2) if not set(p.split()) & HEADING_VERBS)
            if heading.endswith("?") and heading.lower() not in seen_questions:
                seen_questions.add(heading.lower())
                questions.append(heading)
        theme_sources.update(terms)

    needed = 2 if len(pages) >= 2 else 1
    themes = [{"theme": t, "sources": n} for t, n in _themes(theme_sources, needed, max_themes)]
    thin = [t for t, _ in _themes(theme_sources, 1, 40) if theme_sources[t] == 1 and " " in t][:8] \
        if len(pages) >= 2 else []

    # Real headings that touch the most shared themes: a structure grounded in sources.
    weight = {t["theme"]: t["sources"] for t in themes}
    scored, seen = [], set()
    for source in structure:
        for heading in source["headings"]:
            key = heading.lower()
            if key in seen:
                continue
            seen.add(key)
            score = sum(weight.get(term, 0) for term in set(phrases(tokens(heading), max_n=2)))
            if score:
                scored.append((score, heading))
    scored.sort(key=lambda item: -item[0])
    common_headings = [heading for _, heading in scored[:6]]

    counts = [p.get("word_count", 0) for p in pages]
    average = round(sum(counts) / len(counts))
    data = {
        "pages_analyzed": len(pages),
        "average_word_count": average,
        "length": {"shortest": min(counts), "average": average, "longest": max(counts)},
        "common_themes": themes,
        "common_headings": common_headings,
        "questions_covered": questions[:8],
        "thin_topics": thin,
        "structure_by_source": structure,
        "representative_headings": common_headings,
    }
    return ToolResult.success(
        f"Compared {len(pages)} pages: {len(themes)} shared themes, {len(questions)} questions answered, "
        f"{min(counts)}-{max(counts)} words",
        data,
    )
