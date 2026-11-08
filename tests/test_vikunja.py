from __future__ import annotations

import json

import httpx
import pytest

from flowci.vikunja import VikunjaClient
from tests.fakes import make_settings


def _client(settings, handler):
    return VikunjaClient(
        settings,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            headers={"Authorization": "Bearer vik-test"},
        ),
    )


@pytest.mark.asyncio
async def test_create_uses_put(tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "PUT" and request.url.path == "/api/v1/projects/7/tasks":
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": 3, "project_id": 7, "labels": [], **body})
        return httpx.Response(404, text="missing")

    settings = make_settings(tmp_path, vikunja_token="vik-test", vikunja_project_id=7)
    client = _client(settings, handler)
    try:
        task = await client.create_task(
            title="Новая",
            description="текст",
            due_date="2026-11-20",
            labels=[],
            priority=1,
        )
    finally:
        await client._client.aclose()
    assert task.id == 3
    assert task.due_date == "2026-11-20T00:00:00Z"
    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/api/v1/projects/7/tasks"
    assert seen[0].headers["authorization"] == "Bearer vik-test"
    body = json.loads(seen[0].content.decode())
    assert body["title"] == "Новая"
    assert body["due_date"] == "2026-11-20T00:00:00Z"


@pytest.mark.asyncio
async def test_update_posts_existing_due_date(tmp_path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/api/v1/tasks/5":
            return httpx.Response(
                200,
                json={
                    "id": 5,
                    "title": "Старая",
                    "description": "старое",
                    "done": False,
                    "priority": 3,
                    "due_date": "2026-12-01T00:00:00Z",
                    "project_id": 7,
                    "percent_done": 0,
                    "assignees": [{"id": 4, "username": "review"}],
                    "reminders": [{"reminder": "2026-11-30T10:00:00Z", "relative_period": 0}],
                    "is_favorite": True,
                    "repeat_after": 86400,
                    "repeat_mode": 0,
                    "start_date": "2026-11-01T00:00:00Z",
                    "end_date": "2026-11-02T00:00:00Z",
                    "labels": [
                        {"id": 1, "title": "В работе"},
                        {"id": 2, "title": "Сегодня"},
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/tasks/5":
            body = json.loads(request.content.decode())
            return httpx.Response(200, json={"id": 5, "labels": [], **body})
        return httpx.Response(404, text=request.url.path)

    settings = make_settings(tmp_path, vikunja_token="vik-test", vikunja_project_id=7)
    client = _client(settings, handler)
    try:
        await client.update_task(5, description="новое описание")
    finally:
        await client._client.aclose()
    posts = [request for request in seen if request.method == "POST"]
    assert len(posts) == 1
    body = json.loads(posts[0].content.decode())
    assert body["due_date"] == "2026-12-01T00:00:00Z"
    assert body["priority"] == 3
    assert body["done"] is False
    assert body["description"] == "новое описание"
    assert body["assignees"] == [{"id": 4, "username": "review"}]
    assert body["is_favorite"] is True
    assert body["reminders"][0]["reminder"] == "2026-11-30T10:00:00Z"
    assert body["repeat_after"] == 86400
    assert body["start_date"] == "2026-11-01T00:00:00Z"
    assert body["end_date"] == "2026-11-02T00:00:00Z"
    assert not any(request.method == "DELETE" for request in seen)
