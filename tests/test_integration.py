"""
Phase 7: the integrated workflow. Only what earlier test files don't already cover:
publishing over HTTP, consistency between drafts, approvals and the graph, Cancel request,
and the metadata needed to trace a run.
"""

import threading

from werkzeug.serving import make_server

from app import create_app
from storage.repositories import BlogRepository
from tests import fakes
from tests.conftest import make_settings
from tests.test_workflow import decide, events, start, status
from tools import publish_blog


def run_to_review(make_app, **kwargs):
    llm = fakes.FakeLLM()
    app = make_app(llm=llm, **kwargs)
    client = app.test_client()
    sid = start(client)
    decide(client, "research", sid, "approve")
    assert status(client, sid)["workflow_status"] == "WAITING_FOR_CONTENT_APPROVAL"
    return app, client, sid, llm


# ---------------------------------------------------------------------------
# Publishing: POST /blog, checked by the backend
# ---------------------------------------------------------------------------

def test_publish_goes_through_post_blog(make_app, blog_api):
    app, client, sid, _ = run_to_review(make_app)
    blog_id = app.extensions["contentcrew.runner"].sessions.get(sid)["current_blog_id"]
    decide(client, "content", sid, "approve")

    assert blog_api.calls == [("http://127.0.0.1:5000/blog", {"session_id": sid, "blog_id": blog_id})]
    tool = [t for t in events(client, sid, "tool_completed") if t["tool"] == "publish_blog"][-1]
    assert tool["message"] == "POST http://127.0.0.1:5000/blog" and tool["result"].startswith("POST /blog returned 201")
    s = status(client, sid)
    assert s["workflow_status"] == "COMPLETED" and s["publish_status"] == "published"
    assert events(client, sid, "workflow")[-1]["message"] == "Blog published successfully"


def test_publish_tool_makes_a_real_http_call(tmp_path):
    """No stand-in transport: a real server on a free port, and a real HTTP request to it."""
    app = create_app(make_settings(tmp_path))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = app.test_client()
        client.put("/api/context", json={"company": {"name": "Apimio", "website": "apimio.com",
                                                     "description": "Product information platform for retail teams.",
                                                     "target_audience": ["Retail teams"]}, "competitors": []})
        sid = client.post("/api/chat", json={"message": "Write me a blog about Agentic AI"}).get_json()["session"]["id"]
        runner = app.extensions["contentcrew.runner"]
        blog = BlogRepository(runner.db).create_draft(sid, "Agentic AI for retail", "## Intro\n\nA real post body.")
        settings = make_settings(tmp_path, port=server.server_port)

        refused = publish_blog.publish(settings, sid, blog["id"])     # not approved, not at the publish step
        assert not refused.ok and refused.data["retryable"] is False and "refused" in refused.message

        runner.sessions.update(sid, content_approved=True, status="PUBLISHING", current_blog_id=blog["id"])
        published = publish_blog.publish(settings, sid, blog["id"])
        assert published.ok and published.data["slug"].startswith("agentic-ai-for-retail")
        again = publish_blog.publish(settings, sid, blog["id"])       # a retry after a lost response
        assert again.ok and again.data == {"already_published": True}
    finally:
        server.shutdown()

    unreachable = publish_blog.publish(make_settings(tmp_path, port=server.server_port), "s", "b")
    assert not unreachable.ok and unreachable.data["retryable"] is True


def test_publish_failure_can_be_retried(make_app, blog_api):
    app, client, sid, _ = run_to_review(make_app)
    blog_api.fail_next = 1                                    # the blog API is down for the first call
    decide(client, "content", sid, "approve")
    s = status(client, sid)
    assert s["workflow_status"] == "FAILED" and s["last_error"]["retryable"]
    assert "Couldn't reach the blog API" in s["last_error"]["message"]

    assert client.post(f"/api/sessions/{sid}/retry").status_code == 202
    assert status(client, sid)["publish_status"] == "published"
    assert len(client.get("/api/blogs?status=published").get_json()["blogs"]) == 1


def test_a_refused_publish_is_reported_without_retry(make_app):
    app, client, sid, _ = run_to_review(make_app)
    app.extensions["contentcrew.runner"].sessions.update(sid, current_blog_id="0" * 32)   # state mismatch
    decide(client, "content", sid, "approve")
    s = status(client, sid)
    assert s["workflow_status"] == "FAILED" and not s["last_error"]["retryable"]
    assert "older version" in s["last_error"]["message"]
    assert not events(client, sid, "blog_published")


