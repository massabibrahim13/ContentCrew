"""
Publishing: the rules for turning an approved draft into a published post.

POST /blog (and its alias POST /api/blog) is the only way in. The Publish Blog
tool calls it over HTTP, like any client would, and this function decides.
Every rule is checked on the server, so nothing the browser or an agent sends
can skip one:

    1. the draft and the chat exist, and the draft belongs to that chat
    2. it isn't already published
    3. the human's content approval is recorded for that chat
    4. the workflow is at its publish step (it only gets there after the human
       chose Publish), so a request at any other moment is refused
    5. it's the chat's current draft, not an older version from before a rewrite
    6. it has a title and content
"""

from __future__ import annotations

from graph.state import WorkflowStatus
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
    if session["status"] != WorkflowStatus.PUBLISHING.value:
        raise PublishRefused(409, "wrong_workflow_state",
                             "Publishing only happens at the workflow's publish step, after you choose Publish.")
    if session["current_blog_id"] and blog_id != session["current_blog_id"]:
        raise PublishRefused(409, "not_current_draft",
                             "This is an older version of the draft. Only the current version can be published.")
    if not (blog["title"] or "").strip() or not (blog["content"] or "").strip():
        raise PublishRefused(422, "empty_content", "The draft has no title or no content.")
    return blogs.mark_published(blog_id)
