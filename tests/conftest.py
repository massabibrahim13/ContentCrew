import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import create_app  # noqa: E402
from config import load_settings  # noqa: E402
from tests import fakes  # noqa: E402

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


@pytest.fixture
def fake_search(monkeypatch):
    """Turn search 'on' with a stand-in provider. Use with make_app(search=True)."""
    search = fakes.FakeSearch()
    monkeypatch.setattr("tools.google_search.search", search)
    return search


@pytest.fixture
def make_app(tmp_path):
    """Build an app; pass llm=FakeLLM() to give the agents a model."""
    def build(llm=None, search=False, **overrides):
        if search:
            overrides.update(search_provider="tavily", search_api_key="tvly-test")
        app = create_app(make_settings(tmp_path, **overrides),
                         llm_factory=(lambda: llm) if llm is not None else None)
        app.config.update(TESTING=True)
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
