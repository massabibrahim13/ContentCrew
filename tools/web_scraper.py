"""
Web Scraper tool: read the main text and headings of a public web page.

Returns a ScrapedPage: {url, domain, title, description, headings, text, word_count}.
Failures come back as a ToolResult with a plain message: invalid URL, timeout,
HTTP error, blocked page (access denied, rate limit, bot check), robots.txt, or
an empty page.

Safety rules, because the agent decides which URLs to open:
- Only http(s) URLs.
- Never private, loopback or link-local addresses (blocks requests to your own
  network, a common attack when an AI agent can fetch URLs). Redirects are
  followed by hand so each hop is checked too.
- Respects robots.txt for the ContentCrew user agent.
- Time and size limits on every request.

The returned text is untrusted data. Agents must treat it as material to
analyze, never as instructions (see the prompts in agents/).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from config import Settings
from tools import ToolResult
from tools.text import normalize_space

log = logging.getLogger(__name__)

USER_AGENT = "ContentCrewBot/0.1 (classroom research tool; respects robots.txt)"
TIMEOUT = (5, 10)            # connect, read seconds
MAX_BYTES = 2_000_000
MAX_TEXT_CHARS = 12_000
MAX_REDIRECTS = 4
REMOVE_TAGS = ["script", "style", "noscript", "template", "svg", "iframe", "form", "button",
               "nav", "footer", "header", "aside"]

_robots_cache: dict[str, RobotFileParser | None] = {}
_robots_lock = threading.Lock()


class _Blocked(Exception):
    pass


def _check_host(url: str, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise _Blocked("Only public http(s) pages can be read.")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror:
        raise _Blocked(f"Couldn't find the website {parsed.hostname}.")
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if not address.is_global:
            raise _Blocked("That address points to a private network, so it wasn't opened.")


def _robots_allows(url: str, session: requests.Session) -> bool:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with _robots_lock:
        cached = _robots_cache.get(origin, "missing")
    if cached == "missing":
        parser: RobotFileParser | None = RobotFileParser()
        try:
            response = session.get(f"{origin}/robots.txt", timeout=TIMEOUT, allow_redirects=True)
            if response.status_code in (401, 403):
                parser.disallow_all = True
            elif response.status_code >= 500:
                parser.disallow_all = True      # RFC 9309: assume disallowed when the server errors
            elif response.ok:
                parser.parse(response.text.splitlines())
            else:
                parser.allow_all = True         # no robots.txt: everything is allowed
        except requests.RequestException:
            parser.disallow_all = True          # couldn't check, so don't crawl
        with _robots_lock:
            _robots_cache[origin] = parser
        cached = parser
    return cached.can_fetch(USER_AGENT, url)


BOT_CHECK_MARKERS = ("just a moment", "attention required", "verify you are human", "access denied",
                     "are you a robot", "captcha")


def _validate_url(url: str) -> str | None:
    """A reason the URL can't be used, or None."""
    if not isinstance(url, str) or not url.strip():
        return "No web address was given."
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return "That isn't a web address (it must start with http:// or https://)."
    if not parsed.hostname or "." not in parsed.hostname and parsed.hostname != "localhost":
        return "That web address is incomplete."
    if len(url) > 2000:
        return "That web address is too long."
    return None


def _extract(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    title = normalize_space(soup.title.get_text()) if soup.title else ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = normalize_space(meta.get("content", "")) if meta else ""
    for tag in soup(REMOVE_TAGS):
        tag.decompose()

    container = soup.find("article") or soup.find("main") or soup.body or soup
    headings = []
    for node in container.find_all(["h1", "h2", "h3"]):
        text = normalize_space(node.get_text(" "))
        if 2 < len(text) <= 160:
            headings.append({"level": int(node.name[1]), "text": text})

    blocks = []
    for node in container.find_all(["p", "li", "h2", "h3", "blockquote"]):
        text = normalize_space(node.get_text(" "))
        if len(text) >= 3:
            blocks.append(text)
    text = "\n".join(blocks)[:MAX_TEXT_CHARS]

    domain = (urlparse(url).hostname or "").removeprefix("www.")
    return {
        "url": url,
        "domain": domain,
        "title": title[:200] or domain,
        "description": description[:300],
        "headings": headings[:40],
        "text": text,
        "word_count": len(text.split()),
    }


def fetch_page(url: str, settings: Settings) -> ToolResult:
    """Read one page. Returns a ScrapedPage dict (see graph/state.py)."""
    problem = _validate_url(url)
    if problem:
        return ToolResult.error(problem)
    url = url.strip()
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
    allow_private = settings.scraper_allow_private_hosts

    try:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            _check_host(current, allow_private)
            if not _robots_allows(current, session):
                return ToolResult.error("The site's robots.txt asks crawlers not to read this page, so it was skipped.")
            response = session.get(current, timeout=TIMEOUT, allow_redirects=False, stream=True)
            if response.is_redirect or response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                if not location:
                    return ToolResult.error("The page redirected without saying where to.")
                current = urljoin(current, location)
                continue
            break
        else:
            return ToolResult.error("The page redirected too many times.")

        with response:
            if response.status_code in (401, 403, 451):
                return ToolResult.error("The site blocked automated access to this page.")
            if response.status_code == 429:
                return ToolResult.error("The site is rate-limiting requests; try again later.")
            if response.status_code >= 400:
                return ToolResult.error(f"The page returned HTTP {response.status_code}.")
            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type:
                return ToolResult.error("That address isn't a web page (no HTML).")
            body = b""
            for chunk in response.iter_content(64 * 1024):
                body += chunk
                if len(body) > MAX_BYTES:
                    return ToolResult.error("The page is too large to read.")
            html = body.decode(response.encoding or "utf-8", errors="replace")
    except _Blocked as blocked:
        return ToolResult.error(str(blocked))
    except requests.Timeout:
        return ToolResult.error("The page took too long to load.")
    except requests.RequestException:
        return ToolResult.error("Couldn't load the page.")

    page = _extract(html, current)
    if any(marker in page["title"].lower() for marker in BOT_CHECK_MARKERS):
        return ToolResult.error("The site showed a bot check instead of the page.")
    if page["word_count"] < 40:
        return ToolResult.empty("The page had almost no readable text.", page)
    return ToolResult.success(f"Read {page['word_count']} words and {len(page['headings'])} headings", page)


def clear_robots_cache() -> None:
    """Forget cached robots.txt files (used by tests)."""
    with _robots_lock:
        _robots_cache.clear()
