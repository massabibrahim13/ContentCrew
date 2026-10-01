"""
Google Search tool: find articles on the web for a query.

Google's own Custom Search JSON API is closed to new customers and shuts down on
1 January 2027, and scraping Google's result pages breaks its terms. So the tool
talks to a search API built for this, chosen in .env:

    SEARCH_PROVIDER=tavily      # https://tavily.com, free plan: 1,000 searches a month, no card
    SEARCH_API_KEY=tvly-...

    SEARCH_PROVIDER=serper      # https://serper.dev, Google results (free credits are one-time)

Without those settings the tool returns `not_configured` with a message the UI
shows as-is. It never invents results. Adding a provider means adding one
function to `_PROVIDERS`.

Each result: {"title", "url", "domain", "snippet", "query"}.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import requests

from config import Settings
from tools import ToolResult
from tools.text import normalize_space

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 12
TAVILY_URL = "https://api.tavily.com/search"
SERPER_URL = "https://google.serper.dev/search"


def domain_of(url: str) -> str:
    return (urlparse(url).hostname or "").removeprefix("www.")


def _result(title: str, url: str, snippet: str, query: str) -> Optional[dict]:
    if not url.startswith(("http://", "https://")) or not domain_of(url):
        return None
    return {
        "title": normalize_space(title)[:200] or url,
        "url": url,
        "domain": domain_of(url),
        "snippet": normalize_space(snippet)[:300],
        "query": query,
    }


def _http_problem(response) -> Optional[ToolResult]:
    if response.status_code in (401, 403):
        return ToolResult.error("The search API key was rejected. Check SEARCH_API_KEY in .env.")
    if response.status_code in (429, 432, 433):
        return ToolResult.error("The free search allowance is used up for now (rate limit or monthly credits).")
    if not response.ok:
        return ToolResult.error(f"Search failed (HTTP {response.status_code}).")
    return None


def _post(url: str, payload: dict, headers: dict):
    try:
        return requests.post(url, json=payload, headers=headers, timeout=TIMEOUT_SECONDS), None
    except requests.Timeout:
        return None, ToolResult.error("The search provider took too long to respond.")
    except requests.RequestException:
        return None, ToolResult.error("Couldn't reach the search provider. Check the internet connection.")


def _tavily(query: str, api_key: str, max_results: int, include_domains: list[str]) -> ToolResult:
    payload = {"query": query, "max_results": max_results, "search_depth": "basic"}   # basic = 1 credit
    if include_domains:
        payload["include_domains"] = include_domains
    response, failure = _post(TAVILY_URL, payload,
                              {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    if failure:
        return failure
    problem = _http_problem(response)
    if problem:
        return problem
    try:
        items = response.json().get("results", [])
    except ValueError:
        return ToolResult.error("The search provider sent a response that couldn't be read.")
    results = [r for r in (_result(i.get("title", ""), i.get("url") or "", i.get("content", ""), query)
                           for i in items[:max_results]) if r]
    return _finish(results, query)


def _serper(query: str, api_key: str, max_results: int, include_domains: list[str]) -> ToolResult:
    q = query
    if include_domains:
        q = f"{query} " + " OR ".join(f"site:{d}" for d in include_domains)
    response, failure = _post(SERPER_URL, {"q": q, "num": max_results},
                              {"X-API-KEY": api_key, "Content-Type": "application/json"})
    if failure:
        return failure
    problem = _http_problem(response)
    if problem:
        return problem
    try:
        items = response.json().get("organic", [])
    except ValueError:
        return ToolResult.error("The search provider sent a response that couldn't be read.")
    results = [r for r in (_result(i.get("title", ""), i.get("link") or "", i.get("snippet", ""), query)
                           for i in items[:max_results]) if r]
    return _finish(results, query)


def _finish(results: list[dict], query: str) -> ToolResult:
    if not results:
        return ToolResult.empty(f'No results for "{query}".', [])
    domains = len({r["domain"] for r in results})
    return ToolResult.success(f"{len(results)} results from {domains} site{'s' if domains != 1 else ''}", results)


_PROVIDERS = {"tavily": _tavily, "serper": _serper}


def is_configured(settings: Settings) -> bool:
    return settings.search_provider in _PROVIDERS and bool(settings.search_api_key)


def search(query: str, settings: Settings, max_results: int = 8,
           include_domains: Optional[list[str]] = None) -> ToolResult:
    """Search the web. `include_domains` limits results to those sites (e.g. competitors)."""
    query = normalize_space(query)[:200]
    if not query:
        return ToolResult.error("The search query was empty.")

    if not settings.search_provider or not settings.search_api_key:
        return ToolResult.not_configured(
            "Web search isn't set up. Add SEARCH_PROVIDER=tavily and a free SEARCH_API_KEY to .env."
        )
    provider = _PROVIDERS.get(settings.search_provider)
    if provider is None:
        return ToolResult.not_configured(
            f"SEARCH_PROVIDER '{settings.search_provider}' isn't supported. "
            f"Supported: {', '.join(sorted(_PROVIDERS))}."
        )

    domains = [d for d in (include_domains or []) if d][:10]
    result = provider(query, settings.search_api_key, max(1, min(max_results, 10)), domains)
    if result.status == "error":
        log.warning("Search failed for %r: %s", query, result.message)
    return result
