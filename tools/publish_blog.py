"""
Publish Blog tool: the workflow's one external action, made over HTTP.

It sends POST /blog with {session_id, blog_id}, exactly as any outside client
would, and never touches the database itself. The blog API decides
(services/publishing.py): the workflow must be at its publish step, the human's
approval must be recorded, and the draft must be the current one with content.

By default the address is ContentCrew's own blog, http://127.0.0.1:<port>/blog.
Set BLOG_API_URL to publish somewhere else (a service that accepts the same body).

Outcomes, so the workflow can react sensibly:
    ok                     published (or it already was: a retry after a lost response)
    error, retryable       the API couldn't be reached or had a server error: Retry later
    error, not retryable   the API refused (no approval, wrong state, old draft...)
"""

from __future__ import annotations

from urllib.parse import urlparse

import requests

from config import Settings
from tools import ToolResult

TIMEOUT = (5, 20)          # seconds to connect, seconds to answer


def endpoint(settings: Settings) -> str:
    if settings.blog_api_url:
        return settings.blog_api_url
    host = settings.host if settings.host not in ("", "0.0.0.0", "::") else "127.0.0.1"
    return f"http://{host}:{settings.port}/blog"


def http_post(url: str, payload: dict, timeout) -> requests.Response:
    """The transport. Tests swap this for Flask's test client."""
    with requests.Session() as session:
        # A call to this machine must not go through a proxy configured for the internet.
        session.trust_env = urlparse(url).hostname not in ("127.0.0.1", "localhost", "::1")
        return session.post(url, json=payload, timeout=timeout, headers={"Accept": "application/json"})


def _failed(message: str, retryable: bool) -> ToolResult:
    return ToolResult("error", message, {"retryable": retryable})


def publish(settings: Settings, session_id: str, blog_id: str) -> ToolResult:
    url = endpoint(settings)
    try:
        response = http_post(url, {"session_id": session_id, "blog_id": blog_id}, TIMEOUT)
    except requests.Timeout:
        return _failed("The blog API didn't answer in time.", retryable=True)
    except requests.RequestException:
        return _failed("Couldn't reach the blog API.", retryable=True)

    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}

    if response.status_code in (200, 201) and isinstance(body.get("blog"), dict):
        blog = body["blog"]
        return ToolResult.success(f"POST /blog returned {response.status_code}: published at /blog/{blog['slug']}",
                                  blog)
    if response.status_code == 409 and error.get("code") == "already_published":
        return ToolResult.success("Already published (an earlier attempt went through)", {"already_published": True})
    if response.status_code >= 500 or response.status_code in (408, 429):
        return _failed(f"The blog API had a temporary problem (HTTP {response.status_code}).", retryable=True)
    reason = error.get("message") or f"HTTP {response.status_code}"
    return _failed(f"The blog API refused to publish: {reason}", retryable=False)
