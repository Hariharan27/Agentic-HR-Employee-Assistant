"""Live agent activity: POST /api/v1/chat/stream sends steps as they happen, then the reply."""

import json
from contextlib import nullcontext

import pytest

from app.api.routes.chat import get_llm_gateway, get_policy_service, get_session_scope
from app.main import app
from test_chat_orchestration import FakeLLM, FakePolicies, route


def parse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines["event"], json.loads(lines["data"])))
    return events


@pytest.fixture()
def stream_client(client, db_session):
    app.dependency_overrides[get_session_scope] = lambda: (lambda: nullcontext(db_session))
    app.dependency_overrides[get_policy_service] = lambda: FakePolicies()
    yield client
    app.dependency_overrides.pop(get_session_scope, None)
    app.dependency_overrides.pop(get_policy_service, None)
    app.dependency_overrides.pop(get_llm_gateway, None)


def token_for(client, username: str) -> str:
    return client.post(
        "/api/v1/auth/login", json={"username": username, "password": "correct-password"}
    ).json()["access_token"]


def test_stream_sends_routing_and_tool_steps_before_the_final_reply(stream_client):
    app.dependency_overrides[get_llm_gateway] = lambda: FakeLLM([route(leave_type="CASUAL")])
    token = token_for(stream_client, "employee")

    response = stream_client.post(
        "/api/v1/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "What is my casual leave balance?"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = parse_events(response.text)
    kinds = [kind for kind, _ in events]
    assert kinds[-1] == "final" and kinds.count("final") == 1
    steps = [data for kind, data in events if kind == "step"]
    assert steps[0] == {"id": "route", "label": "Understood: Leave · leave balance", "status": "success"}
    tool_steps = [step for step in steps if ":tool-" in step["id"]]
    assert [step["status"] for step in tool_steps] == ["running", "success"]
    assert tool_steps[0]["label"] == "Checking leave balance"
    final = events[-1][1]
    assert final["intent"] == "leave_balance"
    assert [item["tool"] for item in final["agent_activity"]] == ["get_leave_balance"]
    assert "4" in final["message"]


def test_stream_keeps_the_403_status_for_unauthorized_actions(stream_client):
    app.dependency_overrides[get_llm_gateway] = lambda: FakeLLM(
        [route(intent="approve_leave_request", request_id=999)]
    )
    token = token_for(stream_client, "employee")

    response = stream_client.post(
        "/api/v1/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "Approve leave request #999"},
    )

    assert response.status_code == 403
    assert response.json()["error"]["message"] == "You are not authorized to perform this action"


def test_stream_requires_authentication(stream_client):
    response = stream_client.post("/api/v1/chat/stream", json={"message": "What is my leave balance?"})

    assert response.status_code == 401
