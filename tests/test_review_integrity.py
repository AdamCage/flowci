"""Регрессии по ревью целостности. Исторические скрипты в reviews/evidence не меняются."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.types import File, Message, Update

from flowci.agent import AgentResult
from flowci.cli import main
from flowci.formatting import (
    STATUS_DOING,
    STATUS_TODO,
    apply_header,
    parse_description,
    render_description,
)
from flowci.schedule import ReminderScheduler
from flowci.telegram import _read_text_attachment, build_dispatcher
from flowci.vikunja import Task, VikunjaClient, VikunjaError
from tests.fakes import NOW, decision, make_agent, make_settings


def _header(step: str = "старый шаг", result: str = "было") -> str:
    return render_description(
        {
            "Ожидаемый результат": result,
            "Следующий шаг": step,
            "Последний подтверждённый результат": "—",
        },
        "Свободный текст.",
    )


class BarrierLLM:
    def __init__(self) -> None:
        self.count = 0
        self.both = asyncio.Event()

    async def complete(self, system: str, user: str) -> str:
        self.count += 1
        if self.count == 2:
            self.both.set()
        await self.both.wait()
        text = json.loads(user)["text"]
        if text == "step":
            return json.dumps(
                decision(action="append", task_id=1, payload={"next_step": "New concurrent next step"}),
                ensure_ascii=False,
            )
        return json.dumps(
            decision(action="change_status", task_id=1, payload={"status": STATUS_DOING}),
            ensure_ascii=False,
        )

    async def transcribe(self, path: Path) -> str:
        return ""


class CaptureSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict]] = []
        self.fail_get_file = False
        self.document_bytes = b"OMEGA_8421 executor me due 2026-11-13"

    async def close(self) -> None:
        return None

    async def make_request(self, bot, method, timeout=None):
        api = method.__api_method__
        data = method.model_dump(exclude_none=True)
        self.calls.append((api, data))
        if api == "getFile":
            if self.fail_get_file:
                raise RuntimeError("offline download failure")
            return File(file_id=data["file_id"], file_unique_id="unique", file_path="review/document.txt")
        if api == "sendMessage":
            chat_id = data["chat_id"]
            return Message(
                message_id=99,
                date=NOW,
                chat={"id": chat_id, "type": "private" if int(chat_id) > 0 else "group"},
                text=data.get("text", ""),
            )
        return True

    async def stream_content(self, *args, **kwargs):
        yield self.document_bytes


class TelegramFixture:
    def __init__(self, tmp_path: Path, model: dict) -> None:
        self.agent, self.vik, self.llm, self.store = make_agent(tmp_path, model)
        self.session = CaptureSession()
        self.bot = Bot(token="123456:offline_fake_token_review", session=self.session)
        self.dp = build_dispatcher(self.agent.settings, self.store, self.agent)

    async def close(self) -> None:
        await self.bot.session.close()
        self.store.close()

    async def message(self, text=None, user=111, chat=111, document=False, file_name="request.txt", reply_to=None):
        data = dict(
            message_id=1,
            date=NOW,
            chat={"id": chat, "type": "private" if chat > 0 else "group"},
            from_user={"id": user, "is_bot": False, "first_name": "Owner"},
        )
        if reply_to is not None:
            data["reply_to_message"] = {
                "message_id": int(reply_to),
                "date": NOW,
                "chat": {"id": chat, "type": "private" if chat > 0 else "group"},
                "from_user": {"id": 1, "is_bot": True, "first_name": "FlowCI"},
                "text": "question",
            }
        if text is not None:
            data["text"] = text
            if text.startswith("/start"):
                data["entities"] = [{"type": "bot_command", "offset": 0, "length": 6}]
        if document:
            data["document"] = {
                "file_id": "document",
                "file_unique_id": "review-doc",
                "file_name": file_name,
                "mime_type": "text/plain",
                "file_size": len(self.session.document_bytes),
            }
            data["caption"] = "Создай задачу мне к 2026-11-13 из вложения"
        return await self.dp.feed_update(self.bot, Update(update_id=1, message=Message(**data)))

    async def callback(self, data: str, user: int = 111, chat: int = 111):
        return await self.dp.feed_update(
            self.bot,
            Update.model_validate(
                {
                    "update_id": 2,
                    "callback_query": {
                        "id": "callback",
                        "from": {"id": user, "is_bot": False, "first_name": "Owner"},
                        "chat_instance": "review",
                        "data": data,
                        "message": {
                            "message_id": 9,
                            "date": int(NOW.timestamp()),
                            "chat": {"id": chat, "type": "private" if chat > 0 else "group"},
                            "text": "Confirm",
                        },
                    },
                }
            ),
        )

    def replies(self) -> list[dict]:
        return [
            {"chat_id": item["chat_id"], "text": item.get("text", "")}
            for api, item in self.session.calls
            if api == "sendMessage"
        ]


async def test_update_keeps_assignees_favorite_and_reminders(tmp_path) -> None:
    state = {
        "task": {
            "id": 5,
            "title": "Сохранить поля",
            "description": _header(),
            "done": False,
            "priority": 2,
            "due_date": "2026-12-01T00:00:00Z",
            "project_id": 7,
            "assignees": [{"id": 4, "username": "review"}],
            "reminders": [{"reminder": "2026-11-30T10:00:00Z", "relative_period": 0}],
            "is_favorite": True,
            "repeat_after": 0,
            "repeat_mode": 0,
            "start_date": "2026-11-01T00:00:00Z",
            "end_date": "2026-11-02T00:00:00Z",
            "labels": [{"id": 1, "title": "В работе"}, {"id": 2, "title": "Сегодня"}],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/tasks/5":
            return httpx.Response(200, json=state["task"])
        if request.method == "POST" and request.url.path == "/api/v1/tasks/5":
            body = json.loads(request.content.decode())
            state["task"] = {
                **state["task"],
                "title": body.get("title", ""),
                "description": body.get("description", ""),
                "done": body.get("done", False),
                "priority": body.get("priority", 0),
                "due_date": body.get("due_date"),
                "assignees": body["assignees"] if "assignees" in body else None,
                "reminders": body["reminders"] if "reminders" in body else None,
                "is_favorite": body["is_favorite"] if "is_favorite" in body else False,
                "repeat_after": body.get("repeat_after", 0),
                "repeat_mode": body.get("repeat_mode", 0),
                "start_date": body.get("start_date"),
                "end_date": body.get("end_date"),
                "labels": body.get("labels", state["task"]["labels"]),
            }
            return httpx.Response(200, json=state["task"])
        return httpx.Response(404, text=request.url.path)

    settings = make_settings(tmp_path, vikunja_token="vik-test", vikunja_project_id=7)
    client = VikunjaClient(
        settings,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), headers={"Authorization": "Bearer vik-test"}),
    )
    try:
        saved = await client.update_task(5, header={"next_step": "только шаг"})
    finally:
        await client._client.aclose()
    assert saved.assignees == ["review"]
    assert state["task"]["is_favorite"] is True
    assert state["task"]["reminders"][0]["reminder"].startswith("2026-11-30T10:00:00")
    assert "только шаг" in saved.description
    assert "Сегодня" in saved.labels
    assert "В работе" in saved.labels


async def test_concurrent_append_and_status_both_survive(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(tmp_path, decision())
    vikunja.seed(Task(id=1, title="Одновременное", labels=[STATUS_TODO], description=_header()))
    agent.llm = BarrierLLM()
    step, status = await asyncio.gather(agent.handle("step"), agent.handle("status"))
    task = vikunja.tasks[1]
    fields, _body = parse_description(task.description)
    assert step.ok and status.ok
    assert STATUS_DOING in task.labels
    assert fields["Следующий шаг"] == "New concurrent next step"
    store.close()


async def test_two_and_three_turn_dialog(tmp_path) -> None:
    payload = {
        "title": "Подготовить демонстрацию Cedar",
        "expected_result": "Показ",
        "next_step": "Собрать стенд",
    }
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="create", payload=payload))
    first = await agent.handle("Создай задачу «Подготовить демонстрацию Cedar»", source="telegram", chat_id=7)
    assert first.action == "ask"
    assert vikunja.tasks == {}
    second = await agent.handle("Я, до 2026-11-21. План на завтра", source="telegram", chat_id=7)
    assert second.action == "create"
    assert llm.calls == 1
    task = next(iter(vikunja.tasks.values()))
    assert task.title == "Подготовить демонстрацию Cedar"
    assert str(task.due_date).startswith("2026-11-21")
    assert task.assignees == ["me"]
    assert "Завтра" in task.labels
    assert "2026-11-09" in task.description
    store.close()

    agent, vikunja, llm, store = make_agent(tmp_path / "three", decision(action="create", payload=payload))
    assert (await agent.handle("Создай задачу Cedar", source="telegram", chat_id=8)).action == "ask"
    middle = await agent.handle("я", source="telegram", chat_id=8)
    assert middle.action == "ask"
    assert "срок" in middle.user_message
    assert vikunja.tasks == {}
    done = await agent.handle("до 2026-11-21", source="telegram", chat_id=8)
    assert done.action == "create"
    assert llm.calls == 1
    assert str(next(iter(vikunja.tasks.values())).due_date).startswith("2026-11-21")
    store.close()

    agent, vikunja, llm, store = make_agent(
        tmp_path / "choice",
        decision(
            action="append",
            payload={"next_step": "снять baseline", "candidates": [1, 2]},
            user_message="Какую дополнить?",
        ),
    )
    vikunja.seed(Task(id=1, title="Orion поиск", labels=[STATUS_TODO], description=_header()))
    vikunja.seed(Task(id=2, title="Orion каталог", labels=[STATUS_TODO], description=_header()))
    asked = await agent.handle("Дополни Orion", source="telegram", chat_id=9)
    assert asked.action == "ask"
    chosen = await agent.handle("Для поиска, задача #2", source="telegram", chat_id=9)
    assert chosen.action == "append"
    assert chosen.task_id == 2
    assert llm.calls == 1
    assert "снять baseline" in vikunja.tasks[2].description
    assert "снять baseline" not in vikunja.tasks[1].description
    store.close()


async def test_event_ask_is_not_noop_and_comment_retry_uses_same_card(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="ask", user_message="Какой срок?"))
    event = {"source": "cursor", "deduplication_key": "ask-1", "facts": ["нужен срок"], "task_hint": "Отчёт"}
    first = await agent.handle_event(event)
    second = await agent.handle_event(event)
    assert first.action == "ask" and first.noop is False
    assert second.action == "ask" and second.noop is False
    assert "уже обработано" not in second.user_message
    assert llm.calls == 1
    assert store.event_stage("ask-1") == "awaiting_input"
    assert store.pending_outbox()
    assert store.pending_outbox()[0]["text"] == "Какой срок?"
    store.close()

    payload = {"title": "Частичная", "assignee": "я", "due_date": "2026-11-21", "comment": "исходный комментарий"}
    agent, vikunja, llm, store = make_agent(tmp_path / "comment", decision(action="create", payload=payload))
    original = vikunja.add_comment
    state = {"failed": False}

    async def flaky(task_id: int, text: str):
        if not state["failed"]:
            state["failed"] = True
            raise VikunjaError(500, "comment down")
        return await original(task_id, text)

    vikunja.add_comment = flaky
    failed = await agent.handle("Новая обязанность, дедлайн 2026-11-21", source="cli", deduplication_key="partial-1")
    assert failed.ok is False
    assert len(vikunja.tasks) == 1
    assert vikunja.comments == []
    retried = await agent.handle("Новая обязанность", source="cli", deduplication_key="partial-1")
    assert retried.ok is True
    assert len(vikunja.tasks) == 1
    assert retried.task_id == failed.task_id
    assert any("исходный комментарий" in comment.text for comment in vikunja.comments)
    assert llm.calls == 1
    third = await agent.handle("ещё раз", source="cli", deduplication_key="partial-1")
    assert third.noop is True
    store.close()


async def test_telegram_reply_resumes_event_question(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="ask", user_message="Кто исполнитель и какой срок?"),
    )
    event = {
        "source": "cursor",
        "deduplication_key": "ask-bind",
        "task_hint": "Подготовить Cedar",
        "facts": ["нужны исполнитель и срок"],
    }
    asked = await agent.handle_event(event)
    assert asked.action == "ask" and asked.noop is False
    llm.decision = decision(
        action="create",
        payload={"title": "Подготовить Cedar", "assignee": "я", "due_date": "2026-11-21"},
    )
    store.bind_owner(111, ())
    store.bind_owner_chat(111)
    resumed = await agent.handle("я, до 2026-11-21", source="telegram", chat_id=111, user_id=111)
    assert resumed.action == "create"
    assert resumed.task_id is not None
    assert "Cedar" in vikunja.tasks[resumed.task_id].title
    assert store.event_stage("ask-bind") == "done"
    assert llm.calls == 2
    assert "Подготовить Cedar" in llm.last_user
    store.close()


async def test_stale_inflight_resumes(tmp_path) -> None:
    agent, _vik, llm, store = make_agent(tmp_path, decision(action="note", payload={"text": "восстановили"}))
    store.claim_dedupe("interrupted-event")
    store.close()
    reopened = make_agent(tmp_path, decision(action="note", payload={"text": "восстановили"}))
    agent, _vik, llm, store = reopened
    result = await agent.handle_event(
        {"source": "cursor", "deduplication_key": "interrupted-event", "facts": ["replay"], "task_hint": "Снова"}
    )
    assert result.noop is False
    assert llm.calls == 1
    assert store.event_stage("interrupted-event") == "done"
    store.close()


async def test_group_does_not_receive_plan(tmp_path) -> None:
    fixture = TelegramFixture(tmp_path, decision(action="plan", payload={"horizon": "today"}))
    try:
        fixture.vik.seed(Task(id=1, title="PRIVATE REVIEW TASK", labels=["Сегодня"], description=_header()))
        await fixture.message("/start")
        before = len(fixture.replies())
        await fixture.message("что мне сегодня делать", chat=-1001)
        leaked = [item for item in fixture.replies()[before:] if item["chat_id"] < 0 or "PRIVATE REVIEW TASK" in item["text"]]
        assert leaked == []
        await fixture.message("что мне сегодня делать", chat=111)
        assert any("PRIVATE REVIEW TASK" in item["text"] and item["chat_id"] == 111 for item in fixture.replies())
    finally:
        await fixture.close()


async def test_unsent_reminder_is_not_marked_sent(tmp_path) -> None:
    agent, _vik, _llm, store = make_agent(tmp_path, decision())
    store.add_reminder("2000-01-01T00:00:00+00:00", "не доставлено", None)
    calls = {"n": 0}

    async def fail(text: str) -> bool:
        calls["n"] += 1
        raise RuntimeError("telegram down")

    scheduler = ReminderScheduler(agent.settings, store, agent, fail)
    for _ in range(4):
        await scheduler._dispatch_reminders()
    assert calls["n"] == 3
    assert store.reminder_status(1) == "failed"
    assert store.list_reminders()[0][3] == 0

    store.add_reminder("2000-01-01T00:00:00+00:00", "ждёт владельца", None)
    gate = {"ready": False, "sent": []}

    async def when_owner(text: str) -> bool:
        if not gate["ready"]:
            return False
        gate["sent"].append(text)
        return True

    scheduler = ReminderScheduler(agent.settings, store, agent, when_owner)
    await scheduler._dispatch_reminders()
    assert gate["sent"] == []
    assert store.reminder_status(2) == "pending"
    gate["ready"] = True
    await scheduler._dispatch_reminders()
    assert gate["sent"] == ["ждёт владельца"]
    assert store.reminder_status(2) == "sent"
    store.close()


async def test_confirmation_survives_503_and_records_user(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="change_status", task_id=1, payload={"status": "Завершено"}),
    )
    vikunja.seed(Task(id=1, title="Закрытие", labels=[STATUS_DOING], due_date="2026-12-01T00:00:00Z"))
    pending = await agent.handle("закрой задачу", source="telegram", confirm=False, user_id=111, chat_id=111)
    assert pending.needs_confirmation is True
    token = pending.confirmation_token or ""
    original = vikunja.list_tasks

    async def unavailable():
        raise VikunjaError(503, "down")

    vikunja.list_tasks = unavailable
    failed = await agent.confirm(token, user_id=111)
    assert failed.ok is False
    assert failed.action == "ask"
    assert "Завершено" not in vikunja.tasks[1].labels
    stranger = await agent.confirm(token, user_id=222)
    assert stranger.action == "ask"
    assert "Завершено" not in vikunja.tasks[1].labels
    vikunja.list_tasks = original
    confirmed = await agent.confirm(token, user_id=111)
    assert confirmed.action == "change_status"
    assert "Завершено" in vikunja.tasks[1].labels
    assert vikunja.tasks[1].due_date == "2026-12-01T00:00:00Z"
    audit = store.list_audit()
    assert any(action == "change_status" and "user:111" in summary for action, summary, _task in audit)
    store.close()


async def test_callback_error_is_answered(tmp_path) -> None:
    fixture = TelegramFixture(
        tmp_path,
        decision(action="change_status", task_id=1, payload={"status": "Завершено"}),
    )
    try:
        fixture.vik.seed(Task(id=1, title="Кнопка", labels=[STATUS_TODO]))
        await fixture.message("/start")
        await fixture.message("Закрой задачу 1")
        row = fixture.store._conn.execute("SELECT token FROM pending_confirmations").fetchone()
        assert row is not None
        token = str(row[0])

        async def unavailable():
            raise VikunjaError(503, "down")

        fixture.vik.list_tasks = unavailable
        before = len(fixture.replies())
        await fixture.callback("confirm:" + token)
        assert any("недоступна" in item["text"].lower() or "Не получилось" in item["text"] for item in fixture.replies()[before:])
        assert "Завершено" not in fixture.vik.tasks[1].labels
        status = fixture.store._conn.execute(
            "SELECT status FROM pending_confirmations WHERE token = ?",
            (token,),
        ).fetchone()
        assert status is not None and str(status[0]) == "pending"
    finally:
        await fixture.close()


async def test_tomorrow_and_no_deadline_and_unknown_assignee(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="create", payload={"title": "Отчёт", "assignee": "я"}),
    )
    result = await agent.handle("Поставь задачу «Отчёт», исполнитель я, дедлайн завтра", source="cli")
    assert result.action == "create"
    assert str(next(iter(vikunja.tasks.values())).due_date).startswith("2026-11-09")
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "open",
        decision(action="create", payload={"title": "Изучить алгоритмы", "assignee": "я", "plan": "Позже"}),
    )
    result = await agent.handle(
        "Создай задачу «Изучить алгоритмы», исполнитель я, срока нет и ставить дедлайн не надо",
        source="cli",
    )
    assert result.action == "create"
    assert next(iter(vikunja.tasks.values())).due_date is None
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "ivan",
        decision(action="create", payload={"title": "Поручение Ивану", "assignee": "Иван", "due_date": "2026-11-15"}),
    )
    result = await agent.handle("Поручи Ивану задачу до 2026-11-15", source="cli")
    assert result.action == "ask"
    assert result.ok is True
    assert "не найден" in result.user_message
    assert vikunja.tasks == {}
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "known",
        decision(action="create", payload={"title": "Поручение Ивану", "assignee": "Иван", "due_date": "2026-11-15"}),
    )
    vikunja.users["иван"] = (5, "ivan")
    result = await agent.handle("Поручи Ивану задачу до 2026-11-15", source="cli")
    assert result.action == "create"
    assert next(iter(vikunja.tasks.values())).assignees == ["ivan"]
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "assign-error",
        decision(action="create", payload={"title": "Себе", "assignee": "я", "due_date": "2026-11-15"}),
    )

    async def broken(task_id: int) -> None:
        raise VikunjaError(500, "assign down")

    vikunja.assign_me = broken
    result = await agent.handle("Создай задачу себе до 2026-11-15", source="cli")
    assert result.ok is False
    assert "Создал задачу" not in result.user_message
    assert "назначить" in result.user_message
    assert vikunja.tasks
    assert vikunja.tasks[result.task_id or 0].assignees == []
    store.close()


async def test_naive_reminder_is_local_and_past_is_not_scheduled(tmp_path) -> None:
    agent, _vik, _llm, store = make_agent(
        tmp_path,
        decision(action="remind", payload={"fire_at": "2026-11-09T10:00:00", "text": "созвон"}),
    )
    result = await agent.handle("напомни 9 ноября в 10:00", source="cli")
    assert result.action == "remind"
    assert store.list_reminders()[0][1] == "2026-11-09T07:00:00+00:00"
    store.close()

    agent, _vik, _llm, store = make_agent(
        tmp_path / "past",
        decision(action="remind", payload={"fire_at": "2026-11-01T10:00:00", "text": "вчера"}),
    )
    result = await agent.handle("напомни в прошлое время", source="cli")
    assert result.action == "ask"
    assert store.list_reminders() == []
    store.close()


async def test_report_includes_note_and_measurement_not_the_order(tmp_path) -> None:
    create = decision(
        action="create",
        payload={
            "title": "Проверить отчёт",
            "assignee": "я",
            "due_date": "2026-11-20",
            "comment": "контрольная точка",
            "comment_kind": "факт",
        },
    )
    agent, vikunja, llm, store = make_agent(tmp_path, create)
    created = await agent.handle("Создай задачу проверить отчёт до 2026-11-20, исполнитель я", source="cli")
    assert created.action == "create"
    llm.decision = decision(action="append", task_id=created.task_id, payload={"next_step": "снять latency"})
    await agent.handle("замер latency 42 мс", source="cli")
    llm.decision = decision(action="note", payload={"text": "На встрече согласовали cosine similarity"})
    await agent.handle("заметка встречи", source="cli")
    llm.decision = decision(action="report")
    report = await agent.handle("Что сделано за неделю?", source="cli")
    message = report.user_message
    facts = message.split("Предположения:", 1)[0]
    assert "42 мс" in message
    assert "cosine similarity" in message
    assert "Создай задачу проверить отчёт" not in facts
    assert "Создай задачу проверить отчёт" in message
    store.close()


async def test_text_attachment_is_read_and_other_formats_are_not(tmp_path) -> None:
    big = tmp_path / "big.txt"
    big.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
    content, notice = _read_text_attachment(big)
    assert content is None and notice and "64" in notice
    plain = tmp_path / "note.md"
    plain.write_text("строка отчёта", encoding="utf-8")
    content, notice = _read_text_attachment(plain)
    assert content == "строка отчёта" and notice is None

    fixture = TelegramFixture(
        tmp_path / "tg",
        decision(
            action="create",
            payload={"title": "Из файла", "assignee": "я", "due_date": "2026-11-13", "comment": "файл прочитан"},
        ),
    )
    try:
        await fixture.message("/start")
        await fixture.message(document=True)
        assert "OMEGA_8421" in fixture.llm.last_user
        assert len(fixture.vik.tasks) == 1
        fixture.session.document_bytes = b"%PDF binary"
        fixture.llm.decision = decision(action="note", payload={"text": "файл лежит"})
        await fixture.message(document=True, file_name="sheet.pdf")
        assert any("не прочитано" in item["text"] for item in fixture.replies())
    finally:
        await fixture.close()


def test_html_description_does_not_gain_an_empty_header() -> None:
    html = (
        "<p>Ожидаемый результат: Native rich text result</p>"
        "<p>Следующий шаг: Native old step</p>"
        "<p>Последний подтверждённый результат: Native evidence</p>"
    )
    fields, body = parse_description(html)
    assert fields["Ожидаемый результат"] == "Native rich text result"
    assert fields["Следующий шаг"] == "Native old step"
    assert "Native evidence" in fields["Последний подтверждённый результат"]
    assert "<p>" not in body
    updated = apply_header(html, {"next_step": "Updated step"})
    assert "—" not in updated
    assert "Native old step" not in updated
    assert "Native rich text result" in updated
    assert "Native evidence" in updated
    assert "Следующий шаг: Updated step" in updated
    assert updated.count("Следующий шаг") == 1


def test_cli_failure_exits_nonzero_and_clarification_stays_zero(monkeypatch, capsys) -> None:
    async def failed(*args, **kwargs):
        return AgentResult(action="ask", user_message="Vikunja сейчас недоступна.", ok=False, task_id=None)

    async def clarify(*args, **kwargs):
        return AgentResult(action="ask", user_message="Какой срок?", ok=True, task_id=4)

    monkeypatch.setattr("flowci.cli._run", failed)
    assert main(["план", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["action"] == "ask"
    assert payload["task_id"] is None
    assert "недоступна" in payload["user_message"]

    monkeypatch.setattr("flowci.cli._run", clarify)
    assert main(["уточни", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "action": "ask", "task_id": 4, "user_message": "Какой срок?"}


def test_bootstrap_uses_repo_paths_and_does_not_rotate_token() -> None:
    text = Path("scripts/bootstrap.ps1").read_text(encoding="utf-8")
    assert "C:/projects/flowci" not in text
    assert "AddYears(10)" not in text
    assert "AddDays(90)" in text
    assert "Select-FlowCIPermissions" in text
    assert "not creating a replacement" in text
    assert "tasks?" in text or "tasks" in text
    assert "comments?" in text or "comment" in text


def test_hook_key_includes_artifacts_and_project() -> None:
    import importlib.util
    from datetime import datetime, timezone

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("flowci_task_sync_hook_integrity", root / ".cursor" / "hooks" / "task-sync.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    base = {
        "task_hint": "Эксперимент",
        "facts": ["готово"],
        "next_step": "сравнить",
        "session_id": "session",
        "project": "one",
        "artifacts": ["results-v1.csv"],
    }
    first = hook.build_event(base, "", {"conversation_id": "session"}, now)
    second = hook.build_event({**base, "artifacts": ["results-v2.csv"]}, "", {"conversation_id": "session"}, now)
    third = hook.build_event({**base, "project": "two"}, "", {"conversation_id": "session"}, now)
    assert first is not None and second is not None and third is not None
    assert first["deduplication_key"] != second["deduplication_key"]
    assert first["deduplication_key"] != third["deduplication_key"]


def test_hook_outbox_retries_without_printing_token(tmp_path, monkeypatch) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("flowci_task_sync_hook_outbox", root / ".cursor" / "hooks" / "task-sync.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".flowci").mkdir()
    (tmp_path / ".env").write_text("FLOWCI_INGEST_TOKEN=hook-test-token\nFLOWCI_INGEST_PORT=9\n", encoding="utf-8")
    (tmp_path / ".flowci" / "session.json").write_text(
        json.dumps({"task_hint": "Починить отчёт", "facts": ["тест"], "project": "FlowCI"}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(hook, "git_status", lambda _root: "")
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO("{}\n"))

    def boom(*args, **kwargs):
        raise OSError("offline")

    monkeypatch.setattr(hook, "post_event", boom)
    with pytest.raises(RuntimeError):
        hook.deliver()
    stored = list((tmp_path / ".flowci" / "outbox").glob("*.json"))
    assert stored
    blob = stored[0].read_text(encoding="utf-8")
    assert "hook-test-token" not in blob
    assert "Починить отчёт" in blob
    seen: list[dict] = []

    def accept(url: str, token: str, event: dict) -> dict:
        assert token == "hook-test-token"
        seen.append(event)
        return {"ok": True}

    monkeypatch.setattr(hook, "post_event", accept)
    hook.deliver()
    assert seen and seen[0]["task_hint"] == "Починить отчёт"
    assert list((tmp_path / ".flowci" / "outbox").glob("*.json")) == []


async def test_cursor_event_cannot_move_due_or_confirmed_result(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(
            action="change_status",
            task_id=1,
            payload={
                "status": "Завершено",
                "confirmed_result": "Unverified harness claim",
                "due_date": "2030-01-01",
                "comment": "сессия закончилась",
            },
        ),
    )
    vikunja.seed(Task(id=1, title="Harness", due_date="2026-11-20", labels=[STATUS_DOING], description=_header()))
    result = await agent.handle_event(
        {"source": "cursor", "session_id": "probe", "facts": ["commit"], "deduplication_key": "harness-1"}
    )
    task = vikunja.tasks[1]
    fields, _body = parse_description(task.description)
    assert result.action == "append"
    assert task.done is False
    assert task.due_date == "2026-11-20"
    assert fields["Последний подтверждённый результат"] in {"", "—"}
    store.close()

    measured = tmp_path / "latency.txt"
    measured.write_text("42 мс", encoding="utf-8")
    agent, vikunja, _llm, store = make_agent(
        tmp_path / "file",
        decision(
            action="append",
            task_id=1,
            payload={"confirmed_result": "latency 42 мс", "due_date": "2030-01-01", "comment": "есть файл"},
        ),
    )
    vikunja.seed(Task(id=1, title="Harness", due_date="2026-11-20", labels=[STATUS_DOING], description=_header()))
    await agent.handle_event(
        {
            "source": "cursor",
            "deduplication_key": "harness-file",
            "facts": ["измерение"],
            "artifacts": [str(measured)],
            "task_hint": "Harness",
        }
    )
    task = vikunja.tasks[1]
    fields, _body = parse_description(task.description)
    assert task.due_date == "2026-11-20"
    assert fields["Последний подтверждённый результат"] == "latency 42 мс"
    store.close()


async def test_personal_plan_sorts_by_deadline_and_hides_others(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(tmp_path, decision(action="plan", payload={"horizon": "today"}))
    today = "2026-11-08"
    vikunja.seed(
        Task(
            id=1,
            title="Позже моя",
            labels=[STATUS_TODO],
            assignees=["me"],
            due_date="2026-11-20",
            description=render_description(
                {"Ожидаемый результат": "а", "Следующий шаг": "б", "Последний подтверждённый результат": "—", "Когда планируется": today},
                "",
            ),
        )
    )
    vikunja.seed(
        Task(
            id=2,
            title="Раньше без исполнителя",
            labels=[STATUS_TODO],
            assignees=[],
            due_date="2026-11-09",
            description=render_description(
                {"Ожидаемый результат": "а", "Следующий шаг": "б", "Последний подтверждённый результат": "—", "Когда планируется": today},
                "",
            ),
        )
    )
    vikunja.seed(
        Task(
            id=3,
            title="Чужая",
            labels=["Сегодня"],
            assignees=["other-user"],
            due_date=today,
            description=_header(),
        )
    )
    result = await agent.handle("Что мне сегодня делать?", source="telegram")
    assert "Чужая" not in result.user_message
    assert result.user_message.index("Раньше без исполнителя") < result.user_message.index("Позже моя")
    digest = await agent.morning_digest()
    assert "Чужая" not in digest
    assert "Раньше без исполнителя" in digest
    store.close()


async def test_several_actions_ask_instead_of_applying(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="create", payload={"title": "Сразу всё", "assignee": "я", "due_date": "2026-11-21"}),
    )
    result = await agent.handle("Создай задачу отчёт и напомни завтра", source="cli")
    assert result.action == "ask"
    assert "первым" in result.user_message
    assert vikunja.tasks == {}
    assert llm.calls == 0
    store.close()
