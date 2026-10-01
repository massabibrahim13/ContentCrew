"""
Application settings.

Every value comes from environment variables (loaded from `.env` if present),
so secrets never live in the code. Copy `.env.example` to `.env` and fill it in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"

load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Server
    host: str = "127.0.0.1"
    port: int = 5000
    debug: bool = False
    secret_key: str = "dev-only-change-me"

    # Storage
    database_path: Path = BASE_DIR / "data" / "contentcrew.db"
    checkpoint_path: Path = BASE_DIR / "data" / "checkpoints.db"   # LangGraph's saved workflow state
    log_dir: Path = BASE_DIR / "logs"

    # Language model. Default: Groq's free tier (no credit card).
    llm_provider: str = "groq"
    llm_model: str = ""
    groq_api_key: str = field(default="", repr=False)
    openai_api_key: str = field(default="", repr=False)   # optional, paid

    # Web search. Default: Tavily's free plan (1,000 searches a month, no credit card).
    search_provider: str = ""
    search_api_key: str = field(default="", repr=False)
    seo_api_key: str = field(default="", repr=False)       # no free provider exists; unused

    # The web scraper refuses private/internal addresses (SSRF protection).
    # Only the test suite turns this on, to read pages from a local test server.
    scraper_allow_private_hosts: bool = False

    # Runs the workflow in the request thread instead of a background thread.
    # Only the test suite turns this on, so results are deterministic.
    run_workflow_synchronously: bool = False

    @property
    def integrations(self) -> dict[str, bool]:
        """Which external services are configured. Booleans only, never keys."""
        return {
            "llm": bool(self.llm_model and self._llm_key_present()),
            "search": bool(self.search_provider and self.search_api_key),
        }

    def _llm_key_present(self) -> bool:
        keys = {"groq": self.groq_api_key, "openai": self.openai_api_key}
        return bool(keys.get(self.llm_provider))


def load_settings(**overrides) -> Settings:
    """Build settings from the environment. Keyword overrides win (used by tests)."""
    values = dict(
        host=_env("FLASK_HOST", "127.0.0.1"),
        port=_env_int("FLASK_PORT", 5000),
        debug=_env_bool("FLASK_DEBUG", False),
        secret_key=_env("FLASK_SECRET_KEY") or "dev-only-change-me",
        database_path=Path(_env("DATABASE_PATH") or BASE_DIR / "data" / "contentcrew.db"),
        checkpoint_path=Path(_env("CHECKPOINT_PATH") or BASE_DIR / "data" / "checkpoints.db"),
        log_dir=Path(_env("LOG_DIR") or BASE_DIR / "logs"),
        llm_provider=_env("LLM_PROVIDER", "groq").lower(),
        llm_model=_env("LLM_MODEL"),
        groq_api_key=_env("GROQ_API_KEY"),
        openai_api_key=_env("OPENAI_API_KEY"),
        search_provider=_env("SEARCH_PROVIDER").lower(),
        search_api_key=_env("SEARCH_API_KEY"),
        seo_api_key=_env("SEO_API_KEY"),
    )
    values.update(overrides)
    return Settings(**values)
