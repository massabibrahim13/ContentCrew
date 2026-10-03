import pytest

from api import deps

CONTENT = "## Introduction\n\n" + "Agentic AI systems plan, use tools and ask for approval. " * 3


@pytest.fixture
def draft(app, client, onboarded):
    """A chat whose current draft is waiting for review, as after the Generation Agent finishes."""
    session = client.post("/api/chat", json={"message": "Write me a blog"}).get_json()["session"]
    with app.app_context():
        blog = deps.blog_repo().create_draft(session["id"], "Agentic AI: The Future of Work", CONTENT)
        deps.session_repo().update(session["id"], status="WAITING_FOR_CONTENT_APPROVAL", current_blog_id=blog["id"])
    return session, blog


def at_publish_step(app, session_id):
    """What the workflow does after the human chooses Publish: approval recorded, publish step running."""
    with app.app_context():
        deps.session_repo().update(session_id, content_approved=True, status="PUBLISHING")


def test_get_and_edit_draft(client, draft):
    _, blog = draft
    assert client.get(f"/api/blog/{blog['id']}").get_json()["blog"]["status"] == "draft"

    edited = client.patch(f"/api/blog/{blog['id']}", json={"title": "Agentic AI at work"})
    assert edited.status_code == 200
    assert edited.get_json()["blog"]["title"] == "Agentic AI at work"
    assert edited.get_json()["blog"]["content"] == CONTENT  # untouched field stays as-is


def test_edit_validation(client, draft):
    _, blog = draft
    response = client.patch(f"/api/blog/{blog['id']}", json={"title": "", "content": "too short"})
    assert response.status_code == 422
    assert set(response.get_json()["error"]["fields"]) == {"title", "content"}


def test_publish_requires_server_side_approval(client, draft):
    session, blog = draft
    response = client.post("/api/blog", json={"session_id": session["id"], "blog_id": blog["id"]})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "approval_required"


def test_publish_after_approval(app, client, draft):
    session, blog = draft
    at_publish_step(app, session["id"])

    response = client.post("/blog", json={"session_id": session["id"], "blog_id": blog["id"]})   # POST /blog
    assert response.status_code == 201
    published = response.get_json()["blog"]
    assert published["status"] == "published"
    assert published["slug"].startswith("agentic-ai-the-future-of-work-")

    again = client.post("/api/blog", json={"session_id": session["id"], "blog_id": blog["id"]})   # the alias
    assert again.status_code == 409 and again.get_json()["error"]["code"] == "already_published"
    assert client.patch(f"/api/blog/{blog['id']}", json={"title": "New title"}).status_code == 409
    assert len(client.get("/api/blogs?status=published").get_json()["blogs"]) == 1


def test_publish_is_refused_outside_the_publish_step(app, client, draft):
    session, blog = draft
    with app.app_context():
        deps.session_repo().update(session["id"], content_approved=True)   # approved, but still in review
    response = client.post("/blog", json={"session_id": session["id"], "blog_id": blog["id"]})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "wrong_workflow_state"


def test_only_the_current_draft_can_be_published_or_edited(app, client, draft):
    session, old = draft
    with app.app_context():
        new = deps.blog_repo().create_draft(session["id"], "Agentic AI, rewritten", CONTENT + " New version.")
        deps.session_repo().update(session["id"], current_blog_id=new["id"])
    edit = client.patch(f"/api/blog/{old['id']}", json={"title": "Editing the old one"})
    assert edit.status_code == 409 and edit.get_json()["error"]["code"] == "not_current_draft"

    at_publish_step(app, session["id"])
    response = client.post("/blog", json={"session_id": session["id"], "blog_id": old["id"]})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "not_current_draft"
    assert client.post("/blog", json={"session_id": session["id"], "blog_id": new["id"]}).status_code == 201


def test_drafts_are_locked_outside_review(app, client, draft):
    session, blog = draft
    at_publish_step(app, session["id"])        # after Publish: a late edit would skip the approval
    response = client.patch(f"/api/blog/{blog['id']}", json={"title": "Sneaky late change"})
    assert response.status_code == 409 and response.get_json()["error"]["code"] == "draft_locked"


def test_empty_drafts_are_not_published(app, client, draft):
    session, blog = draft
    with app.app_context(), deps.db().connect() as conn:
        conn.execute("UPDATE blogs SET content = '  ' WHERE id = ?", (blog["id"],))
    at_publish_step(app, session["id"])
    response = client.post("/blog", json={"session_id": session["id"], "blog_id": blog["id"]})
    assert response.status_code == 422 and response.get_json()["error"]["code"] == "empty_content"


def test_draft_from_another_session_is_refused(app, client, draft):
    _, blog = draft
    other = client.post("/api/chat", json={"message": "Another chat"}).get_json()["session"]
    at_publish_step(app, other["id"])
    response = client.post("/api/blog", json={"session_id": other["id"], "blog_id": blog["id"]})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "wrong_session"


def test_unknown_blog(client, onboarded):
    assert client.get("/api/blog/" + "a" * 32).status_code == 404
    assert client.get("/api/blog/bad-id").status_code == 404
