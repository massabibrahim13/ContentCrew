"""Small text helpers shared by the tools (tokenizing, stopwords, phrases, Markdown sections)."""

from __future__ import annotations

import re
from typing import Iterable

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each even every few for from
further get gets got had has have having he her here hers him his how i if in into is it its itself
just let lets like make makes may me might more most much must my need new no nor not now of off
often on once one only or other our ours out over own per really same say says see she should so
some such than that the their them then there these they this those through to too under until up
upon us use used uses using very via want was way we well were what when where which while who whom
why will with within without would yet you your yours
ll re ve s t d m
""".split())

# Words that show up on almost every web page and say nothing about the topic.
WEB_NOISE = set("""
cookie cookies privacy policy terms login log sign signup subscribe newsletter menu home
contact copyright rights reserved click read learn share follow email www http https com blog
skip content main navigation search toggle cart account free trial demo request book
""".split())

_TOKEN = re.compile(r"[a-z][a-z0-9'\-]*[a-z0-9]|[a-z]")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def is_noise(word: str) -> bool:
    # Two-letter words are kept unless they're filler ("ai", "ux" and "b2b" matter in marketing).
    return word in STOPWORDS or word in WEB_NOISE or len(word) < 2 or word.isdigit()


def phrases(words: list[str], max_n: int = 3) -> Iterable[str]:
    """Yield 1-3 word phrases that don't start or end with a filler word."""
    for n in range(1, max_n + 1):
        for i in range(len(words) - n + 1):
            gram = words[i:i + n]
            if is_noise(gram[0]) or is_noise(gram[-1]):
                continue
            if n > 1 and any(w in WEB_NOISE for w in gram):
                continue
            yield " ".join(gram)


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Markdown sections (used by the Generation Agent and the SEO Analysis tool)
# ---------------------------------------------------------------------------

_H2_START = re.compile(r"^##[ \t]+\S", re.MULTILINE)      # "## Heading", not "### Subheading"


def split_sections(markdown: str) -> list[dict]:
    """
    Split a post into its parts: [{"heading": str | None, "markdown": str}, ...].
    Text before the first "## " heading (an introduction without a heading) has
    heading None. "###" subheadings stay inside their section.
    `join_sections(split_sections(md))` gives the same post back.
    """
    text = (markdown or "").strip()
    starts = [m.start() for m in _H2_START.finditer(text)]
    sections = []
    preamble = text[:starts[0]] if starts else text
    if preamble.strip():
        sections.append({"heading": None, "markdown": preamble.strip()})
    for i, start in enumerate(starts):
        chunk = text[start:starts[i + 1] if i + 1 < len(starts) else len(text)].strip()
        heading = chunk.splitlines()[0][2:].strip().strip("#").strip()
        sections.append({"heading": heading, "markdown": chunk})
    return sections


def join_sections(sections: list[dict]) -> str:
    return "\n\n".join(s["markdown"].strip() for s in sections if s["markdown"].strip())


def heading_key(text: str) -> str:
    """Compare headings ignoring case, punctuation and filler words."""
    return " ".join(w for w in tokens(text) if w not in STOPWORDS)
