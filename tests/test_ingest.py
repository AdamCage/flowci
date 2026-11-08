from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from flowci.ingest import create_app, loopback_host, normalize_event
from tests.fakes import decision, make_agent


def test_loopback_only() -> None:
    assert loopback_host("0.0.0.0") == "127.0.0.1"
    assert loopback_host("localhost") == "127.0.0.1"


def test_normalize_event_requires_key() -> None:
    try:
        normalize_event({"source": "cursor"})
    except ValueError as exc:
        assert "deduplication_key" in str(exc)
    else:
        raise AssertionError("expected ValueError")


async def test_ingest_auth_and_duplicate_key(tmp_path) -> None:
    agent, vikunja, llm, _store = make_agent(
        tmp_path,
        decision(
            action="create",
            payload={
                "title": "Событие из Cursor",
                "assignee": "я",
                "due_date": "2026-11-22",
                "comment": "факт из сессии",
                "comment_kind": "факт",
            },
        ),
    )
    app = create_app(agent, "ingest-test-token")
    event = {
        "source": "cursor",
        "session_id": "sess-1",
        "timestamp": "2026-11-08T09:00:00Z",
        "project": "FlowCI",
        "task_hint": "Событие из Cursor",
        "facts": ["изменён agent.py"],
        "artifacts": ["src/flowci/agent.py"],
        "next_step": "прогнать тесты",
        "confidence": 0.8,
        "deduplication_key": "cursor-sess-1",
    }
    async with TestClient(TestServer(app)) as client:
        health = await client.get("/health")
        assert health.status == 200
        denied = await client.post("/v1/events", json=event)
        assert denied.status == 401
        missing = await client.post(
            "/v1/events",
            json={"source": "cursor"},
            headers={"Authorization": "Bearer ingest-test-token"},
        )
        assert missing.status == 400
        first = await client.post(
            "/v1/events",
            json=event,
            headers={"Authorization": "Bearer ingest-test-token"},
        )
        second = await client.post(
            "/v1/events",
            json=event,
            headers={"Authorization": "Bearer ingest-test-token"},
        )
        assert first.status == 200
        assert second.status == 200
        first_body = await first.json()
        second_body = await second.json()
    assert first_body["action"] == "create"
    assert first_body["noop"] is False
    assert second_body["noop"] is True
    assert llm.calls == 1
    assert len(vikunja.tasks) == 1
    assert "ingest-test-token" not in str(first_body)
