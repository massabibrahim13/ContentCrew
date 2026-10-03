"""Tools in isolation. The scraper tests use a real HTTP server on this machine."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from config import load_settings
from tests import fakes
from tools import competitor_analysis, google_search, keyword_analysis
from tools import web_scraper
from tools.web_scraper import fetch_page as real_fetch_page  # bound before the offline patch

ARTICLE = b"""<html><head><title>Agentic AI explained</title><meta name="description" content="A plain guide to agents.">
<script>alert('x')</script></head>
<body><nav>Home | Pricing | Sign in</nav>
<article><h1>Agentic AI explained</h1>
<h2>What is an AI agent?</h2><p>""" + b"An agent plans a task and uses tools to finish it. " * 20 + b"""</p>
<h2>Keeping humans in the loop</h2><p>""" + b"Approval checkpoints keep people in charge. " * 10 + b"""</p>
</article><footer>Copyright</footer></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        routes = {
            "/robots.txt": (200, "text/plain", b"User-agent: *\nDisallow: /private\n"),
            "/article": (200, "text/html; charset=utf-8", ARTICLE),
            "/private": (200, "text/html", ARTICLE),
            "/notes.txt": (200, "text/plain", b"just text"),
            "/slow": (200, "text/html", ARTICLE),
            "/empty": (200, "text/html", b"<html><body><nav>Menu</nav><p>Hi</p></body></html>"),
            "/botcheck": (200, "text/html", b"<html><head><title>Just a moment...</title></head><body>" + b"x " * 60 + b"</body></html>"),
        }
        if self.path == "/slow":
            import time
            time.sleep(1.5)
        if self.path == "/forbidden":
            self.send_response(403)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            return
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/article")
            self.end_headers()
            return
        status, kind, body = routes.get(self.path, (404, "text/html", b"missing"))
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture(autouse=True)
def fresh_robots():
    web_scraper.clear_robots_cache()


LOCAL = load_settings(scraper_allow_private_hosts=True)


# --- Web scraper -------------------------------------------------------------

def test_scraper_extracts_main_content_only(site):
    result = real_fetch_page(f"{site}/article", LOCAL)
    assert result.ok, result.message
    page = result.data
    assert page["title"] == "Agentic AI explained"
    assert [h["text"] for h in page["headings"]] == ["Agentic AI explained", "What is an AI agent?",
                                                     "Keeping humans in the loop"]
    assert "alert" not in page["text"] and "Pricing" not in page["text"] and "Copyright" not in page["text"]
    assert page["word_count"] > 100
    assert page["description"] == "A plain guide to agents."


def test_scraper_respects_robots_txt(site):
    result = real_fetch_page(f"{site}/private", LOCAL)
    assert result.status == "error" and "robots.txt" in result.message


def test_scraper_follows_redirects_and_rejects_non_html(site):
    assert real_fetch_page(f"{site}/redirect", LOCAL).ok
    assert "isn't a web page" in real_fetch_page(f"{site}/notes.txt", LOCAL).message
    assert "404" in real_fetch_page(f"{site}/nothing", LOCAL).message


def test_scraper_blocks_private_addresses_by_default(site):
    result = real_fetch_page(f"{site}/article", load_settings(scraper_allow_private_hosts=False))
    assert result.status == "error" and "private network" in result.message
    assert real_fetch_page("file:///etc/passwd", LOCAL).status == "error"


# --- Search ------------------------------------------------------------------

def test_search_reports_not_configured():
    result = google_search.search("agentic ai", load_settings(search_provider="", search_api_key=""))
    assert result.status == "not_configured" and result.data is None
    other = google_search.search("agentic ai", load_settings(search_provider="bing", search_api_key="k"))
    assert other.status == "not_configured" and "isn't supported" in other.message


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.ok = status, payload, 200 <= status < 300

    def json(self):
        return self._payload


