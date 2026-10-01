"""
SEO Analysis tool: measure a draft against the approved keyword strategy.

Everything here is counted from the text, so every number can be checked by
hand. Nothing is a search-volume or ranking estimate:

    keyword placement    is the main keyword in the title, the introduction, a heading?
    keyword use          how often each approved keyword appears (per 100 words)
    keyword stuffing     a keyword used so often it reads unnaturally
    structure            word count and section headings
    readability          sentence and paragraph length, plus a Flesch reading-ease
                         score (approximate: syllables are counted by a rule)
    topical coverage     which topics most competitors cover the post also mentions
    originality          8-word phrases that match a competitor page word for word
    unsupported figures  percentages, amounts and years that appear nowhere in the
                         company context or the research, so a human should check them

The thresholds are common editorial guidelines, not search-engine rules.

`issues` explains each problem in plain words for the human. `fixable` lists
the ones the Generation Agent can fix by revising the text, phrased as
instructions; the optimize step sends those (and only those) to the model.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from tools import ToolResult
from tools.text import split_sections

STUFFING_PER_100 = 2.5        # main keyword more often than this per 100 words reads as stuffing
SECONDARY_STUFFING_PER_100 = 1.5
INTRO_WORDS = 120             # "the introduction" = the first 120 words of the body
TARGET_SENTENCE_WORDS = 22    # average sentence length above this reads as heavy
LONG_SENTENCE_WORDS = 30
LONG_PARAGRAPH_WORDS = 150
SHARED_PHRASE_WORDS = 8       # this many words in a row matching a competitor page = copied

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")
_FIGURE = re.compile(
    r"[$€£]\s?\d[\d,.]*(?:\s?(?:million|billion|thousand|[mbk])\b)?"
    r"|\b\d[\d,.]*\s?(?:%|percent\b|per cent\b|million\b|billion\b|thousand\b|times\b)"
    r"|\b(?:19|20)\d{2}\b"
    r"|\b\d{1,3}(?:,\d{3})+\b|\b\d{4,}\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def plain_text(markdown: str) -> str:
    """Markdown to readable text: no heading marks, list bullets, emphasis or link URLs."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", markdown or "")
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"[*_`>]", "", text)


def words_of(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def count_term(term: str, text: str) -> int:
    """Whole-phrase matches, ignoring case ("ai" doesn't match inside "paid")."""
    if not term.strip():
        return 0
    pattern = rf"(?<![a-z0-9]){re.escape(term.lower().strip())}(?![a-z0-9])"
    return len(re.findall(pattern, (text or "").lower()))


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(words_of(s)) >= 3]


def _syllables(word: str) -> int:
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def reading_ease(text: str) -> Optional[float]:
    """Flesch reading ease (0-100, higher is easier). Approximate: syllables come from a rule."""
    sentences = _sentences(text)
    words = words_of(text)
    if not sentences or len(words) < 30:
        return None
    syllables = sum(_syllables(w) for w in words)
    score = 206.835 - 1.015 * (len(words) / len(sentences)) - 84.6 * (syllables / len(words))
    return round(max(0.0, min(100.0, score)), 1)


def reading_ease_label(score: Optional[float]) -> str:
    if score is None:
        return "not enough text to score"
    for floor, label in ((70, "easy"), (60, "plain English"), (50, "fairly difficult"),
                         (30, "difficult"), (0, "very difficult")):
        if score >= floor:
            return label
    return "very difficult"


