"""Units that don't need the HTTP layer."""

import pytest

from config import load_settings
from llm.provider import LLMConfigurationError, get_chat_model
from services import events


def test_event_builders_validate():
    event = events.tool_completed_event("google_search", "analysis", "run_1", "search_competitor_content",
                                        "call_1", "completed", "Searched competitor content", "8 results")
    assert event["type"] == "tool_completed" and event["result"] == "8 results"
    with pytest.raises(ValueError):
        events.agent_started_event("researcher", "run_1", "Not a real agent")
    with pytest.raises(ValueError):
        events.tool_completed_event("x", "analysis", "r", None, "c", "maybe", "m", "r")
    with pytest.raises(ValueError):
        events.approval_required_event("publish", {})


def test_supervisor_reads_requests():
    from supervisor.supervisor_agent import Supervisor
    assert Supervisor.understand_request("Write me a blog about Agentic AI for our target audience") == ("blog", "Agentic AI")
    assert Supervisor.understand_request("Can you write an article on PIM for retail?") == ("blog", "PIM for retail")
    assert Supervisor.understand_request("hello")[0] == "unsupported"
    assert Supervisor.decide_next({"request_type": "blog"}) == "analysis"
    assert Supervisor.decide_next({"request_type": "blog", "research_approved": True}) == "generation"
    assert Supervisor.decide_next({"request_type": "blog", "research_approved": True, "content_approved": True}) == "publish"
    assert Supervisor.decide_next({"request_type": "blog", "content_approved": True, "publish_status": "published"}) == "end"


def test_llm_errors_are_described_plainly():
    from llm.provider import describe_llm_error
    from tests.fakes import AuthenticationError
    assert "API key" in describe_llm_error(AuthenticationError("sk-123 invalid"))
    assert "sk-123" not in describe_llm_error(AuthenticationError("sk-123 invalid"))


def test_llm_factory_explains_missing_config(tmp_path):
    with pytest.raises(LLMConfigurationError, match="LLM_MODEL"):
        get_chat_model(load_settings(llm_provider="groq", llm_model="", groq_api_key=""))
    with pytest.raises(LLMConfigurationError, match="GROQ_API_KEY"):
        get_chat_model(load_settings(llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key=""))
    with pytest.raises(LLMConfigurationError, match="isn't supported"):
        get_chat_model(load_settings(llm_provider="nope", llm_model="x"))


def test_groq_is_the_free_default_and_builds_without_network():
    pytest.importorskip("langchain_groq")
    settings = load_settings(llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="gsk_test")
    model = get_chat_model(settings)
    assert type(model).__name__ == "ChatGroq" and model.model_name == "openai/gpt-oss-120b"
    assert model.max_retries >= 4          # rides out short free-tier rate limits
    assert settings.integrations["llm"] is True


def test_settings_never_expose_keys():
    settings = load_settings(llm_provider="groq", groq_api_key="gsk-secret", llm_model="openai/gpt-oss-120b")
    assert "gsk-secret" not in repr(settings)
    assert settings.integrations["llm"] is True


def test_phase2_database_is_upgraded_in_place(tmp_path):
    """A database created before Phase 3 gains the new column and keeps its data."""
    import sqlite3
    from storage.database import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
        current_agent TEXT, current_node TEXT, approval_required TEXT,
        research_approved INTEGER NOT NULL DEFAULT 0, content_approved INTEGER NOT NULL DEFAULT 0,
        blog_status TEXT NOT NULL DEFAULT 'none', publish_status TEXT NOT NULL DEFAULT 'none',
        last_error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("INSERT INTO sessions (id, title, status, created_at, updated_at) VALUES ('a', 't', 'REQUESTED', 'x', 'x')")
    conn.commit()
    conn.close()

    Database(path).initialize()
    conn = sqlite3.connect(path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    assert "graph_thread_id" in columns
    assert conn.execute("SELECT title FROM sessions WHERE id = 'a'").fetchone() == ("t",)
