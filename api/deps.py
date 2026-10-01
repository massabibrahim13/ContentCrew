"""Shared objects the routes need, created once in `create_app` and stored on the app."""

from __future__ import annotations

from flask import current_app

from config import Settings
from services.workflow_runner import WorkflowRunner
from storage.database import Database
from storage.repositories import BlogRepository, CompanyRepository, EventRepository, SessionRepository


def settings() -> Settings:
    return current_app.extensions["contentcrew.settings"]


def db() -> Database:
    return current_app.extensions["contentcrew.db"]


def runner() -> WorkflowRunner:
    return current_app.extensions["contentcrew.runner"]


def company_repo() -> CompanyRepository:
    return CompanyRepository(db())


def session_repo() -> SessionRepository:
    return SessionRepository(db())


def event_repo() -> EventRepository:
    return EventRepository(db())


def blog_repo() -> BlogRepository:
    return BlogRepository(db())


def session_or_404(session_id: str) -> dict:
    """Look up a chat session, or answer 404 with a message the UI can show."""
    from api.errors import ApiError

    valid = isinstance(session_id, str) and len(session_id) == 32 and all(c in "0123456789abcdef" for c in session_id)
    session = session_repo().get(session_id) if valid else None
    if session is None:
        raise ApiError(404, "session_not_found", "This chat doesn't exist. Start a new chat.")
    return session