def test_after_regeneration_only_the_new_draft_is_published(make_app):
    app, client, sid, _ = run_to_review(make_app)
    first = events(client, sid, "blog_ready")[-1]["blog_id"]
    client.post("/api/chat", json={"message": "Make the introduction more concise", "session_id": sid})
    second = events(client, sid, "blog_ready")[-1]["blog_id"]
    assert second != first

    assert client.patch(f"/api/blog/{first}", json={"title": "Edit the old one"}).status_code == 409
    client.patch(f"/api/blog/{second}", json={"title": "The final title"})
    decide(client, "content", sid, "approve")

    published = events(client, sid, "blog_published")[-1]
    assert published["blog_id"] == second and published["title"] == "The final title"
    assert client.get(f"/api/blog/{first}").get_json()["blog"]["status"] == "draft"


# ---------------------------------------------------------------------------
# Approvals and the graph agree
# ---------------------------------------------------------------------------

def test_cancel_request_ends_the_run_cleanly(make_app):
    client = make_app(llm=fakes.FakeLLM()).test_client()
    sid = start(client)
    assert decide(client, "research", sid, "reject").status_code == 202

    s = status(client, sid)
    assert s["workflow_status"] == "CANCELLED" and s["approval_required"] is None
    assert events(client, sid, "approval_resolved")[-1]["decision"] == "reject"
    assert "Request cancelled" in [m for m in events(client, sid, "message") if m["role"] == "assistant"][-1]["content"]
    plan = events(client, sid, "plan_updated")[-1]["plan"]
    assert [step["status"] for step in plan][-4:] == ["skipped"] * 4
    assert not [e for e in events(client, sid) if e.get("agent") == "generation"]

    # The chat carries on: a new request starts a fresh run.
    client.post("/api/chat", json={"message": "Write me a blog about PIM", "session_id": sid})
    assert status(client, sid)["workflow_status"] == "WAITING_FOR_RESEARCH_APPROVAL"


def test_an_approval_meant_for_another_checkpoint_is_refused(make_app):
    app = make_app(llm=fakes.FakeLLM())
    client = app.test_client()
    sid = start(client)                                       # the graph waits for the research approval
    runner = app.extensions["contentcrew.runner"]
    runner.sessions.update(sid, approval_required="content")  # the session row says something else

    response = decide(client, "content", sid, "approve")
    assert response.status_code == 409
    s = status(client, sid)
    assert s["approval_required"] == "research" and s["workflow_status"] == "WAITING_FOR_RESEARCH_APPROVAL"
    assert runner.state_of(sid)["research_approved"] is False   # nothing was approved by mistake


def test_missing_workflow_state_fails_without_guessing(make_app):
    app = make_app(llm=fakes.FakeLLM())
    client = app.test_client()
    sid = start(client)
    app.extensions["contentcrew.runner"].sessions.update(sid, graph_thread_id="lost-thread")
    assert decide(client, "research", sid, "approve").status_code == 409
    s = status(client, sid)
    assert s["workflow_status"] == "FAILED" and not s["last_error"]["retryable"]
    assert "saved workflow state is missing" in s["last_error"]["message"]


# ---------------------------------------------------------------------------
# Observability: a run can be reconstructed from its events
# ---------------------------------------------------------------------------

def test_every_step_is_traceable(make_app):
    app, client, sid, _ = run_to_review(make_app)
    decide(client, "content", sid, "approve")
    thread = app.extensions["contentcrew.runner"].sessions.get(sid)["graph_thread_id"]

    workflow_events = [e for e in events(client, sid) if e["type"] != "message" or e["role"] == "assistant"]
    assert all(e.get("workflow_id") == thread for e in workflow_events)
    for e in events(client, sid):
        if e["type"] in ("node_started", "node_completed", "tool_started", "tool_completed"):
            assert e["agent"] and e["node"] and e["created_at"], e
        if e["type"] in ("node_completed", "tool_completed"):
            assert e["status"] in ("completed", "skipped", "failed"), e
        if e["type"] in ("approval_required", "approval_resolved", "blog_ready", "blog_published"):
            assert e["agent"] and e["node"], e

    trace = client.get(f"/api/sessions/{sid}/trace").get_json()["trace"]
    keyword_step = next(row for row in trace if row.get("node") == "analyze_keywords" and row["type"] == "tool_completed")
    assert keyword_step["agent"] == "analysis" and keyword_step["tool"] == "keyword_analysis"
    assert keyword_step["status"] == "completed" and keyword_step["result"] and keyword_step["workflow_id"] == thread
    assert trace[-1]["message"] == "Blog published successfully"