def test_serper_results_are_cleaned(monkeypatch):
    payload = {"organic": [
        {"title": "  What is   agentic AI ", "link": "https://example.com/a", "snippet": "An intro"},
        {"title": "Bad", "link": "javascript:alert(1)"},
    ]}
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, key=headers["X-API-KEY"])
        return FakeResponse(200, payload)

    monkeypatch.setattr(google_search.requests, "post", fake_post)
    result = google_search.search("agentic ai", load_settings(search_provider="serper", search_api_key="key"))
    assert result.ok and sent["key"] == "key" and sent["json"]["q"] == "agentic ai"
    assert result.data == [{"title": "What is agentic AI", "url": "https://example.com/a", "domain": "example.com",
                            "snippet": "An intro", "query": "agentic ai"}]


def test_tavily_results_are_cleaned(monkeypatch):
    payload = {"results": [
        {"title": "Agentic AI guide", "url": "https://example.com/g", "content": "  A   guide ", "score": 0.9},
        {"title": "No url", "url": ""},
    ]}
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(url=url, json=json, auth=headers["Authorization"])
        return FakeResponse(200, payload)

    monkeypatch.setattr(google_search.requests, "post", fake_post)
    result = google_search.search("agentic ai", load_settings(search_provider="tavily", search_api_key="tvly-k"))
    assert result.ok and sent["auth"] == "Bearer tvly-k"
    assert sent["json"]["search_depth"] == "basic"          # 1 free credit per search
    assert result.data == [{"title": "Agentic AI guide", "url": "https://example.com/g", "domain": "example.com",
                            "snippet": "A guide", "query": "agentic ai"}]


def test_tavily_quota_is_explained(monkeypatch):
    monkeypatch.setattr(google_search.requests, "post", lambda *a, **k: FakeResponse(432, {}))
    result = google_search.search("x", load_settings(search_provider="tavily", search_api_key="k"))
    assert result.status == "error" and "free search allowance" in result.message


def test_serper_errors_are_explained(monkeypatch):
    monkeypatch.setattr(google_search.requests, "post", lambda *a, **k: FakeResponse(401, {}))
    result = google_search.search("x", load_settings(search_provider="serper", search_api_key="bad"))
    assert result.status == "error" and "rejected" in result.message


def test_a_rejected_key_is_remembered_and_not_retried(monkeypatch, caplog):
    sent = []
    monkeypatch.setattr(google_search.requests, "post",
                        lambda *a, **k: sent.append(1) or FakeResponse(401, {"detail": {"error": "Invalid API key"}}))
    settings = load_settings(search_provider="tavily", search_api_key="tvly-wrong")
    first = google_search.search("x", settings)
    assert first.status == "error" and "rejected" in first.message and "start with" not in first.message
    assert "Invalid API key" in caplog.text and "tvly-wrong" not in caplog.text    # reason logged, key never

    again = google_search.search("y", settings)
    assert again.status == "not_configured" and "rejected" in again.message and len(sent) == 1
    assert not google_search.is_configured(settings)
    assert google_search.is_configured(load_settings(search_provider="tavily", search_api_key="tvly-fixed"))


def test_a_tavily_key_without_its_prefix_gets_a_hint(monkeypatch):
    monkeypatch.setattr(google_search.requests, "post", lambda *a, **k: FakeResponse(401, {}))
    result = google_search.search("x", load_settings(search_provider="tavily", search_api_key='"tvly-quoted"'))
    assert "Tavily keys start with tvly-" in result.message


# --- Analysis tools ----------------------------------------------------------

def test_keyword_analysis_is_labelled_heuristic_and_has_no_invented_metrics():
    pages = list(fakes.PAGES.values())
    result = keyword_analysis.analyze("Agentic AI", pages, load_settings(), brand_names=["Akeneo"])
    keywords = result.data["keywords"]
    assert result.ok and all(k["source"] == "heuristic" for k in keywords)
    agentic = next(k for k in keywords if k["term"] == "agentic ai")
    assert agentic["documents"] == len(pages)                      # a real count
    assert agentic["difficulty"].endswith("(heuristic)") and agentic["intent"].endswith("(heuristic)")
    assert all(isinstance(k["related"], list) for k in keywords)
    assert result.data["metrics"]["status"] == "not_configured" and result.data["metrics"]["data"] == {}
    assert keyword_analysis.analyze("x", [], load_settings()).status == "empty"


