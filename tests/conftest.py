import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from config import load_settings  # noqa: E402
from tests import fakes  # noqa: E402
from tools import google_search  # noqa: E402

VALID_CONTEXT = {
    "company": {
        "name": "Apimio",
        "website": "apimio.com",
        "industry": "E-commerce software",
        "description": "AI-powered commerce and automation platform for product information.",
        "target_audience": ["E-commerce brands", "Retail teams"],
        "products": ["Product information management"],
    },
    "competitors": [
        {"name": "Akeneo", "website": "https://www.akeneo.com"},
        {"name": "Salsify", "website": ""},
    ],
}


def make_settings(tmp_path, **overrides):
    values = dict(
        database_path=tmp_path / "test.db",
        checkpoint_path=tmp_path / "checkpoints.db",
        log_dir=tmp_path / "logs",
        run_workflow_synchronously=True,
        openai_api_key="",
        groq_api_key="",
        llm_model="",
        search_provider="",
        search_api_key="",
        seo_api_key="",
    )
    values.update(overrides)
    return load_settings(**values)


@pytest.fixture(autouse=True)
def offline_scraper(monkeypatch):
    """Workflow tests never touch the internet: competitor pages come from fixtures."""
    monkeypatch.setattr("tools.web_scraper.fetch_page", fakes.fake_fetch_page)
    fakes.UNREADABLE.clear()
    google_search.forget_rejected_keys()      # a key rejected in one test doesn't carry into the next


@pytest.fixture
def fake_search(monkeypatch):
    """Turn search 'on' with a stand-in provider. Use with make_app(search=True)."""
    search = fakes.FakeSearch()
    monkeypatch.setattr("tools.google_search.search", search)
    return search


class _Response:
    """Looks like a `requests` response, backed by Flask's test client."""

    def __init__(self, response):
        self.status_code = response.status_code
        self._json = response.get_json(silent=True)

    def json(self):
        if self._json is None:
            raise ValueError("not JSON")
        return self._json


@pytest.fixture
def blog_api(monkeypatch):
    """
    The Publish Blog tool makes a real HTTP POST /blog. Tests don't run a server, so its
    transport is pointed at the app through Flask's test client (same routes, same checks).
    `blog_api.fail_next` makes the next calls fail as if the network were down.
    """
    import requests

    class BlogApi:
        app = None
        fail_next = 0
        calls: list = []

        def __call__(self, url, payload, timeout):
            self.calls.append((url, payload))
            if self.fail_next:
                self.fail_next -= 1
                raise requests.ConnectionError("connection refused")
            path = "/" + url.split("//", 1)[-1].split("/", 1)[-1]
            return _Response(self.app.test_client().post(path, json=payload))

    api = BlogApi()
    api.calls = []
    monkeypatch.setattr("tools.publish_blog.http_post", api)
    return api


@pytest.fixture
def make_app(tmp_path, blog_api):
    """Build an app; pass llm=FakeLLM() to give the agents a model."""
    def build(llm=None, search=False, **overrides):
        if search:
            overrides.update(search_provider="tavily", search_api_key="tvly-test")
        app = create_app(make_settings(tmp_path, **overrides),
                         llm_factory=(lambda: llm) if llm is not None else None)
        app.config.update(TESTING=True)
        blog_api.app = app
        return app
    return build


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def onboarded(client):
    response = client.put("/api/context", json=VALID_CONTEXT)
    assert response.status_code == 200, response.get_json()
    return response.get_json()
