import pytest

from api import deps

CONTENT = "## Introduction\n\n" + "Agentic AI systems plan, use tools and ask for approval. " * 3


@pytest.fixture
def draft(app, client, onboarded):
    session = client.post("/api/chat", json={"message": "Write me a blog"}).get_json()["session"]
    with app.app_context():
        blog = deps.blog_repo().create_draft(session["id"], "Agentic AI: The Future of Work", CONTENT)
    return session, blog


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
    with app.app_context():
        deps.session_repo().update(session["id"], content_approved=True)

    response = client.post("/api/blog", json={"session_id": session["id"], "blog_id": blog["id"]})
    assert response.status_code == 201
    published = response.get_json()["blog"]
    assert published["status"] == "published"
    assert published["slug"].startswith("agentic-ai-the-future-of-work-")

    again = client.post("/api/blog", json={"session_id": session["id"], "blog_id": blog["id"]})
    assert again.status_code == 409
    assert client.patch(f"/api/blog/{blog['id']}", json={"title": "New title"}).status_code == 409
    assert len(client.get("/api/blogs?status=published").get_json()["blogs"]) == 1


def test_draft_from_another_session_is_refused(app, client, draft):
    _, blog = draft
    other = client.post("/api/chat", json={"message": "Another chat"}).get_json()["session"]
    with app.app_context():
        deps.session_repo().update(other["id"], content_approved=True)
    response = client.post("/api/blog", json={"session_id": other["id"], "blog_id": blog["id"]})
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "wrong_session"


def test_unknown_blog(client, onboarded):
    assert client.get("/api/blog/" + "a" * 32).status_code == 404
    assert client.get("/api/blog/bad-id").status_code == 404
