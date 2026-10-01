"""
Publish Blog tool: the workflow's one external action.

It publishes through ContentCrew's own blog backend (the same code behind
POST /api/blog), which refuses unless the human approved the draft. Pointing it
at a real CMS later (WordPress, Webflow...) means changing this file only.
"""

from __future__ import annotations

from services.publishing import PublishRefused, publish_approved_blog
from storage.database import Database
from tools import ToolResult


def publish(db: Database, session_id: str, blog_id: str) -> ToolResult:
    try:
        blog = publish_approved_blog(db, session_id, blog_id)
    except PublishRefused as refused:
        return ToolResult.error(refused.message)
    return ToolResult.success(f"Published at /blog/{blog['slug']}", blog)
