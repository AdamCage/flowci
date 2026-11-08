"""Регрессии R01–R07 и P2. Живая Vikunja не используется."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import httpx

from flowci.agent import AgentResult
from flowci.cli import main
from flowci.formatting import (
    STATUS_DOING,
    STATUS_TODO,
    apply_header,
    classify_due,
    format_plan,
    parse_description,
    strip_tags,
)
from flowci.schedule import ReminderScheduler
from flowci.store import Store
from flowci.telegram import build_dispatcher
from flowci.vikunja import Task, VikunjaClient
from tests.fakes import NOW, decision, make_agent, make_settings
from tests.test_review_integrity import TelegramFixture


def test_explicit_due_operation_ignores_experiment_and_attachment_dates() -> None:
    today = NOW.date()
    kind, iso = classify_due(
        "Дополни задачу #1: результаты эксперимента за 2026-11-01 готовы. Дедлайн не меняй.",
        today,
    )
    assert kind == "keep" and iso is None
    kind, iso = classify_due("Поставь задачу, дедлайн 2026-12-01", today)
    assert kind == "set" and iso == "2026-12-01"
    kind, iso = classify_due("перенеси на 2026-12-02", today)
    assert kind == "set" and iso == "2026-12-02"
    attachment = "Комментарий: приложи отчёт\nСодержимое:\nотчёт за 2026-11-01 и дедлайн 2030-01-01"
    assert classify_due(attachment, today) == ("absent", None)


async def test_progress_date_does_not_move_deadline(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="append", task_id=1, payload={"next_step": "замерить p95"}),
    )
    vikunja.seed(Task(id=1, title="Эксперимент", due_date="2026-11-25", labels=[STATUS_DOING]))
    result = await agent.handle(
        "Дополни задачу #1: результаты эксперимента за 2026-11-01 готовы. "
        "Следующий шаг — замерить p95. Дедлайн не меняй.",
        source="telegram",
        chat_id=1,
    )
    assert result.action == "append"
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "model-due",
        decision(
            action="append",
            task_id=1,
            payload={"next_step": "замерить p95", "due_date": "2026-11-01"},
        ),
    )
    vikunja.seed(Task(id=1, title="Эксперимент", due_date="2026-11-25", labels=[STATUS_DOING]))
    await agent.handle(
        "результаты за 2026-11-01 готовы. Дедлайн не меняй.",
        source="cli",
    )
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()


async def test_attachment_body_date_is_not_a_deadline(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="append", task_id=1, payload={"next_step": "прочитать отчёт", "due_date": "2026-11-01"}),
    )
    vikunja.seed(Task(id=1, title="Отчёт", due_date="2026-11-25", labels=[STATUS_TODO]))
    text = "Файл от пользователя.\nКомментарий: дополни задачу\nСодержимое:\nотчёт за 2026-11-01"
    await agent.handle(text, source="telegram", chat_id=1)
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()


async def test_confirming_existing_card_does_not_move_due_or_confirm(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(
            action="create",
            payload={
                "title": "Compare Atlas embeddings",
                "assignee": "я",
                "due_date": "2030-01-01",
                "confirmed_result": "нельзя",
                "comment": "IDE proposes a task",
            },
        ),
    )
    vikunja.seed(
        Task(id=1, title="Compare Atlas embeddings", due_date="2026-11-20", labels=[STATUS_DOING])
    )
    asked = await agent.handle_event(
        {
            "source": "cursor",
            "deduplication_key": "existing-task-link",
            "task_hint": "Compare Atlas embeddings",
            "facts": ["new activity"],
        }
    )
    assert asked.action == "ask"
    reply = await agent.handle("Да, дополнить её.", source="telegram", chat_id=111, user_id=111)
    assert reply.action == "append"
    assert vikunja.tasks[1].due_date == "2026-11-20"
    fields, _body = parse_description(vikunja.tasks[1].description)
    assert fields["Последний подтверждённый результат"] in {"", "—"}
    assert llm.calls == 1
    store.close()


async def test_task_number_reply_does_not_confirm_without_file(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="ask", user_message="К какой задаче отнести результат?"),
    )
    vikunja.seed(
        Task(
            id=1,
            title="Measured Atlas task",
            description="Ожидаемый результат: report\nСледующий шаг: measure\nПоследний подтверждённый результат: —",
            labels=[STATUS_DOING],
        )
    )
    await agent.handle_event(
        {
            "source": "cursor",
            "deduplication_key": "resume-evidence",
            "task_hint": "Atlas",
            "facts": ["Сессия заявляет latency 42 мс"],
            "artifacts": [],
        }
    )
    llm.decision = decision(
        action="append",
        task_id=1,
        payload={"confirmed_result": "Unverified harness latency 42 ms", "next_step": "continue"},
    )
    result = await agent.handle("Это задача #1.", source="telegram", chat_id=111, user_id=111)
    assert result.action == "append"
    fields, _body = parse_description(vikunja.tasks[1].description)
    assert fields["Последний подтверждённый результат"] in {"", "—"}
    assert json.loads(llm.last_user)["source"] == "cursor"
    store.close()


async def test_parallel_same_key_creates_one_card(tmp_path) -> None:
    payload = {"title": "Concurrent new work", "assignee": "я", "due_date": "2026-11-25", "comment": "original"}
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="create", payload=payload))

    async def slow(system: str, user: str) -> str:
        await asyncio.sleep(0.05)
        llm.calls += 1
        return json.dumps(llm.decision, ensure_ascii=False)

    llm.complete = slow  # type: ignore[method-assign]
    llm.calls = 0
    event = {"source": "cursor", "deduplication_key": "same-key", "facts": ["new task"], "task_hint": "Concurrent new work"}
    first, second = await asyncio.gather(agent.handle_event(event), agent.handle_event(event))
    messages = {first.user_message, second.user_message}
    assert len(vikunja.tasks) == 1
    assert llm.calls == 1
    assert any("already processing" in message for message in messages)
    assert store.event_stage("same-key") == "done"
    store.close()


async def test_expired_lease_resumes_same_card(tmp_path) -> None:
    payload = {"title": "Recovered", "assignee": "я", "due_date": "2026-11-25"}
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="create", payload=payload))
    vikunja.seed(
        Task(
            id=4,
            title="Recovered",
            description="flowci-event:expired-key",
            due_date="2026-11-25",
            labels=[STATUS_TODO],
        )
    )
    store.open_event("expired-key", {"text": "Recovered", "source": "cursor", "extra": {}, "allow_completion": False})
    store._conn.execute(
        "UPDATE dedupe SET task_id = 4, stage = 'claimed', lease_until = '2000-01-01T00:00:00+00:00', "
        "payload_json = ? WHERE deduplication_key = 'expired-key'",
        (
            json.dumps(
                {
                    "action": "create",
                    "title": "Recovered",
                    "created": True,
                    "labels": [STATUS_TODO],
                    "labels_done": True,
                    "assign_mode": "me",
                    "assign_done": False,
                    "pending_comment": "[предположение] источник: cursor\noriginal",
                    "comment_done": False,
                },
                ensure_ascii=False,
            ),
        ),
    )
    store._conn.commit()
    store.release_held_claims()
    result = await agent.handle("again", source="cli", deduplication_key="expired-key")
    assert result.task_id == 4
    assert len(vikunja.tasks) == 1
    assert llm.calls == 0
    store.close()


async def test_label_failure_after_put_retries_same_card(tmp_path) -> None:
    tasks: list[dict] = []
    labels_down = {"value": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/projects/7/tasks":
            return httpx.Response(200, json=tasks)
        if request.method == "PUT" and request.url.path == "/api/v1/projects/7/tasks":
            body = json.loads(request.content.decode())
            task = {"id": 8, "project_id": 7, "labels": [], **body}
            tasks.append(task)
            return httpx.Response(200, json=task)
        if request.method == "GET" and request.url.path == "/api/v1/tasks/8":
            return httpx.Response(200, json=tasks[0])
        if request.method == "GET" and request.url.path == "/api/v1/labels":
            return httpx.Response(200, json=[])
        if request.method == "PUT" and request.url.path == "/api/v1/labels":
            if labels_down["value"]:
                return httpx.Response(500, json={"message": "temporary label outage"})
            return httpx.Response(200, json={"id": 3, "title": json.loads(request.content.decode())["title"]})
        if request.method == "PUT" and request.url.path == "/api/v1/tasks/8/labels":
            title = "Нужно сделать"
            tasks[0]["labels"] = [{"id": 3, "title": title}]
            return httpx.Response(200, json={"label_id": 3})
        if request.method == "GET" and request.url.path == "/api/v1/user":
            return httpx.Response(200, json={"id": 1, "username": "me"})
        if request.method == "PUT" and request.url.path == "/api/v1/tasks/8/assignees":
            return httpx.Response(200, json={"user_id": 1})
        if request.method == "PUT" and request.url.path == "/api/v1/tasks/8/comments":
            return httpx.Response(200, json={"id": 1, "comment": json.loads(request.content.decode())["comment"]})
        return httpx.Response(404, json={"message": request.url.path})

    settings = make_settings(tmp_path, vikunja_project_id=7)
    client = VikunjaClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    from tests.fakes import FakeLLM

    llm = FakeLLM(
        decision(
            action="create",
            payload={
                "title": "Partially labelled creation",
                "assignee": "я",
                "due_date": "2026-11-20",
                "comment": "original evidence",
            },
        )
    )
    store = Store(tmp_path / "flowci.db")
    from flowci.agent import TaskAgent

    agent = TaskAgent(settings, store, client, llm, clock=lambda: NOW)
    first = await agent.handle("new task, дедлайн 2026-11-20", source="cli", deduplication_key="label-failure")
    assert first.ok is False
    assert first.task_id == 8
    assert store.event_stage("label-failure") == "task_ready"
    assert len(tasks) == 1
    labels_down["value"] = False
    second = await agent.handle("new task", source="cli", deduplication_key="label-failure")
    assert second.ok is True
    assert second.task_id == 8
    assert len(tasks) == 1
    assert llm.calls == 1
    assert any(label.get("title") == "Нужно сделать" for label in tasks[0]["labels"])
    await client.aclose()
    store.close()


async def test_lost_put_response_finds_card_by_marker(tmp_path) -> None:
    saved: list[dict] = []
    failed = {"value": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/projects/7/tasks":
            return httpx.Response(200, json=saved)
        if request.method == "PUT" and request.url.path == "/api/v1/projects/7/tasks":
            body = json.loads(request.content.decode())
            task = {"id": 6, "project_id": 7, "labels": [], **body}
            saved.append(task)
            if failed["value"]:
                failed["value"] = False
                raise httpx.ReadTimeout("lost response")
            return httpx.Response(200, json=task)
        if request.method == "GET" and request.url.path == "/api/v1/tasks/6":
            return httpx.Response(200, json=saved[0])
        if request.method == "GET" and request.url.path == "/api/v1/labels":
            return httpx.Response(200, json=[])
        if request.method == "PUT" and request.url.path == "/api/v1/labels":
            return httpx.Response(200, json={"id": 2, "title": "Нужно сделать"})
        if request.method == "PUT" and "/labels" in request.url.path:
            saved[0]["labels"] = [{"id": 2, "title": "Нужно сделать"}]
            return httpx.Response(200, json={"label_id": 2})
        if request.method == "GET" and request.url.path == "/api/v1/user":
            return httpx.Response(200, json={"id": 1, "username": "me"})
        if request.method == "PUT" and request.url.path.endswith("/assignees"):
            return httpx.Response(200, json={"user_id": 1})
        if request.method == "PUT" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json={"id": 1, "comment": "ok"})
        return httpx.Response(404, json={"message": request.url.path})

    settings = make_settings(tmp_path, vikunja_project_id=7)
    client = VikunjaClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    from tests.fakes import FakeLLM
    from flowci.agent import TaskAgent

    llm = FakeLLM(
        decision(action="create", payload={"title": "Timeout card", "assignee": "я", "due_date": "2026-11-20"})
    )
    store = Store(tmp_path / "flowci.db")
    agent = TaskAgent(settings, store, client, llm, clock=lambda: NOW)
    result = await agent.handle("new, дедлайн 2026-11-20", source="cli", deduplication_key="timeout-key")
    assert result.ok is True
    assert result.task_id == 6
    assert len(saved) == 1
    assert "flowci-event:timeout-key" in saved[0]["description"]
    assert llm.calls == 1
    await client.aclose()
    store.close()


async def test_two_clients_keep_description_and_done(tmp_path) -> None:
    state = {
        "id": 1,
        "title": "Shared card",
        "description": "Original description",
        "done": False,
        "labels": [],
        "assignees": [],
        "reminders": [],
        "is_favorite": False,
        "project_id": 7,
        "priority": 0,
        "due_date": "2026-12-01T00:00:00Z",
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            snapshot = copy.deepcopy(state)
            await asyncio.sleep(0.05)
            return httpx.Response(200, json=snapshot)
        if request.method == "POST":
            body = json.loads(request.content.decode())
            state.update(body)
            return httpx.Response(200, json=copy.deepcopy(state))
        return httpx.Response(404)

    settings = make_settings(tmp_path, vikunja_project_id=7)
    transport = httpx.MockTransport(handler)
    one = VikunjaClient(settings, client=httpx.AsyncClient(transport=transport))
    two = VikunjaClient(settings, client=httpx.AsyncClient(transport=transport))
    await asyncio.gather(
        one.update_task(1, description="New independent description"),
        two.update_task(1, done=True),
    )
    assert state["description"] == "New independent description"
    assert state["done"] is True
    await one.aclose()
    await two.aclose()


async def test_three_model_questions_keep_original_request(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="ask", user_message="Кто исполнитель?"),
    )
    original = (
        "Создай задачу QuasarCX720. Ожидаемый результат RESULT_QCX720. Следующий шаг STEP_QCX720."
    )
    first = await agent.handle(original, source="telegram", chat_id=12)
    assert first.action == "ask"
    llm.decision = decision(action="ask", user_message="К какому сроку?")
    second = await agent.handle("я", source="telegram", chat_id=12)
    assert second.action == "ask"
    saved = store.get_dialog("12")
    assert saved is not None
    assert saved["original_text"] == original
    assert "я" in saved["answers"]
    llm.complete = _boom  # type: ignore[method-assign]
    failed = await agent.handle("уточни ещё", source="telegram", chat_id=12)
    assert failed.ok is False
    saved = store.get_dialog("12")
    assert saved is not None
    assert saved["original_text"] == original
    assert saved["answers"] == ["я", "уточни ещё"]
    llm.complete = llm.__class__.complete.__get__(llm, llm.__class__)  # type: ignore[attr-defined]
    llm.decision = decision(
        action="create",
        payload={"title": "QuasarCX720", "assignee": "я", "due_date": "2026-11-21", "expected_result": "RESULT_QCX720", "next_step": "STEP_QCX720"},
    )

    async def restore(system: str, user: str) -> str:
        llm.calls += 1
        llm.last_user = user
        return json.dumps(llm.decision, ensure_ascii=False)

    llm.complete = restore  # type: ignore[method-assign]
    done = await agent.handle("до 2026-11-21", source="telegram", chat_id=12)
    assert done.action == "create"
    prompt = json.loads(llm.last_user)["text"]
    assert "QuasarCX720" in prompt
    assert "RESULT_QCX720" in prompt
    assert "STEP_QCX720" in prompt
    assert "я" in prompt
    task = next(iter(vikunja.tasks.values()))
    assert task.title == "QuasarCX720"
    fields, _body = parse_description(task.description)
    assert "RESULT_QCX720" in fields["Ожидаемый результат"]
    assert "STEP_QCX720" in fields["Следующий шаг"]
    store.close()


async def _boom(system: str, user: str) -> str:
    raise RuntimeError("model down")


async def test_reply_to_first_question_leaves_the_second_open(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="ask", user_message="Вопрос Cedar"))
    sent: list[tuple[int, str]] = []

    async def send(text: str) -> int:
        sent.append((len(sent) + 1, text))
        return len(sent)

    agent.outbox_sender = send
    await agent.handle_event({"source": "cursor", "deduplication_key": "event-A", "facts": ["Cedar"], "task_hint": "Cedar"})
    llm.decision = decision(action="ask", user_message="Вопрос Delta")
    await agent.handle_event({"source": "cursor", "deduplication_key": "event-B", "facts": ["Delta"], "task_hint": "Delta"})
    assert [item[0] for item in sent] == [1, 2]
    ambiguous = await agent.handle("я, до 2026-11-25", source="telegram", chat_id=111, user_id=111)
    assert ambiguous.action == "ask"
    assert "Несколько вопросов" in ambiguous.user_message
    assert vikunja.tasks == {}
    llm.decision = decision(
        action="create",
        payload={"title": "Первое поручение Cedar", "assignee": "я", "due_date": "2026-11-25"},
    )
    answered = await agent.handle(
        "я, до 2026-11-25",
        source="telegram",
        chat_id=111,
        user_id=111,
        reply_to=1,
    )
    assert answered.action == "create"
    assert [task.title for task in vikunja.tasks.values()] == ["Первое поручение Cedar"]
    assert store.event_stage("event-A") == "done"
    assert store.event_stage("event-B") == "awaiting_input"
    store.close()


async def test_cancel_drops_undelivered_question_and_repeat_does_not_enqueue(tmp_path) -> None:
    agent, _vik, llm, store = make_agent(tmp_path, decision(action="ask", user_message="Какой срок для Cedar?"))
    sent: list[str] = []

    async def send(text: str) -> bool:
        sent.append(text)
        return True

    await agent.handle_event({"source": "cursor", "deduplication_key": "cancel-me", "task_hint": "Cedar", "facts": ["новая обязанность"]})
    assert len(store.pending_outbox()) == 1
    cancelled = await agent.handle("отмена", source="telegram", chat_id=111, user_id=111)
    assert cancelled.action == "note"
    scheduler = ReminderScheduler(agent.settings, store, agent, send)
    await scheduler._dispatch_outbox()
    assert sent == []
    assert store.pending_outbox() == []
    store.close()

    agent, _vik, llm, store = make_agent(tmp_path / "repeat", decision(action="ask", user_message="Какой срок для Cedar?"))
    sent.clear()
    agent.outbox_sender = send
    event = {"source": "cursor", "deduplication_key": "repeat-question", "facts": ["новая обязанность"]}
    await agent.handle_event(event)
    await agent.handle_event(event)
    assert sent == ["Какой срок для Cedar?"]
    assert llm.calls == 1
    store.close()


def test_legacy_sent_reminder_is_not_pending(tmp_path) -> None:
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE reminders ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, fire_at TEXT NOT NULL, text TEXT NOT NULL, "
        "task_id INTEGER, sent INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, "
        "created_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO reminders(fire_at, text, sent, attempts, created_at) "
        "VALUES ('2000-01-01T00:00:00+00:00', 'already delivered', 1, 1, '2000-01-01T00:00:00+00:00')"
    )
    conn.execute("ALTER TABLE reminders ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'")
    conn.commit()
    conn.close()
    store = Store(db)
    assert store.reminder_status(1) == "sent"
    assert store.due_reminders("2026-11-08T12:00:00+00:00") == []
    store.close()


async def test_no_recipient_does_not_consume_attempts(tmp_path) -> None:
    agent, _vik, _llm, store = make_agent(tmp_path, decision())
    reminder_id = store.add_reminder("2000-01-01T00:00:00+00:00", "WAIT FOR OWNER", None)

    async def nobody(text: str):
        return None

    scheduler = ReminderScheduler(agent.settings, store, agent, nobody)
    for _ in range(3):
        await scheduler._dispatch_reminders()
    assert store.reminder_status(reminder_id) == "pending"
    row = store._conn.execute("SELECT attempts FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert int(row["attempts"]) == 0

    async def ready(text: str) -> bool:
        return True

    scheduler = ReminderScheduler(agent.settings, store, agent, ready)
    await scheduler._dispatch_reminders()
    assert store.reminder_status(reminder_id) == "sent"
    store.close()

    agent, _vik, _llm, store = make_agent(tmp_path / "transport", decision())
    reminder_id = store.add_reminder("2000-01-01T00:00:00+00:00", "boom", None)
    calls = {"n": 0}

    async def fail(text: str) -> bool:
        calls["n"] += 1
        raise RuntimeError("telegram down")

    scheduler = ReminderScheduler(agent.settings, store, agent, fail)
    for _ in range(4):
        await scheduler._dispatch_reminders()
    assert calls["n"] == 3
    assert store.reminder_status(reminder_id) == "failed"
    store.close()


async def test_parallel_confirmation_runs_once(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="change_status", task_id=1, payload={"status": "Завершено"}),
    )
    vikunja.seed(Task(id=1, title="Close once", labels=[STATUS_DOING]))
    asked = await agent.handle("закрой задачу #1", source="telegram", chat_id=111, user_id=111)
    token = asked.confirmation_token or ""
    first, second = await asyncio.gather(agent.confirm(token, 111), agent.confirm(token, 111))
    actions = [first.action, second.action]
    assert actions.count("change_status") == 1
    assert actions.count("ask") == 1
    assert len([call for call in vikunja.calls if call[0] == "update_task"]) == 1
    assert len(vikunja.comments) == 1
    assert len([item for item in store.list_audit() if item[0] == "change_status"]) == 1
    store.close()


def test_corrupt_session_does_not_block_outbox(tmp_path, monkeypatch) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("flowci_task_sync_v2", root / ".cursor" / "hooks" / "task-sync.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    (tmp_path / ".flowci").mkdir()
    (tmp_path / ".env").write_text("FLOWCI_INGEST_TOKEN=review-only-token\nFLOWCI_INGEST_PORT=9\n", encoding="utf-8")
    event = hook.build_event(
        {"session_id": "s", "facts": ["queued"], "task_hint": "Уже в очереди", "project": "P"},
        "",
        {},
        NOW,
    )
    hook.write_outbox(tmp_path, event)
    (tmp_path / ".flowci" / "session.json").write_text("{ incomplete", encoding="utf-8")
    attempted: list[str] = []

    def accept(url: str, token: str, item: dict) -> dict:
        attempted.append(str(item["deduplication_key"]))
        return {"ok": True}

    monkeypatch.setattr(hook, "find_root", lambda _start: tmp_path)
    monkeypatch.setattr(hook, "post_event", accept)
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO("{}\n"))
    monkeypatch.setattr(hook.sys, "stdout", io.StringIO())
    monkeypatch.setattr(hook.sys, "stderr", io.StringIO())
    assert hook.main() == 0
    assert attempted == [event["deduplication_key"]]
    assert hook.load_outbox(tmp_path) == []


def test_legacy_today_label_stays_without_planned_field() -> None:
    legacy = Task(
        id=1,
        title="Legacy task planned Today",
        due_date="2026-12-01",
        labels=["Сегодня", "В работе"],
        assignees=["me"],
        description="Ожидаемый результат: таблица\nСледующий шаг: замер\nПоследний подтверждённый результат: —",
    )
    plan = format_plan([legacy], "today", today=NOW.date(), owner="me")
    assert legacy.title in plan
    due_only = Task(id=2, title="Only a deadline", due_date=NOW.date().isoformat(), labels=[STATUS_TODO], assignees=["me"])
    labeled = format_plan([legacy, due_only], "today", today=NOW.date(), owner="me")
    assert legacy.title in labeled


def test_html_step_replaces_old_paragraph_and_model_sees_text(tmp_path) -> None:
    html = (
        "<p>Ожидаемый результат: NATIVE_TARGET_948</p>"
        "<p>Следующий шаг: NATIVE_STEP_842</p>"
        "<p>Последний подтверждённый результат: NATIVE_RESULT_124</p>"
    )
    fields, _body = parse_description(html)
    assert fields["Ожидаемый результат"] == "NATIVE_TARGET_948"
    assert "NATIVE_TARGET_948" in strip_tags(html)
    updated = apply_header(html, {"next_step": "New structured step"})
    assert "NATIVE_STEP_842" not in updated
    assert "New structured step" in updated
    assert updated.count("Следующий шаг") == 1
    assert "NATIVE_TARGET_948" in updated
    assert "<p>" in updated

    async def run() -> None:
        agent, vik, llm, store = make_agent(tmp_path, decision(action="find", task_id=1))
        vik.seed(Task(id=1, title="Native HTML task", description=html, assignees=["me"]))
        await agent.handle("Найди задачу #1", source="cli")
        assert "NATIVE_TARGET_948" in llm.last_user
        assert "NATIVE_STEP_842" in llm.last_user
        assert "<p>" not in json.loads(llm.last_user)["tasks"][0]["description_text"]
        store.close()

    asyncio.run(run())


async def test_each_allowlisted_user_has_a_private_chat(tmp_path) -> None:
    fixture = TelegramFixture(tmp_path, decision(action="plan", payload={"horizon": "today"}))
    fixture.agent.settings = replace(fixture.agent.settings, tg_allowed_user_ids=(111, 222))
    fixture.dp = build_dispatcher(fixture.agent.settings, fixture.store, fixture.agent)
    fixture.vik.seed(Task(id=1, title="PRIVATE REVIEW TASK", labels=["Сегодня"], description="Следующий шаг: живой"))
    try:
        await fixture.message("/start", user=111, chat=111)
        await fixture.message("/start", user=222, chat=222)
        assert any(item["chat_id"] == 222 and "FlowCI" in item["text"] for item in fixture.replies())
        before = len(fixture.replies())
        calls = fixture.llm.calls
        await fixture.message("покажи план на сегодня", user=222, chat=222)
        assert fixture.llm.calls > calls
        assert any(item["chat_id"] == 222 and "PRIVATE REVIEW TASK" in item["text"] for item in fixture.replies()[before:])
        before = len(fixture.replies())
        await fixture.message("покажи план", user=222, chat=-100)
        assert fixture.replies()[before:] == []
        stranger = len(fixture.replies())
        await fixture.message("покажи план", user=333, chat=333)
        assert fixture.replies()[stranger:] == []
    finally:
        await fixture.close()


def test_cli_failure_still_exits_nonzero(monkeypatch, capsys) -> None:
    async def failed(*args, **kwargs):
        return AgentResult(action="ask", user_message="Vikunja сейчас недоступна.", ok=False)

    monkeypatch.setattr("flowci.cli._run", failed)
    assert main(["план", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_new_task_still_accepts_explicit_deadline() -> None:
    assert classify_due("Создай задачу себе до 2026-11-15", NOW.date()) == ("set", "2026-11-15")