def test_competitor_analysis_finds_shared_themes():
    result = competitor_analysis.compare(list(fakes.PAGES.values()))
    themes = {t["theme"]: t["sources"] for t in result.data["common_themes"]}
    assert themes.get("data quality") == 2 and themes.get("product information") == 2
    assert result.data["representative_headings"]
    assert competitor_analysis.compare([]).status == "empty"


def test_scraper_rejects_invalid_urls():
    for bad in ["", "not a url", "ftp://example.com/file", "https://"]:
        result = real_fetch_page(bad, LOCAL)
        assert result.status == "error" and result.message, bad


def test_scraper_handles_timeouts_blocks_bot_checks_and_empty_pages(site, monkeypatch):
    monkeypatch.setattr(web_scraper, "TIMEOUT", (1, 0.5))
    assert "too long" in real_fetch_page(f"{site}/slow", LOCAL).message
    assert "blocked automated access" in real_fetch_page(f"{site}/forbidden", LOCAL).message
    assert "bot check" in real_fetch_page(f"{site}/botcheck", LOCAL).message
    empty = real_fetch_page(f"{site}/empty", LOCAL)
    assert empty.status == "empty" and "almost no readable text" in empty.message


def test_competitor_targeted_search_is_sent_to_the_provider(monkeypatch):
    sent = {}

    def fake_post(url, json, headers, timeout):
        sent.update(json)
        return FakeResponse(200, {"results": [], "organic": []})

    monkeypatch.setattr(google_search.requests, "post", fake_post)
    google_search.search("agentic ai", load_settings(search_provider="tavily", search_api_key="k"),
                         include_domains=["akeneo.com", "salsify.com"])
    assert sent["include_domains"] == ["akeneo.com", "salsify.com"]
    google_search.search("agentic ai", load_settings(search_provider="serper", search_api_key="k"),
                         include_domains=["akeneo.com"])
    assert sent["q"] == "agentic ai site:akeneo.com"


def test_search_empty_and_network_failure(monkeypatch):
    import requests as real_requests
    settings = load_settings(search_provider="tavily", search_api_key="k")
    monkeypatch.setattr(google_search.requests, "post", lambda *a, **k: FakeResponse(200, {"results": []}))
    assert google_search.search("x", settings).status == "empty"

    def boom(*a, **k):
        raise real_requests.ConnectionError("down")

    monkeypatch.setattr(google_search.requests, "post", boom)
    result = google_search.search("x", settings)
    assert result.status == "error" and "Couldn't reach" in result.message


def test_competitor_analysis_reports_questions_length_structure_and_thin_topics():
    pages = list(fakes.PAGES.values())
    data = competitor_analysis.compare(pages).data
    assert "What is agentic AI?" in data["questions_covered"]
    assert data["length"]["shortest"] <= data["length"]["average"] <= data["length"]["longest"]
    assert [s["domain"] for s in data["structure_by_source"]] == [p["domain"] for p in pages]
    assert all(s["h2"] == 2 for s in data["structure_by_source"])
    assert data["thin_topics"] and data["common_headings"]


def test_keyword_intent_and_difficulty_are_heuristic_labels():
    from tools.keyword_analysis import heuristic_difficulty, heuristic_intent
    assert heuristic_intent("best pim software", set()) == "commercial (heuristic)"
    assert heuristic_intent("pim pricing", set()) == "transactional (heuristic)"
    assert heuristic_intent("how to choose a pim", set()) == "informational (heuristic)"
    assert heuristic_intent("akeneo features", {"akeneo"}) == "navigational (heuristic)"
    assert heuristic_difficulty(3, 3, True) == "Crowded (heuristic)"
    assert heuristic_difficulty(0, 3, False) == "Open (heuristic)"
