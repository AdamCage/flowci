from __future__ import annotations

from flowci.formatting import apply_header, parse_description, render_description
from flowci.vikunja import Task
from tests.fakes import NOW, decision, make_agent, writes


def test_description_keeps_free_text() -> None:
    original = render_description(
        {
            "Ожидаемый результат": "было",
            "Следующий шаг": "старый",
            "Последний подтверждённый результат": "нет",
        },
        "Свободный текст",
    )
    updated = apply_header(original, {"next_step": "новый"})
    fields, body = parse_description(updated)
    assert body == "Свободный текст"
    assert fields["Следующий шаг"] == "новый"
    assert fields["Ожидаемый результат"] == "было"
    assert fields["Последний подтверждённый результат"] == "нет"


async def test_create_task(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="create",
            payload={
                "title": "Сравнить эмбеддинги",
                "expected_result": "Таблица качества и latency",
                "next_step": "Снять замеры на корпусе",
                "assignee": "я",
                "due_date": "2026-11-20",
                "plan": "Сегодня",
                "comment": "Поставили сравнение",
                "comment_kind": "факт",
            },
        ),
    )
    result = await agent.handle("Сравни эмбеддинги до 20 ноября", source="cli")
    assert result.created_task is True
    assert result.action == "create"
    assert len(vikunja.tasks) == 1
    task = next(iter(vikunja.tasks.values()))
    assert task.title == "Сравнить эмбеддинги"
    assert "Ожидаемый результат: Таблица качества и latency" in task.description
    assert "Следующий шаг: Снять замеры на корпусе" in task.description
    assert "Последний подтверждённый результат:" in task.description
    assert task.labels == ["Нужно сделать", "Сегодня"]
    assert task.due_date == "2026-11-20"
    assert task.assignees == ["me"]
    assert any("Поставили сравнение" in comment.text and "[факт]" in comment.text for comment in vikunja.comments)
    assert "Создал задачу" in result.user_message


async def test_append_does_not_change_status(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="append",
            task_id=1,
            payload={
                "next_step": "завтра тесты",
                "comment": "Нашёл проблему KV-cache",
                "comment_kind": "факт",
                "status": "Завершено",
            },
        ),
    )
    due = "2026-12-01T00:00:00Z"
    vikunja.seed(
        Task(
            id=1,
            title="Батчинг KV-cache",
            description=render_description(
                {
                    "Ожидаемый результат": "Стабильный батч",
                    "Следующий шаг": "Профилировать",
                    "Последний подтверждённый результат": "—",
                },
                "Не затирать этот абзац.",
            ),
            due_date=due,
            labels=["В работе", "Сегодня"],
        )
    )
    result = await agent.handle("Разобрался с батчингом, завтра продолжу тесты", source="cli")
    task = vikunja.tasks[1]
    assert result.action == "append"
    assert "без смены статуса" in result.user_message
    assert task.labels == ["В работе", "Сегодня"]
    assert task.done is False
    assert task.due_date == due
    assert "Следующий шаг: завтра тесты" in task.description
    assert "Ожидаемый результат: Стабильный батч" in task.description
    assert "Не затирать этот абзац." in task.description
    assert "Завершено" not in task.labels
    assert any("[факт]" in comment.text and "KV-cache" in comment.text for comment in vikunja.comments)
    assert not any(call[0] == "create_task" for call in vikunja.calls)


async def test_ambiguous_match_asks_and_does_not_duplicate(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="create",
            confidence=0.92,
            user_message="Какую задачу дополнить: #1 или #2?",
            payload={
                "title": "Сравнить эмбеддинги",
                "assignee": "я",
                "due_date": "2026-11-20",
                "candidates": [1, 2],
            },
        ),
    )
    vikunja.seed(Task(id=1, title="Сравнить эмбеддинги для поиска", labels=["Нужно сделать"]))
    vikunja.seed(Task(id=2, title="Сравнить эмбеддинги для каталога", labels=["В работе"]))
    result = await agent.handle("Дополни сравнение эмбеддингов", source="cli")
    assert result.action == "ask"
    assert "Какую задачу" in result.user_message
    assert result.created_task is False
    assert set(vikunja.tasks) == {1, 2}
    assert writes(vikunja.calls) == []


async def test_low_confidence_asks_without_create(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="create",
            confidence=0.2,
            payload={"title": "Новая обязанность", "assignee": "я", "due_date": "2026-11-20"},
            user_message="Уточните, это новая задача?",
        ),
    )
    result = await agent.handle("может быть задача", source="cli")
    assert result.action == "ask"
    assert result.user_message == "Уточните, это новая задача?"
    assert vikunja.tasks == {}


