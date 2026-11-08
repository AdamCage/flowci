"""Общие заготовки тестов. Секреты здесь нарочно фальшивые."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

from flowci.agent import TaskAgent
from flowci.config import Settings, resolve_timezone
from flowci.formatting import apply_header, with_group
from flowci.store import Store
from flowci.vikunja import Comment, Task, marker_matches

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=resolve_timezone("Europe/Moscow"))


def make_settings(tmp_path, **overrides) -> Settings:
    values = dict(
        routerai_api_key="test-router-key",
        routerai_base_url="https://router.example/v1",
        routerai_chat_model="test-chat",
        routerai_stt_model="test-stt",
        tg_bot_token="test-tg-token",
        tg_allowed_user_ids=(),
        vikunja_url="http://vikunja.test",
        vikunja_token="test-vikunja-token",
        vikunja_project_id=7,
        flowci_ingest_token="test-ingest-token",
        flowci_ingest_host="127.0.0.1",
        flowci_ingest_port=8787,
        flowci_timezone="Europe/Moscow",
        flowci_morning_hour=9,
        flowci_data_dir=tmp_path,
        repo_root=tmp_path,
    )
    values.update(overrides)
    return Settings(**values)


class FakeLLM:
    def __init__(self, decision: dict) -> None:
        self.decision = decision
        self.calls = 0

    async def complete(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        return json.dumps(self.decision, ensure_ascii=False)

    async def transcribe(self, path) -> str:
        return "транскрипт"


class FakeVikunja:
    def __init__(self) -> None:
        self.tasks: dict[int, Task] = {}
        self.comments: list[Comment] = []
        self.calls: list[tuple] = []
        self.users: dict[str, tuple[int, str]] = {}
        self._next_task = 1
        self._next_comment = 1
        self._locks: dict[int, asyncio.Lock] = {}

    def seed(self, task: Task) -> None:
        self.tasks[task.id] = task
        self._next_task = max(self._next_task, task.id + 1)

    async def list_tasks(self) -> list[Task]:
        self.calls.append(("list_tasks",))
        return [self.tasks[key] for key in sorted(self.tasks)]

    async def create_task(
        self,
        *,
        title: str,
        description: str,
        due_date: str | None,
        labels: list[str],
        priority: int = 0,
        done: bool = False,
        on_created=None,
        event_marker: str | None = None,
    ) -> Task:
        self.calls.append(("create_task", title))
        if event_marker and not marker_matches(description or "", event_marker):
            description = ((description or "").rstrip() + "\n" + event_marker).strip()
        task = Task(
            id=self._next_task,
            title=title,
            description=description,
            due_date=due_date,
            labels=list(labels),
            priority=priority,
            done=done,
            project_id=1,
        )
        self._next_task += 1
        self.tasks[task.id] = task
        if on_created is not None:
            outcome = on_created(task)
            if outcome is not None:
                await outcome
        return task

    async def sync_task_labels(self, task_id: int, labels: list[str]) -> None:
        self.calls.append(("sync_task_labels", task_id, list(labels)))
        self.tasks[task_id].labels = list(labels)

    async def find_task_by_marker(self, marker: str) -> Task | None:
        found = [task for task in self.tasks.values() if marker_matches(task.description or "", marker)]
        if len(found) != 1:
            return None
        return found[0]

    def _lock_for(self, task_id: int) -> asyncio.Lock:
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def update_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        done: bool | None = None,
        priority: int | None = None,
        due_date: str | None = None,
        set_due_date: bool = False,
        header: dict | None = None,
        body: str | None = None,
        body_append: str | None = None,
        label_group: tuple[str, ...] | None = None,
        label_value: str | None = None,
    ) -> Task:
        async with self._lock_for(task_id):
            current = self.tasks[task_id]
            new_description = current.description
            if description is not None:
                new_description = description
            elif header or body is not None or (body_append or ""):
                new_description = apply_header(
                    current.description,
                    header or {},
                    body=body,
                    body_append=body_append,
                )
            labels = list(current.labels)
            if label_group is not None:
                labels = with_group(labels, tuple(label_group), label_value)
            stored = Task(
                id=current.id,
                title=current.title if title is None else title,
                description=new_description,
                due_date=due_date if set_due_date else current.due_date,
                labels=labels,
                priority=current.priority if priority is None else priority,
                done=current.done if done is None else done,
                assignees=list(current.assignees),
                project_id=current.project_id,
            )
            self.tasks[task_id] = stored
            self.calls.append(("update_task", task_id, stored.due_date, list(stored.labels), stored.description))
            return stored

    async def add_comment(self, task_id: int, text: str) -> Comment:
        self.calls.append(("add_comment", task_id, text))
        comment = Comment(
            id=self._next_comment,
            task_id=task_id,
            text=text,
            created="2026-11-08T09:00:00+03:00",
        )
        self._next_comment += 1
        self.comments.append(comment)
        return comment

    async def list_comments(self, task_id: int) -> list[Comment]:
        self.calls.append(("list_comments", task_id))
        return [comment for comment in self.comments if comment.task_id == task_id]

    async def delete_task(self, task_id: int) -> None:
        self.calls.append(("delete_task", task_id))
        self.tasks.pop(task_id, None)

    async def assign_me(self, task_id: int) -> None:
        self.calls.append(("assign_me", task_id))
        self.tasks[task_id].assignees = ["me"]

    async def current_user(self) -> tuple[int, str]:
        self.calls.append(("current_user",))
        return 1, "me"

    async def find_user(self, query: str) -> int | None:
        self.calls.append(("find_user", query))
        item = self.users.get(query.casefold())
        if item is None:
            return None
        return item[0]

    async def assign_user(self, task_id: int, user_id: int) -> None:
        self.calls.append(("assign_user", task_id, user_id))
        username = str(user_id)
        for _name, (uid, display) in self.users.items():
            if uid == user_id:
                username = display
                break
        self.tasks[task_id].assignees = [username]


def make_agent(tmp_path, decision: dict, *, clock=None):
    settings = make_settings(tmp_path)
    store = Store(tmp_path / "flowci.db")
    vikunja = FakeVikunja()
    llm = FakeLLM(decision)
    agent = TaskAgent(settings, store, vikunja, llm, clock=clock or (lambda: NOW))
    return agent, vikunja, llm, store


def decision(**overrides) -> dict:
    base = {
        "action": "note",
        "task_id": None,
        "confidence": 0.95,
        "needs_confirmation": False,
        "payload": {},
        "user_message": "",
    }
    base.update(overrides)
    return base


def writes(calls: list[tuple]) -> list[tuple]:
    names = {"create_task", "update_task", "add_comment", "delete_task", "assign_me", "assign_user"}
    return [call for call in calls if call[0] in names]
