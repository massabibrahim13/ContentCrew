import json

from api import deps
from graph.state import WorkflowStatus


def _events(client, session_id):
    return client.get(f"/api/sessions/{session_id}").get_json()["events"]


def test_chat_requires_onboarding(client):
    response = client.post("/api/chat", json={"message": "Write me a blog about Agentic AI"})
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "onboarding_required"


def test_chat_validates_message(client, onboarded):
    assert client.post("/api/chat", json={"message": ""}).status_code == 422
    assert client.post("/api/chat", json={"message": "x" * 2001}).status_code == 422
    assert client.post("/api/chat", json={"message": "hi", "session_id": "../etc"}).status_code == 422


def test_chat_creates_session_and_real_events(client, onboarded):
    response = client.post("/api/chat", json={"message": "Write me a blog about Agentic AI"})
    assert response.status_code == 202
    session = response.get_json()["session"]
    assert session["title"] == "Write me a blog about Agentic AI"
    # Tests run the workflow synchronously, so it has already reached the first pause.
    assert session["status"] == WorkflowStatus.WAITING_FOR_RESEARCH_APPROVAL.value

    events = _events(client, session["id"])
    assert events[0] == {**events[0], "type": "message", "role": "user",
                         "content": "Write me a blog about Agentic AI"}
    messages = [e["message"] for e in events if e["type"] == "workflow"]
    assert messages[0] == "Request received"
    assert "Apimio" in messages[1] and "2 competitors" in messages[1]
    agents = [e["agent"] for e in events if e["type"] == "agent_started"]
    assert agents[0] == "supervisor" and "analysis" in agents
    assert events[-1]["type"] == "approval_required"
    assert events[-1]["message"] == "Research completed. Review the strategy before content generation."


def test_follow_up_message_uses_same_session(client, onboarded):
    first = client.post("/api/chat", json={"message": "hello there"}).get_json()["session"]
    second = client.post("/api/chat", json={"message": "hello again",
                                            "session_id": first["id"]}).get_json()["session"]
    assert second["id"] == first["id"]
    contents = [e["content"] for e in _events(client, first["id"])
                if e["type"] == "message" and e["role"] == "user"]
    assert contents == ["hello there", "hello again"]


def test_new_message_waits_while_approval_is_pending(client, onboarded):
    session = client.post("/api/chat", json={"message": "Write me a blog about AI"}).get_json()["session"]
    blocked = client.post("/api/chat", json={"message": "Another one", "session_id": session["id"]})
    assert blocked.status_code == 409 and blocked.get_json()["error"]["code"] == "approval_pending"


def test_unknown_session(client, onboarded):
    response = client.post("/api/chat", json={"message": "Hello there", "session_id": "0" * 32})
    assert response.status_code == 404
    assert client.get("/api/sessions/does-not-exist").status_code == 404


def test_busy_and_waiting_sessions_are_protected(app, client, onboarded):
    session = client.post("/api/chat", json={"message": "First request"}).get_json()["session"]
    with app.app_context():
        deps.session_repo().update(session["id"], status=WorkflowStatus.RESEARCHING.value)
    busy = client.post("/api/chat", json={"message": "Again", "session_id": session["id"]})
    assert busy.status_code == 409 and busy.get_json()["error"]["code"] == "workflow_busy"

    with app.app_context():
        deps.session_repo().update(session["id"],
                                   status=WorkflowStatus.WAITING_FOR_RESEARCH_APPROVAL.value)
    waiting = client.post("/api/chat", json={"message": "Again", "session_id": session["id"]})
    assert waiting.get_json()["error"]["code"] == "approval_pending"


def test_workflow_failure_becomes_error_event(app, client, onboarded, monkeypatch):
    runner = app.extensions["contentcrew.runner"]

    def explode(*_args):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(runner.company, "get_context", explode)
    session = client.post("/api/chat", json={"message": "Write something"}).get_json()["session"]
    events = _events(client, session["id"])
    assert events[-1]["type"] == "error"
    assert "simulated" not in events[-1]["message"]  # no internals leak to the UI
    status = client.get(f"/api/status?session_id={session['id']}").get_json()
    assert status["workflow_status"] == "FAILED"
    assert status["running"] is False


def test_event_stream_replays_and_resumes(client, onboarded):
    session = client.post("/api/chat", json={"message": "Write me a blog"}).get_json()["session"]
    sid = session["id"]

    body = client.get(f"/api/sessions/{sid}/events?once=1").get_data(as_text=True)
    payloads = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    assert payloads and payloads[0]["type"] == "message"

    last_id = payloads[1]["id"]
    resumed = client.get(f"/api/sessions/{sid}/events?once=1",
                         headers={"Last-Event-ID": str(last_id)}).get_data(as_text=True)
    resumed_ids = [json.loads(l[6:])["id"] for l in resumed.splitlines() if l.startswith("data: ")]
    assert resumed_ids and min(resumed_ids) > last_id


def test_status_endpoint(client, onboarded):
    assert client.get("/api/status").status_code == 422
    session = client.post("/api/chat", json={"message": "Write me a blog"}).get_json()["session"]
    status = client.get(f"/api/status?session_id={session['id']}").get_json()
    assert status["workflow_status"] == "WAITING_FOR_RESEARCH_APPROVAL"
    assert status["approval_required"] == "research"
    assert status["current_agent"] == "supervisor"
    assert status["last_event_id"] > 0
    assert status["tool_activity"] and status["running"] is False


def test_health_reports_booleans_only(client):
    data = client.get("/api/health").get_json()
    assert data["status"] == "ok" and data["database"] == "ok"
    assert data["integrations"] == {"llm": False, "search": False}
    assert data["workflow_engine"] == "connected"


def test_approval_needs_a_pending_checkpoint(client, onboarded):
    session = client.post("/api/chat", json={"message": "Write me a blog"}).get_json()["session"]
    response = client.post("/api/approval/content",
                           json={"session_id": session["id"], "decision": "approve"})
    assert response.status_code == 409   # it's waiting for the research approval, not content
    assert response.get_json()["error"]["code"] == "no_pending_approval"

    bad = client.post("/api/approval/research",
                      json={"session_id": session["id"], "decision": "modify"})
    assert bad.status_code == 422 and "feedback" in bad.get_json()["error"]["fields"]


def test_unknown_api_route_is_json(client):
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_security_headers(client):
    response = client.get("/")
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_app_page_redirects_until_onboarded(client):
    assert client.get("/app").status_code == 302
    client.put("/api/context", json=client.get("/api/context/sample").get_json())
    assert client.get("/app").status_code == 200