async def test_note_does_not_create_task(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="note", payload={"text": "Просто наблюдение про latency"}),
    )
    result = await agent.handle("Заметка: latency сегодня прыгала", source="cli")
    assert result.action == "note"
    assert "не создавал" in result.user_message
    assert vikunja.tasks == {}
    assert writes(vikunja.calls) == []
    assert store.list_notes() == ["Просто наблюдение про latency"]


async def test_completion_rejected_without_confirmation(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="change_status",
            task_id=1,
            confidence=0.99,
            payload={"status": "Завершено"},
        ),
    )
    due = "2026-12-01T00:00:00Z"
    vikunja.seed(Task(id=1, title="Закрыть отчёт", due_date=due, labels=["В работе"]))
    result = await agent.handle("закрой задачу", source="cli", confirm=False)
    assert result.needs_confirmation is True
    assert result.confirmation_token
    assert "Подтвердите" in result.user_message
    assert vikunja.tasks[1].labels == ["В работе"]
    assert vikunja.tasks[1].due_date == due
    assert not any(call[0] == "update_task" for call in vikunja.calls)
    assert not any(call[0] == "delete_task" for call in vikunja.calls)


async def test_completion_allowed_with_confirmation(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="change_status",
            task_id=1,
            confidence=0.99,
            payload={"status": "Завершено", "comment": "Человек подтвердил"},
        ),
    )
    due = "2026-12-01T00:00:00Z"
    vikunja.seed(Task(id=1, title="Закрыть отчёт", due_date=due, labels=["В работе", "Сегодня"]))
    result = await agent.handle("закрой задачу", source="cli", confirm=True)
    task = vikunja.tasks[1]
    assert result.needs_confirmation is False
    assert result.action == "change_status"
    assert "Завершено" in task.labels
    assert "В работе" not in task.labels
    assert "Сегодня" in task.labels
    assert task.done is True
    assert task.due_date == due


async def test_confirmation_token_completes_task(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(action="change_status", task_id=1, confidence=0.99, payload={"status": "Завершено"}),
    )
    vikunja.seed(Task(id=1, title="Закрыть отчёт", labels=["В работе"], due_date="2026-12-01T00:00:00Z"))
    pending = await agent.handle("готово", source="telegram", confirm=False)
    assert pending.needs_confirmation is True
    assert vikunja.tasks[1].labels == ["В работе"]
    confirmed = await agent.confirm(pending.confirmation_token or "")
    assert confirmed.action == "change_status"
    assert "Завершено" in vikunja.tasks[1].labels
    assert vikunja.tasks[1].due_date == "2026-12-01T00:00:00Z"


async def test_delete_requires_confirmation(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(action="change_status", task_id=1, confidence=0.99, payload={"delete": True}),
    )
    vikunja.seed(Task(id=1, title="Лишняя", labels=["Нужно сделать"]))
    rejected = await agent.handle("удали", source="cli", confirm=False)
    assert rejected.needs_confirmation is True
    assert 1 in vikunja.tasks
    allowed, vikunja2, _llm2, _store2 = make_agent(
        tmp_path / "other",
        decision(action="change_status", task_id=1, confidence=0.99, payload={"delete": True}),
    )
    vikunja2.seed(Task(id=1, title="Лишняя", labels=["Нужно сделать"]))
    result = await allowed.handle("удали", source="cli", confirm=True)
    assert result.action == "change_status"
    assert vikunja2.tasks == {}


async def test_duplicate_deduplication_key_is_noop(tmp_path) -> None:
    payload = {
        "title": "Проверить latency",
        "assignee": "я",
        "due_date": "2026-11-21",
        "expected_result": "Цифра latency",
        "comment": "Новая проверка",
        "comment_kind": "факт",
    }
    agent, vikunja, llm, _store = make_agent(tmp_path, decision(action="create", payload=payload))
    first = await agent.handle("поручение, дедлайн 2026-11-21", source="cli", deduplication_key="evt-42")
    second = await agent.handle("поручение ещё раз", source="cli", deduplication_key="evt-42")
    assert first.created_task is True
    assert second.noop is True
    assert "уже обработано" in second.user_message
    assert llm.calls == 1
    assert sum(1 for call in vikunja.calls if call[0] == "create_task") == 1
    assert len(vikunja.tasks) == 1


