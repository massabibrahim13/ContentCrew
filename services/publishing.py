"""
Publishing: the one function that turns an approved draft into a published post.

Both POST /api/blog and the Publish Blog tool call this, so the rules are the
same however publishing is triggered. The key rule: the human's content
approval must be recorded on the server for that chat. Nothing the browser or
an agent sends can skip it.
"""

from __future__ import annotations

from storage.database import Database
from storage.repositories import BlogRepository, SessionRepository


class PublishRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def publish_approved_blog(db: Database, session_id: str, blog_id: str) -> dict:
    blogs = BlogRepository(db)
    blog = blogs.get(blog_id)
    if blog is None:
        raise PublishRefused(404, "blog_not_found", "That blog post doesn't exist.")
    session = SessionRepository(db).get(session_id)
    if session is None:
        raise PublishRefused(404, "session_not_found", "This chat doesn't exist.")
    if blog["session_id"] != session_id:
        raise PublishRefused(403, "wrong_session", "This draft belongs to a different chat.")
    if blog["status"] == "published":
        raise PublishRefused(409, "already_published", "This post is already published.")
    if not session["content_approved"]:
        raise PublishRefused(403, "approval_required", "Approve the draft before publishing it.")
    return blogs.mark_published(blog_id)
