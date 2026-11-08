"""Регрессии G01–G06 и HTML-шапки. Живая Vikunja и порт 3456 не используются."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import io
import json
import sqlite3
from pathlib import Path

import httpx

from flowci.agent import _dialog_prompt
from flowci.formatting import STATUS_DOING, STATUS_TODO, apply_header, classify_due, human_due_text, parse_description
from flowci.schedule import ReminderScheduler
from flowci.store import Store
from flowci.vikunja import Task, VikunjaClient, VikunjaError, marker_matches
from tests.fakes import NOW, decision, make_agent, make_settings


def test_due_operation_uses_only_the_human_target() -> None:
    today = NOW.date()
    measured = "Дополни задачу: срок обработки сократился с 4 до 2 секунд; измерено 2026-11-02"
    assert classify_due(measured, today) == ("absent", None)
    assert classify_due("результат к публикации. Измерение 2026-11-01", today) == ("absent", None)
    assert classify_due("Перенеси срок задачи с 2026-11-25 на 2026-12-01", today) == ("set", "2026-12-01")
    assert classify_due("перенеси с 25.11 на 01.12", today) == ("set", "2026-12-01")
    prompt = _dialog_prompt(
        {
            "original_text": "Создай задачу «Срок»",
            "question": "Какой срок или срока нет?",
            "answers": ["до 2026-11-23"],
            "payload": {},
        },
        "до 2026-11-23",
    )
    assert "срока нет" in prompt
    assert "срока нет" not in human_due_text(prompt)
    assert "Вопрос:" not in human_due_text(prompt)
    assert classify_due(prompt, today) == ("set", "2026-11-23")
    poisoned = (
        "Исходное сообщение:\nСоздай задачу\n"
        "Ответ 1:\nдо 2026-11-23\n"
        "Вопрос:\nили срока нет?"
    )
    assert classify_due(poisoned, today) == ("set", "2026-11-23")


async def test_measurement_and_transfer_do_not_pick_the_wrong_date(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="append", task_id=1, payload={"next_step": "смотреть замер", "due_date": "2026-11-02"}),
    )
    vikunja.seed(Task(id=1, title="Замер", due_date="2026-11-25", labels=[STATUS_DOING]))
    await agent.handle(
        "Дополни задачу #1: срок обработки сократился с 4 до 2 секунд; измерено 2026-11-02",
        source="telegram",
        chat_id=1,
    )
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "publication",
        decision(action="append", task_id=1, payload={"due_date": "2026-11-01", "comment": "к публикации"}),
    )
    vikunja.seed(Task(id=1, title="Публикация", due_date="2026-11-25", labels=[STATUS_TODO]))
    await agent.handle("результат к публикации. Измерение 2026-11-01", source="cli")
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "transfer",
        decision(action="append", task_id=1, payload={"due_date": "2026-11-25"}),
    )
    vikunja.seed(Task(id=1, title="Перенос", due_date="2026-11-25", labels=[STATUS_TODO]))
    await agent.handle("Перенеси срок задачи с 2026-11-25 на 2026-12-01", source="telegram", chat_id=1)
    assert str(vikunja.tasks[1].due_date).startswith("2026-12-01")
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "dotted",
        decision(action="append", task_id=1, payload={"due_date": "2026-11-25"}),
    )
    vikunja.seed(Task(id=1, title="Короткий перенос", due_date="2026-11-25", labels=[STATUS_TODO]))
    await agent.handle("перенеси с 25.11 на 01.12", source="cli")
    assert str(vikunja.tasks[1].due_date).startswith("2026-12-01")
    store.close()


async def test_quoted_question_does_not_clear_human_date(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="ask", user_message="Какой срок или срока нет?"),
    )
    asked = await agent.handle("Создай задачу «Публикация отчёта», исполнитель я", source="telegram", chat_id=5)
    assert asked.action == "ask"
    llm.decision = decision(
        action="create",
        payload={
            "title": "Публикация отчёта",
            "assignee": "я",
            "due_date": "2026-11-23",
            "no_deadline": False,
        },
    )
    created = await agent.handle("до 2026-11-23", source="telegram", chat_id=5)
    assert created.action == "create"
    assert str(next(iter(vikunja.tasks.values())).due_date).startswith("2026-11-23")
    store.close()


async def test_model_tomorrow_without_request_is_ignored(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="append", task_id=1, payload={"comment": "заметка", "due_date": "завтра"}),
    )
    vikunja.seed(Task(id=1, title="Срок стоит", due_date="2026-11-25", labels=[STATUS_TODO]))
    await agent.handle("Добавь комментарий: всё спокойно", source="telegram", chat_id=1)
    assert vikunja.tasks[1].due_date == "2026-11-25"
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "create",
        decision(action="create", payload={"title": "Без поручения", "assignee": "я", "due_date": "завтра"}),
    )
    result = await agent.handle("Создай задачу «Без поручения», исполнитель я", source="cli")
    assert result.action == "ask"
    assert vikunja.tasks == {}
    store.close()

    agent, vikunja, _llm, store = make_agent(
        tmp_path / "iso",
        decision(action="create", payload={"title": "Без поручения", "assignee": "я", "due_date": "2026-12-01"}),
    )
    result = await agent.handle("Создай задачу «Без поручения», исполнитель я", source="cli")
    assert result.action == "ask"
    assert vikunja.tasks == {}
    store.close()


async def test_event_b_does_not_inherit_event_a_context(tmp_path) -> None:
    evidence = tmp_path / "alpha-evidence.txt"
    evidence.write_text("alpha", encoding="utf-8")
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="ask", user_message="Вопрос Alpha"))
    sent: list[int] = []

    async def send(text: str) -> int:
        sent.append(len(sent) + 1)
        return sent[-1]

    agent.outbox_sender = send
    await agent.handle_event(
        {
            "source": "cursor",
            "deduplication_key": "event-A",
            "task_hint": "ALPHA_HINT",
            "facts": ["факт A"],
            "artifacts": [str(evidence)],
        }
    )
    llm.decision = decision(action="ask", user_message="Вопрос Beta")
    await agent.handle_event(
        {
            "source": "cursor",
            "deduplication_key": "event-B",
            "task_hint": "BETA_HINT",
            "facts": ["факт B"],
            "artifacts": [],
        }
    )
    dialog_b = store.get_dialog("event:event-B")
    assert dialog_b is not None
    assert "ALPHA_HINT" not in dialog_b["original_text"]
    assert str(evidence) not in json.dumps(dialog_b["extra"], ensure_ascii=False)
    assert dialog_b["extra"].get("deduplication_key") == "event-B"
    vikunja.seed(Task(id=1, title="Чужая", labels=[STATUS_DOING], description="Следующий шаг: держать"))
    llm.decision = decision(
        action="append",
        task_id=1,
        payload={"confirmed_result": "B_UNVERIFIED_RESULT_WITHOUT_B_FILE", "next_step": "дальше"},
    )
    result = await agent.handle("Это задача #1", source="telegram", chat_id=7, user_id=7, reply_to=sent[-1])
    assert result.action == "append"
    fields, _body = parse_description(vikunja.tasks[1].description)
    assert "B_UNVERIFIED" not in fields["Последний подтверждённый результат"]
    store.close()


async def test_reply_to_closed_question_does_not_run_the_other_event(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="ask", user_message="Вопрос Cedar"))
    sent: list[int] = []

    async def send(text: str) -> int:
        sent.append(len(sent) + 1)
        return sent[-1]

    agent.outbox_sender = send
    await agent.handle_event({"source": "cursor", "deduplication_key": "event-A", "facts": ["Cedar"], "task_hint": "Cedar"})
    llm.decision = decision(action="ask", user_message="Вопрос Delta")
    await agent.handle_event({"source": "cursor", "deduplication_key": "event-B", "facts": ["Delta"], "task_hint": "Delta"})
    llm.decision = decision(
        action="create",
        payload={"title": "Первое поручение Cedar", "assignee": "я", "due_date": "2026-11-25"},
    )
    answered = await agent.handle("я, до 2026-11-25", source="telegram", chat_id=111, user_id=111, reply_to=1)
    assert answered.action == "create"
    kept = store.get_dialog("event:event-A")
    assert kept is not None
    assert kept["question_message_id"] == 1
    llm.decision = decision(
        action="create",
        payload={"title": "Delta B", "assignee": "я", "due_date": "2026-12-01"},
    )
    again = await agent.handle("я, до 2026-12-01", source="telegram", chat_id=111, user_id=111, reply_to=1)
    assert "закрыт" in again.user_message
    assert [task.title for task in vikunja.tasks.values()] == ["Первое поручение Cedar"]
    assert store.event_stage("event-B") == "awaiting_input"
    unknown = await agent.handle("я", source="telegram", chat_id=111, user_id=111, reply_to=99)
    assert "закрыт" in unknown.user_message
    assert store.event_stage("event-B") == "awaiting_input"
    store.close()


def test_marker_match_is_exact() -> None:
    assert marker_matches("prefix flowci-event:own-v3-marker-10\n", "flowci-event:own-v3-marker-1") is False
    assert marker_matches("flowci-event:own-v3-marker-1", "flowci-event:own-v3-marker-1") is True
    assert marker_matches("flowci-event:own-v3-marker-1\nхвост", "flowci-event:own-v3-marker-1") is True


async def test_marker_recovery_does_not_touch_another_card(tmp_path) -> None:
    calls: list[str] = []
    cards = [
        {
            "id": 10,
            "title": "Чужая",
            "description": "flowci-event:own-v3-marker-10",
            "labels": [],
            "project_id": 7,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET" and request.url.path == "/api/v1/projects/7/tasks":
            return httpx.Response(200, json=cards)
        if request.method == "PUT" and request.url.path == "/api/v1/projects/7/tasks":
            raise httpx.ReadTimeout("lost response")
        return httpx.Response(404, json={"message": request.url.path})

    settings = make_settings(tmp_path, vikunja_project_id=7)
    client = VikunjaClient(settings, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    try:
        await client.create_task(
            title="Новая",
            description="текст",
            due_date=None,
            labels=[],
            event_marker="flowci-event:own-v3-marker-1",
        )
        raise AssertionError("recovery must stop")
    except VikunjaError:
        pass
    assert not any(path.endswith("/comments") for path in calls)
    assert cards[0]["description"] == "flowci-event:own-v3-marker-10"
    await client.aclose()

    agent, vikunja, llm, store = make_agent(
        tmp_path / "agent",
        decision(action="create", payload={"title": "Своя карточка", "assignee": "я", "due_date": "2026-11-20"}),
    )
    original = "flowci-event:own-v3-marker-10"
    vikunja.seed(Task(id=10, title="Чужая", description=original, labels=[STATUS_TODO]))
    store.open_event("own-v3-marker-1", {"text": "new", "source": "cli", "extra": {}, "allow_completion": True})
    store._conn.execute(
        "UPDATE dedupe SET lease_until = '2000-01-01T00:00:00+00:00' WHERE deduplication_key = 'own-v3-marker-1'"
    )
    store._conn.commit()
    store.release_held_claims()
    result = await agent.handle("new", source="cli", deduplication_key="own-v3-marker-1")
    assert result.task_id != 10
    assert vikunja.tasks[10].description == original
    assert all(comment.task_id != 10 for comment in vikunja.comments)
    assert llm.calls == 1
    store.close()


async def test_several_marker_matches_change_nothing(tmp_path) -> None:
    agent, vikunja, llm, store = make_agent(
        tmp_path,
        decision(action="create", payload={"title": "Не надо", "assignee": "я", "due_date": "2026-11-20"}),
    )
    vikunja.seed(Task(id=1, title="Одна", description="flowci-event:dup", labels=[STATUS_TODO]))
    vikunja.seed(Task(id=2, title="Другая", description="заметка flowci-event:dup", labels=[STATUS_TODO]))
    before = {task_id: task.description for task_id, task in vikunja.tasks.items()}
    store.open_event("dup", {"text": "retry", "source": "cli", "extra": {}, "allow_completion": True})
    store._conn.execute("UPDATE dedupe SET lease_until = '2000-01-01T00:00:00+00:00' WHERE deduplication_key = 'dup'")
    store._conn.commit()
    store.release_held_claims()
    result = await agent.handle("retry", source="cli", deduplication_key="dup")
    assert result.ok is False
    assert "нескольких" in result.user_message
    assert {task_id: task.description for task_id, task in vikunja.tasks.items()} == before
    assert vikunja.comments == []
    assert llm.calls == 0
    store.close()


async def test_old_owner_does_not_create_a_second_card(tmp_path) -> None:
    payload = {"title": "Одна карточка", "assignee": "я", "due_date": "2026-11-25", "comment": "исход"}
    agent, vikunja, llm, store = make_agent(tmp_path, decision(action="create", payload=payload))
    waiting = {"n": 0}
    started = asyncio.Event()
    gate = asyncio.Event()

    async def slow(system: str, user: str) -> str:
        waiting["n"] += 1
        if waiting["n"] == 1:
            started.set()
        await gate.wait()
        llm.calls += 1
        return json.dumps(llm.decision, ensure_ascii=False)

    llm.complete = slow  # type: ignore[method-assign]
    llm.calls = 0
    event = {"source": "cursor", "deduplication_key": "fence", "facts": ["new"], "task_hint": "Одна карточка"}
    first = asyncio.create_task(agent.handle_event(event))
    await started.wait()
    store._conn.execute(
        "UPDATE dedupe SET lease_until = '2000-01-01T00:00:00+00:00' WHERE deduplication_key = 'fence'"
    )
    store._conn.commit()
    second = asyncio.create_task(agent.handle_event(event))
    for _ in range(100):
        if waiting["n"] >= 2:
            break
        await asyncio.sleep(0.01)
    assert waiting["n"] >= 2
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert len(vikunja.tasks) == 1
    assert len([call for call in vikunja.calls if call[0] == "create_task"]) == 1
    assert {first_result.ok, second_result.ok} == {True, False}
    store.close()


async def test_busy_keeps_hook_outbox_and_failed_owner_still_creates_one_card(tmp_path) -> None:
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("flowci_task_sync_v3", root / ".cursor" / "hooks" / "task-sync.py")
    assert spec is not None and spec.loader is not None
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    assert hook.event_settled({"ok": True, "busy": True, "user_message": "already processing"}) is False
    assert hook.event_settled({"ok": True, "action": "ask", "user_message": "Какой срок?"}) is True
    assert hook.event_settled({"ok": True, "action": "create", "user_message": "Создал"}) is True
    (tmp_path / ".flowci").mkdir()
    (tmp_path / ".env").write_text("FLOWCI_INGEST_TOKEN=review-only-token\nFLOWCI_INGEST_PORT=9\n", encoding="utf-8")
    event = hook.build_event({"session_id": "s", "facts": ["ещё"], "task_hint": "В работе", "project": "P"}, "", {}, NOW)
    hook.write_outbox(tmp_path, event)

    def busy(url: str, token: str, item: dict) -> dict:
        return {"ok": True, "busy": True, "action": "note", "user_message": "already processing", "noop": True}

    previous = (hook.sys.stdin, hook.sys.stdout, hook.sys.stderr)
    hook.find_root = lambda _start: tmp_path  # type: ignore[method-assign]
    hook.post_event = busy  # type: ignore[method-assign]
    hook.sys.stdin = io.StringIO("{}\n")
    hook.sys.stdout = io.StringIO()
    hook.sys.stderr = io.StringIO()
    try:
        assert hook.main() == 0
    finally:
        hook.sys.stdin, hook.sys.stdout, hook.sys.stderr = previous
    assert hook.load_outbox(tmp_path)

    payload = {"title": "После сбоя", "assignee": "я", "due_date": "2026-11-25"}
    agent, vikunja, llm, store = make_agent(tmp_path / "retry", decision(action="create", payload=payload))
    started = asyncio.Event()
    release = asyncio.Event()
    state = {"n": 0}

    async def flaky(system: str, user: str) -> str:
        state["n"] += 1
        if state["n"] == 1:
            started.set()
            await release.wait()
            raise RuntimeError("model down")
        llm.calls += 1
        return json.dumps(llm.decision, ensure_ascii=False)

    llm.complete = flaky  # type: ignore[method-assign]
    llm.calls = 0
    event_body = {"source": "cursor", "deduplication_key": "retry-me", "facts": ["создание"], "task_hint": "После сбоя"}
    first = asyncio.create_task(agent.handle_event(event_body))
    await started.wait()
    second = await agent.handle_event(event_body)
    assert second.busy is True
    assert second.user_message == "already processing"
    assert vikunja.tasks == {}
    release.set()
    failed = await first
    assert failed.ok is False
    assert vikunja.tasks == {}
    assert store.event_stage("retry-me") == "claimed"
    scheduler = ReminderScheduler(agent.settings, store, agent, lambda text: asyncio.sleep(0))
    await scheduler._resume_stale()
    assert len(vikunja.tasks) == 1
    assert llm.calls == 1
    store.close()


async def test_task_ready_repeat_does_not_duplicate_comment(tmp_path) -> None:
    payload = {"title": "Комментарий один", "assignee": "я", "due_date": "2026-11-25", "comment": "единственный"}
    agent, vikunja, _llm, store = make_agent(tmp_path, decision(action="create", payload=payload))
    blocked = asyncio.Event()
    release = asyncio.Event()
    original = vikunja.add_comment

    async def slow_comment(task_id: int, text: str):
        blocked.set()
        await release.wait()
        return await original(task_id, text)

    vikunja.add_comment = slow_comment  # type: ignore[method-assign]
    event = {"source": "cursor", "deduplication_key": "follow", "facts": ["создание"], "task_hint": "Комментарий один"}
    first = asyncio.create_task(agent.handle_event(event))
    await blocked.wait()
    second = await agent.handle_event(event)
    assert second.busy is True
    release.set()
    created = await first
    assert created.ok is True
    assert len(vikunja.comments) == 1
    assert len([call for call in vikunja.calls if call[0] == "assign_me"]) == 1
    comment = vikunja.comments[0].text
    store._conn.execute(
        "UPDATE dedupe SET stage = 'task_ready', lease_until = '2000-01-01T00:00:00+00:00' "
        "WHERE deduplication_key = 'follow'"
    )
    row = store._conn.execute("SELECT payload_json FROM dedupe WHERE deduplication_key = 'follow'").fetchone()
    spec = json.loads(row["payload_json"])
    spec["assign_done"] = False
    spec["comment_done"] = False
    spec["pending_comment"] = comment
    store._conn.execute(
        "UPDATE dedupe SET payload_json = ? WHERE deduplication_key = 'follow'",
        (json.dumps(spec, ensure_ascii=False),),
    )
    store._conn.commit()
    store.release_held_claims()
    vikunja.add_comment = original  # type: ignore[method-assign]
    again = await agent.handle_event(event)
    assert again.task_id == created.task_id
    assert len(vikunja.comments) == 1
    assert len([call for call in vikunja.calls if call[0] == "assign_me"]) == 1
    assert len(vikunja.tasks) == 1
    store.close()


async def test_executing_confirmation_can_be_confirmed_after_reopen(tmp_path) -> None:
    agent, vikunja, _llm, store = make_agent(
        tmp_path,
        decision(action="change_status", task_id=1, payload={"status": "Завершено"}),
    )
    vikunja.seed(Task(id=1, title="Подтвердить позже", labels=[STATUS_DOING]))
    asked = await agent.handle("закрой задачу #1", source="telegram", chat_id=111, user_id=111)
    token = asked.confirmation_token or ""
    store._conn.execute(
        "UPDATE pending_confirmations SET status = 'executing' WHERE token = ?",
        (token,),
    )
    store._conn.commit()
    store.close()
    reopened = Store(tmp_path / "flowci.db")
    agent.store = reopened
    result = await agent.confirm(token, 111)
    assert result.action == "change_status"
    assert "Завершено" in vikunja.tasks[1].labels
    reopened.close()


async def test_one_pending_question_is_sent_once(tmp_path) -> None:
    agent, _vik, _llm, store = make_agent(tmp_path, decision(action="ask", user_message="Какой срок для гонки?"))
    await agent.handle_event({"source": "cursor", "deduplication_key": "race", "facts": ["вопрос"]})
    assert len(store.pending_outbox()) == 1
    sent: list[int] = []

    async def send(text: str) -> int:
        await asyncio.sleep(0.05)
        sent.append(41)
        return 41

    agent.outbox_sender = send
    scheduler = ReminderScheduler(agent.settings, store, agent, send)
    await asyncio.gather(agent._deliver_outbox(), scheduler._dispatch_outbox())
    assert sent == [41]
    dialog = store.get_dialog("event:race")
    assert dialog is not None
    assert dialog["question_message_id"] == 41
    assert store.pending_outbox() == []
    store.close()


def test_bold_label_is_replaced_and_html_body_is_applied() -> None:
    updated = apply_header("<p><strong>Следующий шаг:</strong> OLD</p>", {"next_step": "NEW"})
    assert "NEW" in updated
    assert "OLD" not in updated
    assert "<strong>" in updated
    html = "<p><strong>Ожидаемый результат:</strong> KEEP</p><p>OLD BODY</p>"
    replaced = apply_header(html, {}, body="NEW_BODY_ONLY")
    assert "KEEP" in replaced
    assert "NEW_BODY_ONLY" in replaced
    assert "OLD BODY" not in replaced
    assert "<p>" in replaced
    assert "<strong>" in replaced


async def test_expired_write_lease_cannot_overwrite_a_newer_edit(tmp_path) -> None:
    state = {
        "id": 3,
        "title": "Карточка",
        "description": "before",
        "done": False,
        "labels": [],
        "project_id": 7,
        "priority": 0,
        "due_date": "2026-12-01T00:00:00Z",
    }
    gets = {"n": 0}
    posts: list[dict] = []
    first_get = asyncio.Event()
    release_first = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/tasks/3":
            gets["n"] += 1
            if gets["n"] == 1:
                first_get.set()
                await release_first.wait()
            return httpx.Response(200, json=copy.deepcopy(state))
        if request.method == "POST" and request.url.path == "/api/v1/tasks/3":
            body = json.loads(request.content.decode())
            posts.append(body)
            state.update(body)
            return httpx.Response(200, json=copy.deepcopy(state))
        return httpx.Response(404)

    settings = make_settings(tmp_path, vikunja_project_id=7)
    transport = httpx.MockTransport(handler)
    older = VikunjaClient(settings, client=httpx.AsyncClient(transport=transport))
    newer = VikunjaClient(settings, client=httpx.AsyncClient(transport=transport))
    first = asyncio.create_task(older.update_task(3, description="stale description"))
    await first_get.wait()
    connection = sqlite3.connect(tmp_path / "flowci.db")
    connection.execute("UPDATE task_write_leases SET lease_until = '2000-01-01T00:00:00+00:00'")
    connection.commit()
    connection.close()
    await newer.update_task(3, done=True)
    release_first.set()
    try:
        await first
        raise AssertionError("stale writer must not post")
    except VikunjaError as exc:
        assert exc.status == 409
    assert state["done"] is True
    assert len(posts) == 1
    assert posts[0]["done"] is True
    await older.aclose()
    await newer.aclose()