async def test_plan_view_does_not_change_due_date(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(action="plan", payload={"horizon": "today", "due_date": "2030-01-01"}),
    )
    due = "2026-11-20T00:00:00Z"
    vikunja.seed(
        Task(
            id=1,
            title="Отчёт по ChatCI",
            due_date=due,
            labels=["Сегодня", "В работе"],
            description=render_description(
                {
                    "Ожидаемый результат": "Сводка",
                    "Следующий шаг": "Собрать цифры",
                    "Последний подтверждённый результат": "—",
                    "Когда планируется": "2026-11-08",
                },
                "",
            ),
        )
    )
    result = await agent.handle("что мне сегодня делать", source="cli")
    assert result.action == "plan"
    assert "Отчёт по ChatCI" in result.user_message
    assert vikunja.tasks[1].due_date == due
    assert vikunja.tasks[1].labels == ["Сегодня", "В работе"]
    assert writes(vikunja.calls) == []


async def test_weekly_report_distinguishes_fact_and_assumption(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(tmp_path, decision(action="report", payload={"period": "week"}))
    vikunja.seed(Task(id=1, title="ChatCI", labels=["В работе"]))
    from flowci.vikunja import Comment

    vikunja.comments = [
        Comment(1, 1, "[факт] источник: cursor\nСобран бенчмарк latency", "2026-11-07T10:00:00+03:00"),
        Comment(2, 1, "[предположение] источник: agent\nВозможно, задача почти готова", "2026-11-07T11:00:00+03:00"),
        Comment(3, 1, "просто болтовня без метки", "2026-11-07T12:00:00+03:00"),
        Comment(4, 1, "[факт] источник: cursor\nСтарый закрытый эксперимент", "2026-10-01T10:00:00+03:00"),
    ]
    result = await agent.handle("Что сделали за неделю по ChatCI?", source="cli")
    message = result.user_message
    facts, rest = message.split("Предположения:", 1)
    assumptions, rest = rest.split("Ответы человека:", 1)
    _human, unknown = rest.split("Неизвестно:", 1)
    assert "Собран бенчмарк latency" in facts
    assert "Возможно, задача почти готова" not in facts
    assert "просто болтовня без метки" not in facts
    assert "Возможно, задача почти готова" in assumptions
    assert "Собран бенчмарк latency" not in assumptions
    assert "просто болтовня без метки" in unknown
    assert "Старый закрытый эксперимент" not in message
    assert writes(vikunja.calls) == []
    assert NOW.tzinfo is not None


async def test_harness_event_does_not_complete(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="change_status",
            task_id=1,
            confidence=0.99,
            payload={"status": "Завершено", "comment": "сессия закончилась коммитом"},
        ),
    )
    vikunja.seed(Task(id=1, title="Интеграция", labels=["В работе"], due_date="2026-12-01T00:00:00Z"))
    result = await agent.handle("summary", source="cursor")
    assert "Завершено" not in vikunja.tasks[1].labels
    assert vikunja.tasks[1].labels == ["В работе"]
    assert vikunja.tasks[1].due_date == "2026-12-01T00:00:00Z"
    assert result.needs_confirmation is False
    assert any("[предположение]" in comment.text for comment in vikunja.comments)


async def test_missing_deadline_asks_once(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(
        tmp_path,
        decision(
            action="create",
            payload={"title": "Купить молоко", "assignee": "я"},
        ),
    )
    result = await agent.handle("купи молоко", source="cli")
    assert result.action == "ask"
    assert "срок" in result.user_message
    assert "Купить молоко" in result.user_message
    assert vikunja.tasks == {}


async def test_reminder_is_stored(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="remind", payload={"in_minutes": 60, "text": "проверить CI"}),
    )
    result = await agent.handle("напомни через час проверить CI", source="cli")
    assert result.action == "remind"
    reminders = store.list_reminders()
    assert len(reminders) == 1
    assert reminders[0][2] == "проверить CI"
    assert reminders[0][3] == 0
    assert writes(vikunja.calls) == []


async def test_morning_digest_is_read_only(tmp_path) -> None:
    agent, vikunja, _llm, _store = make_agent(tmp_path, decision(action="note"))
    due = "2026-11-20T00:00:00Z"
    vikunja.seed(
        Task(
            id=1,
            title="Утренний пункт",
            due_date=due,
            labels=["Сегодня", "В работе"],
            description=render_description(
                {
                    "Ожидаемый результат": "План",
                    "Следующий шаг": "Открыть задачу",
                    "Последний подтверждённый результат": "—",
                    "Когда планируется": "2026-11-08",
                },
                "",
            ),
        )
    )
    vikunja.seed(Task(id=2, title="Старый дедлайн", due_date="2026-11-01T00:00:00Z", labels=["Нужно сделать"]))
    text = await agent.morning_digest()
    assert "Утренний пункт" in text
    assert "Старый дедлайн" in text
    assert vikunja.tasks[1].due_date == due
    assert writes(vikunja.calls) == []
