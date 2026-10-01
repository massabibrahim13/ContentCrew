"""
Blog drafts and publishing.

GET   /api/blog/<id>     a draft or published post
PATCH /api/blog/<id>     save edits to a draft         {title?, content?}
POST  /api/blog          publish an approved draft     {session_id, blog_id}
GET   /api/blogs         list posts (?status=draft|published)
GET   /api/posts/<slug>  a published post (what the public /blog/<slug> page shows)

POST /api/blog is the "publish" action the Publish tool calls. It refuses unless
the server has recorded the human's content approval for that chat, so a
crafted request from the browser (or a prompt-injected agent) can't publish.
"""

from __future__ import annotations

import logging
import re

from flask import Blueprint, jsonify, request

from api import deps
from api.errors import ApiError
from api.validation import Validator, json_body
from services.publishing import PublishRefused, publish_approved_blog

log = logging.getLogger(__name__)
bp = Blueprint("blog", __name__)

TITLE_LIMITS = dict(min_len=5, max_len=160)
CONTENT_LIMITS = dict(min_len=50, max_len=60_000)


def _blog_or_404(blog_id: str) -> dict:
    blog = deps.blog_repo().get(blog_id) if re.fullmatch(r"[0-9a-f]{32}", blog_id) else None
    if blog is None:
        raise ApiError(404, "blog_not_found", "That blog post doesn't exist.")
    return blog


@bp.get("/blog/<blog_id>")
def get_blog(blog_id: str):
    return jsonify({"blog": _blog_or_404(blog_id)})


@bp.patch("/blog/<blog_id>")
def update_blog(blog_id: str):
    blog = _blog_or_404(blog_id)
    if blog["status"] != "draft":
        raise ApiError(409, "already_published", "Published posts can't be edited here.")

    data = json_body()
    v = Validator()
    title = v.text(data.get("title"), "title", "a title", **TITLE_LIMITS) if "title" in data else None
    content = (v.text(data.get("content"), "content", "the post content", multiline=True, **CONTENT_LIMITS)
               if "content" in data else None)
    if "title" in data and title is None:
        v.fail("title", "Enter a title.")
    if "content" in data and content is None:
        v.fail("content", "The post can't be empty.")
    v.raise_if_errors()

    return jsonify({"blog": deps.blog_repo().update_draft(blog_id, title, content)})


@bp.post("/blog")
def publish_blog():
    data = json_body()
    v = Validator()
    session_id = v.identifier(data.get("session_id"), "session_id", "the session id")
    blog_id = v.identifier(data.get("blog_id"), "blog_id", "the blog id")
    v.raise_if_errors()

    try:
        published = publish_approved_blog(deps.db(), session_id, blog_id)
    except PublishRefused as refused:
        raise ApiError(refused.status, refused.code, refused.message)
    log.info("Published blog %s (%s)", blog_id, published["slug"])
    return jsonify({"blog": published}), 201


@bp.get("/blogs")
def list_blogs():
    status = request.args.get("status")
    if status not in (None, "draft", "published"):
        raise ApiError(422, "validation_error", "status must be 'draft' or 'published'.")
    return jsonify({"blogs": deps.blog_repo().list(status)})


@bp.get("/posts/<slug>")
def get_published_post(slug: str):
    blog = deps.blog_repo().get_published_by_slug(slug) if re.fullmatch(r"[a-z0-9-]{1,100}", slug) else None
    if blog is None:
        raise ApiError(404, "post_not_found", "That post doesn't exist or isn't published.")
    return jsonify({"post": {k: blog[k] for k in ("title", "content", "slug", "published_at")}})