def _paragraphs(markdown: str) -> list[str]:
    """Prose paragraphs only: no headings, and lists don't count as one long paragraph."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", markdown or "") if b.strip()]
    return [b for b in blocks if not b.startswith("#") and not re.match(r"^\s*(?:[-*+]|\d+[.)])\s", b)]


def shared_phrases(draft_words: list[str], source_words: list[str], n: int = SHARED_PHRASE_WORDS) -> list[str]:
    """Runs of at least `n` words that appear, in order, in both texts (merged when they overlap)."""
    if len(draft_words) < n or len(source_words) < n:
        return []
    source = {tuple(source_words[i:i + n]) for i in range(len(source_words) - n + 1)}
    hits = [i for i in range(len(draft_words) - n + 1) if tuple(draft_words[i:i + n]) in source]
    runs, start, end = [], None, None
    for i in hits:
        if start is None:
            start, end = i, i + n
        elif i <= end:
            end = i + n
        else:
            runs.append((start, end))
            start, end = i, i + n
    if start is not None:
        runs.append((start, end))
    return [" ".join(draft_words[s:e]) for s, e in runs]


def unsupported_figures(text: str, known_text: str) -> list[str]:
    """Figures in the draft whose number appears nowhere in what the agent was given."""
    known_numbers = set(re.findall(r"\d+(?:\.\d+)?", (known_text or "").replace(",", "")))
    found, seen = [], set()
    for match in _FIGURE.finditer(text or ""):
        figure = match.group(0).strip()
        numbers = re.findall(r"\d+(?:\.\d+)?", figure.replace(",", ""))
        if numbers and not any(n in known_numbers for n in numbers) and figure.lower() not in seen:
            seen.add(figure.lower())
            found.append(figure)
    return found[:8]


# ---------------------------------------------------------------------------
# The tool
# ---------------------------------------------------------------------------

def _issue(issues: list, code: str, message: str, fix: Optional[str] = None) -> None:
    issues.append({"code": code, "message": message, "fix": fix})


def analyze(title: str, markdown: str, primary: str, secondary: Iterable[str] = (),
            topics: Iterable[str] = (), sources: Iterable[dict] = (), known_text: str = "",
            target_words: tuple[int, int] = (800, 1200)) -> ToolResult:
    """
    title, markdown     the draft (title kept separately from the Markdown body)
    primary, secondary  the approved keywords
    topics              topics several competitors cover (from the approved research)
    sources             competitor pages read during research ({"domain", "text"})
    known_text          everything the agent was given (company context, research notes,
                        the user's feedback): a figure not in here is "unsupported"
    """
    if not (markdown or "").strip():
        return ToolResult.empty("There's no draft to analyze.", {"issues": [], "fixable": []})

    primary = (primary or "").strip()
    secondary = [k for k in dict.fromkeys(s.strip() for s in secondary) if k and k.lower() != primary.lower()]
    body = plain_text(markdown)
    body_words = words_of(body)
    word_count = len(body_words)
    full_text = f"{title}\n{body}"
    sections = [s for s in split_sections(markdown) if s["heading"]]
    headings = [s["heading"] for s in sections]
    intro = " ".join(body_words[:INTRO_WORDS])
    per_100 = (lambda n: round(n * 100 / word_count, 2)) if word_count else (lambda n: 0.0)

    primary_count = count_term(primary, full_text)
    primary_info = {
        "term": primary, "count": primary_count, "per_100_words": per_100(primary_count),
        "in_title": count_term(primary, title) > 0,
        "in_introduction": count_term(primary, intro) > 0,
        "in_headings": sum(1 for h in headings if count_term(primary, h)),
    }
    secondary_info = [{"term": k, "count": count_term(k, full_text)} for k in secondary]
    for item in secondary_info:
        item["found"] = item["count"] > 0
    stuffed = ([primary] if primary and primary_info["per_100_words"] > STUFFING_PER_100 else []) + \
              [k["term"] for k in secondary_info if per_100(k["count"]) > SECONDARY_STUFFING_PER_100]

    sentences = _sentences(body)
    sentence_lengths = [len(words_of(s)) for s in sentences]
    paragraphs = _paragraphs(markdown)
    score = reading_ease(body)
    readability = {
        "average_sentence_words": round(sum(sentence_lengths) / len(sentence_lengths), 1) if sentence_lengths else 0,
        "long_sentences": sum(1 for n in sentence_lengths if n > LONG_SENTENCE_WORDS),
        "long_paragraphs": sum(1 for p in paragraphs if len(words_of(p)) > LONG_PARAGRAPH_WORDS),
        "reading_ease": score,
        "reading_ease_label": reading_ease_label(score),
        "note": "Reading ease is approximate (Flesch formula, syllables counted by a rule).",
    }

    topics = [t for t in dict.fromkeys(topics) if t]
    topics_covered = [t for t in topics if count_term(t, full_text)]
    topics_missing = [t for t in topics if t not in topics_covered]

    copied = []
    for page in sources:
        for phrase in shared_phrases(body_words, words_of(page.get("text", ""))):
            copied.append({"domain": page.get("domain", ""), "phrase": " ".join(phrase.split()[:16])})
    figures = unsupported_figures(body, known_text)

    # -- issues, most important first --------------------------------------------
    issues: list[dict] = []
    if primary:
        if not primary_info["in_title"]:
            _issue(issues, "title_keyword", f'The title doesn\'t include the main keyword "{primary}".')
        if not primary_info["in_introduction"]:
            _issue(issues, "intro_keyword", f'The main keyword "{primary}" isn\'t in the introduction.',
                   f'Use the main keyword "{primary}" naturally in the first paragraph.')
        if headings and not primary_info["in_headings"]:
            _issue(issues, "heading_keyword", f'No section heading uses the main keyword "{primary}".',
                   f'Reword one section heading to include "{primary}" where it fits naturally.')
    for term in stuffed:
        n = primary_count if term == primary else next(k["count"] for k in secondary_info if k["term"] == term)
        _issue(issues, "stuffing", f'"{term}" appears {n} times ({per_100(n)} per 100 words), which reads as keyword stuffing.',
               f'"{term}" appears {n} times. Replace some uses with natural wording.')
    missing = [k["term"] for k in secondary_info if not k["found"]]
    if missing:
        _issue(issues, "missing_keywords", f"Approved keywords not used: {', '.join(missing)}.",
               f"Where they fit naturally, use: {', '.join(missing)}. Skip any that don't fit.")
    if figures:
        _issue(issues, "unsupported_figures",
               f"Figures not found in the research or company context: {', '.join(figures)}. Check or remove them.",
               f"Remove these figures or make the point without numbers, because the research doesn't support "
               f"them: {', '.join(figures)}.")
    if copied:
        _issue(issues, "shared_phrases",
               f"{len(copied)} phrase{'s' if len(copied) != 1 else ''} match competitor pages word for word.",
               "Reword these phrases, which match competitor pages word for word: "
               + "; ".join(f'"{c["phrase"]}"' for c in copied[:4]) + ".")
    if word_count < target_words[0] * 0.85:
        _issue(issues, "too_short", f"The post is {word_count:,} words; the target is {target_words[0]:,} to {target_words[1]:,}.",
               f"Expand the thinnest sections with useful explanation or examples (no new statistics) so the post "
               f"reaches about {target_words[0]:,} words.")
    elif word_count > target_words[1] * 1.3:
        _issue(issues, "too_long", f"The post is {word_count:,} words; the target is {target_words[0]:,} to {target_words[1]:,}.")
    if readability["average_sentence_words"] > TARGET_SENTENCE_WORDS:
        _issue(issues, "long_sentences", f"Sentences average {readability['average_sentence_words']} words.",
               f"Shorten long sentences; they average {readability['average_sentence_words']} words.")
    if readability["long_paragraphs"]:
        n = readability["long_paragraphs"]
        _issue(issues, "long_paragraphs", f"{n} paragraph{'s are' if n != 1 else ' is'} over {LONG_PARAGRAPH_WORDS} words.",
               f"Split paragraphs longer than {LONG_PARAGRAPH_WORDS} words.")
    if topics and len(topics_missing) > len(topics) / 2:
        _issue(issues, "topics_missing", f"Topics most competitors cover that the post doesn't mention: {', '.join(topics_missing[:4])}.",
               f"If they're relevant to the post, briefly cover: {', '.join(topics_missing[:3])}.")

    found = [primary] * bool(primary_count) + [k["term"] for k in secondary_info if k["found"]]
    data = {
        "word_count": word_count,
        "sections": len(sections),
        "headings": headings,
        "primary_keyword": primary_info,
        "secondary_keywords": secondary_info,
        "keywords_found": found,
        "keywords_missing": ([primary] if primary and not primary_count else []) + missing,
        "title_has_keyword": primary_info["in_title"],
        "stuffed_keywords": stuffed,
        "readability": readability,
        "topics_covered": topics_covered,
        "topics_missing": topics_missing,
        "originality": {"shared_phrases": len(copied), "examples": copied[:3],
                        "note": f"Runs of {SHARED_PHRASE_WORDS}+ words that match a competitor page."},
        "unsupported_figures": figures,
        "issues": [i["message"] for i in issues],
        "issue_codes": [i["code"] for i in issues],
        "fixable": [i["fix"] for i in issues if i["fix"]],
    }
    ease = f"reading ease {score:g} (approximate)" if score is not None else "too short to score readability"
    return ToolResult.success(
        f"{word_count:,} words, {len(sections)} sections, main keyword used {primary_count} times, {ease}, "
        f"{len(issues)} issue{'s' if len(issues) != 1 else ''}",
        data,
    )
