"""Клиент Vikunja API v1. Поля, которые не меняем, отправляются обратно целиком."""

from __future__ import annotations

import asyncio
import copy
import re
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from flowci.config import Settings

PROJECT_TITLE = "FlowCI"
_ZERO = "0001-01-01T00:00:00Z"
LABEL_COLORS = {
    "Нужно сделать": "64748b",
    "В работе": "2563eb",
    "Заблокировано": "dc2626",
    "Завершено": "16a34a",
    "Сегодня": "d97706",
    "Завтра": "7c3aed",
    "Неделя": "0891b2",
    "Позже": "94a3b8",
}


class VikunjaError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Vikunja HTTP {status}")
        self.status = status
        self.safe_message = message


@dataclass
class Task:
    id: int
    title: str
    description: str = ""
    due_date: str | None = None
    labels: list[str] = field(default_factory=list)
    priority: int = 0
    done: bool = False
    assignees: list[str] = field(default_factory=list)
    project_id: int = 0


@dataclass
class Comment:
    id: int
    task_id: int
    text: str
    created: str = ""


def to_rfc3339(value: str) -> str:
    text = value.strip()
    if not text or text.startswith("0001-01-01"):
        return _ZERO
    if "T" in text:
        return text
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10] + "T00:00:00Z"
    return text


def marker_matches(description: str, marker: str) -> bool:
    """Маркер совпадает целиком: flowci-event:…-1 не является префиксом …-10."""
    if not description or not marker:
        return False
    pattern = re.compile(rf"(?<![\w:]){re.escape(marker)}(?![\w-])")
    return pattern.search(description) is not None


def normalize_due(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return None
    return text


class VikunjaClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._root = settings.vikunja_url.rstrip("/")
        self._token = settings.vikunja_token
        self._project_id_value = settings.vikunja_project_id
        self._cached_project_id: int | None = None
        self._labels: dict[str, int] = {}
        self._me_id: int | None = None
        self._me_name: str | None = None
        self._task_locks: dict[int, asyncio.Lock] = {}
        self._task_locks_guard = asyncio.Lock()
        self._db_path = settings.db_path
        self._write_owner = uuid.uuid4().hex
        self._owns = client is None
        self._client = client or httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {settings.vikunja_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(30.0),
            trust_env=False,
        )

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def list_tasks(self) -> list[Task]:
        project_id = await self._project_id()
        raw_items = await self._paginate(f"/projects/{project_id}/tasks")
        tasks: list[Task] = []
        for item in raw_items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            if "labels" not in item:
                detail = await self._request("GET", f"/tasks/{int(item['id'])}")
                item = detail.json()
            tasks.append(self._parse_task(item))
        return tasks

    async def create_task(
        self,
        *,
        title: str,
        description: str,
        due_date: str | None,
        labels: list[str],
        priority: int = 0,
        done: bool = False,
        on_created: Callable[[Task], Awaitable[None] | None] | None = None,
        event_marker: str | None = None,
    ) -> Task:
        project_id = await self._project_id()
        text = description or ""
        if event_marker and not marker_matches(text, event_marker):
            text = (text.rstrip() + "\n" + event_marker).strip()
        body: dict[str, object] = {
            "title": title,
            "description": text,
            "priority": int(priority or 0),
            "done": bool(done),
        }
        if due_date:
            body["due_date"] = to_rfc3339(str(due_date))
        try:
            response = await self._request("PUT", f"/projects/{project_id}/tasks", body)
        except VikunjaError as exc:
            if exc.status != 0 or not event_marker:
                raise
            found = await self.find_task_by_marker(event_marker)
            if found is None:
                raise
            task = found
            current_labels: list = []
        else:
            created = response.json()
            task = self._parse_task(created)
            current_labels = created.get("labels") or []
        if on_created is not None:
            outcome = on_created(task)
            if outcome is not None:
                await outcome
        if labels:
            await self._sync_labels(task.id, list(labels), current_labels)
            task.labels = list(labels)
        return task

    async def tasks_with_marker(self, marker: str) -> list[Task]:
        if not marker:
            return []
        return [task for task in await self.list_tasks() if marker_matches(task.description or "", marker)]

    async def find_task_by_marker(self, marker: str) -> Task | None:
        matches = await self.tasks_with_marker(marker)
        if len(matches) != 1:
            return None
        return matches[0]

    async def sync_task_labels(self, task_id: int, labels: list[str]) -> None:
        current = await self._request("GET", f"/tasks/{int(task_id)}")
        raw = current.json()
        items = raw.get("labels") if isinstance(raw, dict) else []
        await self._sync_labels(int(task_id), list(labels), items or [])

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
        lock = await self._task_lock(int(task_id))
        async with lock:
            generation = await asyncio.to_thread(self._acquire_write_lease, int(task_id))
            try:
                return await self._update_locked(
                    int(task_id),
                    title=title,
                    description=description,
                    done=done,
                    priority=priority,
                    due_date=due_date,
                    set_due_date=set_due_date,
                    header=header,
                    body=body,
                    body_append=body_append,
                    label_group=label_group,
                    label_value=label_value,
                    generation=generation,
                )
            finally:
                await asyncio.to_thread(self._release_write_lease, int(task_id), generation)

    async def _update_locked(
        self,
        task_id: int,
        *,
        title: str | None,
        description: str | None,
        done: bool | None,
        priority: int | None,
        due_date: str | None,
        set_due_date: bool,
        header: dict | None,
        body: str | None,
        body_append: str | None,
        label_group: tuple[str, ...] | None,
        label_value: str | None,
        generation: int,
    ) -> Task:
        from flowci.formatting import apply_header

        current = await self._request("GET", f"/tasks/{int(task_id)}")
        raw = current.json()
        if not isinstance(raw, dict):
            raise VikunjaError(502, "task payload was not an object")
        before = copy.deepcopy(raw)
        overlay: dict[str, object] = {}
        if title is not None:
            overlay["title"] = title
        next_description = description
        if next_description is None and (header or body is not None or (body_append or "").strip()):
            next_description = apply_header(
                str(raw.get("description") or ""),
                header or {},
                body=body,
                body_append=body_append,
            )
        if next_description is not None:
            overlay["description"] = next_description
        if done is not None:
            overlay["done"] = bool(done)
        if priority is not None:
            overlay["priority"] = int(priority)
        if set_due_date:
            overlay["due_date"] = to_rfc3339(due_date) if due_date else _ZERO
        if not await asyncio.to_thread(self._generation_current, int(task_id), int(generation)):
            raise VikunjaError(409, "task write lease lost")
        await self._request("POST", f"/tasks/{int(task_id)}", self._writable(raw, overlay))
        if label_group:
            await self._sync_label_group(
                int(task_id),
                raw.get("labels") or [],
                tuple(label_group),
                label_value,
            )
        checked = await self._request("GET", f"/tasks/{int(task_id)}")
        after = checked.json()
        if not isinstance(after, dict):
            raise VikunjaError(502, "task payload was not an object")
        mismatch = _preserved_mismatch(before, after, tuple(label_group) if label_group else None)
        if mismatch:
            raise VikunjaError(409, f"поле {mismatch} изменилось при обновлении")
        return self._parse_task(after)

    def _lease_connection(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS task_write_leases ("
            "task_id INTEGER PRIMARY KEY, owner TEXT NOT NULL, lease_until TEXT NOT NULL, "
            "generation INTEGER NOT NULL DEFAULT 0)"
        )
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(task_write_leases)")}
        if "generation" not in columns:
            connection.execute(
                "ALTER TABLE task_write_leases ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
            )
        return connection

    def _generation_current(self, task_id: int, generation: int) -> bool:
        connection = self._lease_connection()
        try:
            row = connection.execute(
                "SELECT owner, generation FROM task_write_leases WHERE task_id = ?",
                (int(task_id),),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return False
        return str(row[0] or "") == self._write_owner and int(row[1] or 0) == int(generation)

    def _acquire_write_lease(self, task_id: int) -> int:
        deadline = time.monotonic() + 30
        while True:
            connection = self._lease_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT owner, lease_until, generation FROM task_write_leases WHERE task_id = ?",
                    (int(task_id),),
                ).fetchone()
                now = datetime.now(timezone.utc)
                expired = True
                owner = ""
                current_generation = 0
                if row is not None:
                    owner = str(row[0] or "")
                    current_generation = int(row[2] or 0)
                    try:
                        until = datetime.fromisoformat(str(row[1]).replace("Z", "+00:00"))
                    except ValueError:
                        until = now
                    if until.tzinfo is None:
                        until = until.replace(tzinfo=timezone.utc)
                    expired = until <= now
                if row is None or expired or owner == self._write_owner:
                    if row is not None and owner == self._write_owner and not expired:
                        generation = current_generation or 1
                    else:
                        generation = current_generation + 1
                    until_text = (now + timedelta(seconds=30)).replace(microsecond=0).isoformat()
                    connection.execute(
                        "INSERT INTO task_write_leases(task_id, owner, lease_until, generation) "
                        "VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(task_id) DO UPDATE SET owner = excluded.owner, "
                        "lease_until = excluded.lease_until, generation = excluded.generation",
                        (int(task_id), self._write_owner, until_text, generation),
                    )
                    connection.commit()
                    return generation
                connection.rollback()
            finally:
                connection.close()
            if time.monotonic() >= deadline:
                raise VikunjaError(409, "task write lease busy")
            time.sleep(0.02)

    def _release_write_lease(self, task_id: int, generation: int) -> None:
        connection = self._lease_connection()
        try:
            connection.execute(
                "DELETE FROM task_write_leases WHERE task_id = ? AND owner = ? AND generation = ?",
                (int(task_id), self._write_owner, int(generation)),
            )
            connection.commit()
        finally:
            connection.close()

    async def current_user(self) -> tuple[int, str]:
        if self._me_id is not None and self._me_name:
            return self._me_id, self._me_name
        response = await self._request("GET", "/user")
        data = response.json()
        self._me_id = int(data["id"])
        self._me_name = str(data.get("username") or data.get("name") or data["id"])
        return self._me_id, self._me_name

    async def find_user(self, query: str) -> int | None:
        needle = query.strip().casefold()
        if not needle:
            return None
        try:
            items = await self._paginate("/users", {"s": query})
        except VikunjaError as exc:
            if exc.status == 404:
                return None
            raise
        exact: list[int] = []
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            names = [str(item.get("username") or ""), str(item.get("name") or "")]
            if any(name.casefold() == needle for name in names if name):
                exact.append(int(item["id"]))
        if len(exact) == 1:
            return exact[0]
        return None

    async def assign_user(self, task_id: int, user_id: int) -> None:
        await self._request("PUT", f"/tasks/{int(task_id)}/assignees", {"user_id": int(user_id)})

    async def add_comment(self, task_id: int, text: str) -> Comment:
        response = await self._request("PUT", f"/tasks/{task_id}/comments", {"comment": text})
        data = response.json()
        return Comment(
            id=int(data.get("id") or 0),
            task_id=task_id,
            text=str(data.get("comment") or text),
            created=str(data.get("created") or ""),
        )

    async def list_comments(self, task_id: int) -> list[Comment]:
        items = await self._paginate(f"/tasks/{task_id}/comments")
        comments: list[Comment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            comments.append(
                Comment(
                    id=int(item.get("id") or 0),
                    task_id=task_id,
                    text=str(item.get("comment") or ""),
                    created=str(item.get("created") or ""),
                )
            )
        return comments

    async def delete_task(self, task_id: int) -> None:
        await self._request("DELETE", f"/tasks/{task_id}")

    async def assign_me(self, task_id: int) -> None:
        if self._me_id is None:
            response = await self._request("GET", "/user")
            self._me_id = int(response.json()["id"])
        await self._request("PUT", f"/tasks/{task_id}/assignees", {"user_id": self._me_id})

    async def _project_id(self) -> int:
        if self._project_id_value:
            return self._project_id_value
        if self._cached_project_id:
            return self._cached_project_id
        projects = await self._paginate("/projects")
        for project in projects:
            if isinstance(project, dict) and project.get("title") == PROJECT_TITLE:
                self._cached_project_id = int(project["id"])
                return self._cached_project_id
        raise VikunjaError(404, "project FlowCI not found")

    async def _ensure_label(self, title: str) -> int:
        if title in self._labels:
            return self._labels[title]
        await self._load_labels()
        if title in self._labels:
            return self._labels[title]
        response = await self._request(
            "PUT",
            "/labels",
            {"title": title, "hex_color": LABEL_COLORS.get(title, "64748b"), "description": ""},
        )
        label_id = int(response.json()["id"])
        self._labels[title] = label_id
        return label_id

    async def _load_labels(self) -> None:
        items = await self._paginate("/labels")
        for item in items:
            if isinstance(item, dict) and item.get("title") and item.get("id") is not None:
                self._labels[str(item["title"])] = int(item["id"])

    async def _task_lock(self, task_id: int) -> asyncio.Lock:
        async with self._task_locks_guard:
            lock = self._task_locks.get(task_id)
            if lock is None:
                lock = asyncio.Lock()
                self._task_locks[task_id] = lock
            return lock

    async def _sync_label_group(
        self,
        task_id: int,
        current_items: list,
        group: tuple[str, ...],
        selected: str | None,
    ) -> None:
        current: dict[str, int] = {}
        for item in current_items or []:
            if isinstance(item, dict) and item.get("title") and item.get("id") is not None:
                current[str(item["title"])] = int(item["id"])
        blocked = set(group)
        for title, label_id in current.items():
            if title in blocked and title != selected:
                await self._request("DELETE", f"/tasks/{task_id}/labels/{label_id}")
        if selected and selected not in current:
            label_id = await self._ensure_label(selected)
            await self._request("PUT", f"/tasks/{task_id}/labels", {"label_id": label_id})

    async def _sync_labels(self, task_id: int, desired: list[str], current_items: list) -> None:
        current: dict[str, int] = {}
        for item in current_items or []:
            if isinstance(item, dict) and item.get("title") and item.get("id") is not None:
                current[str(item["title"])] = int(item["id"])
        desired_titles: list[str] = []
        seen: set[str] = set()
        for title in desired:
            if title and title not in seen:
                desired_titles.append(title)
                seen.add(title)
        desired_ids: dict[str, int] = {}
        for title in desired_titles:
            if title in current:
                desired_ids[title] = current[title]
            else:
                desired_ids[title] = await self._ensure_label(title)
        wanted = set(desired_ids.values())
        for label_id in current.values():
            if label_id not in wanted:
                await self._request("DELETE", f"/tasks/{task_id}/labels/{label_id}")
        have = set(current.values())
        for label_id in desired_ids.values():
            if label_id not in have:
                await self._request("PUT", f"/tasks/{task_id}/labels", {"label_id": label_id})

    def _writable(self, raw: dict, overlay: dict) -> dict:
        body = copy.deepcopy(raw)
        body.update(overlay)
        body.setdefault("assignees", raw.get("assignees") or [])
        body.setdefault("reminders", raw.get("reminders") or [])
        body.setdefault("is_favorite", bool(raw.get("is_favorite")))
        body.setdefault("repeat_after", raw.get("repeat_after") or 0)
        body.setdefault("repeat_mode", raw.get("repeat_mode") or 0)
        body.setdefault("start_date", raw.get("start_date") or _ZERO)
        body.setdefault("end_date", raw.get("end_date") or _ZERO)
        return body

    def _parse_task(self, item: dict) -> Task:
        labels = _label_titles(item.get("labels"))
        done = bool(item.get("done"))
        if done and not any(label in labels for label in ("Нужно сделать", "В работе", "Заблокировано", "Завершено")):
            labels.append("Завершено")
        return Task(
            id=int(item["id"]),
            title=str(item.get("title") or ""),
            description=str(item.get("description") or ""),
            due_date=normalize_due(item.get("due_date")),
            labels=labels,
            priority=int(item.get("priority") or 0),
            done=done,
            assignees=_assignees(item.get("assignees")),
            project_id=int(item.get("project_id") or 0),
        )

    async def _paginate(self, path: str, params: dict | None = None) -> list:
        page = 1
        items: list = []
        while page <= 20:
            query = dict(params or {})
            query["page"] = page
            query["per_page"] = 50
            response = await self._request("GET", path, params=query)
            data = response.json()
            if isinstance(data, list):
                batch = data
            elif isinstance(data, dict):
                batch = data.get("result") or data.get("tasks") or data.get("data") or []
            else:
                batch = []
            if not isinstance(batch, list):
                batch = []
            items.extend(batch)
            total_pages = response.headers.get("x-pagination-total-pages")
            if total_pages:
                try:
                    if page >= int(total_pages):
                        break
                except ValueError:
                    break
            elif len(batch) < 50:
                break
            page += 1
        return items

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        url = self._root + "/api/v1" + path
        try:
            response = await self._client.request(method, url, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise VikunjaError(0, type(exc).__name__) from None
        if response.status_code >= 400:
            text = response.text[:300]
            if self._token:
                text = text.replace(self._token, "***")
            raise VikunjaError(response.status_code, text)
        return response


def _label_titles(items: object) -> list[str]:
    titles: list[str] = []
    if not isinstance(items, list):
        return titles
    for item in items:
        if isinstance(item, str):
            titles.append(item)
        elif isinstance(item, dict) and item.get("title"):
            titles.append(str(item["title"]))
    return titles


def _preserved_mismatch(before: dict, after: dict, label_group: tuple[str, ...] | None) -> str | None:
    if _assignee_keys(before) != _assignee_keys(after):
        return "assignees"
    if _reminder_keys(before) != _reminder_keys(after):
        return "reminders"
    if bool(before.get("is_favorite")) != bool(after.get("is_favorite")):
        return "is_favorite"
    for key in ("repeat_after", "repeat_mode"):
        if int(before.get(key) or 0) != int(after.get(key) or 0):
            return key
    for key in ("start_date", "end_date"):
        if _date_norm(before.get(key)) != _date_norm(after.get(key)):
            return key
    group = set(label_group or ())
    before_labels = set(_label_titles(before.get("labels"))) - group
    after_labels = set(_label_titles(after.get("labels"))) - group
    if before_labels != after_labels:
        return "labels"
    return None


def _assignee_keys(raw: dict) -> set[str]:
    found: set[str] = set()
    items = raw.get("assignees") or []
    if not isinstance(items, list):
        return found
    for item in items:
        if isinstance(item, dict):
            if item.get("id") is not None:
                found.add(str(item.get("id")))
            elif item.get("username"):
                found.add(str(item["username"]).casefold())
        elif item:
            found.add(str(item).casefold())
    return found


def _reminder_keys(raw: dict) -> set[str]:
    found: set[str] = set()
    items = raw.get("reminders") or []
    if not isinstance(items, list):
        return found
    for item in items:
        if isinstance(item, dict):
            stamp = str(item.get("reminder") or "")[:19]
            relative = str(item.get("relative_period") or item.get("relative_to") or "")
            found.add(stamp + "|" + relative)
        elif item:
            found.add(str(item))
    return found


def _date_norm(value: object) -> str:
    text = str(value or "")
    if not text or text.startswith("0001-01-01") or text in {"None", "null"}:
        return ""
    return text[:19]


def _assignees(items: object) -> list[str]:
    names: list[str] = []
    if not isinstance(items, list):
        return names
    for item in items:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict):
            names.append(str(item.get("username") or item.get("name") or item.get("id") or ""))
    return [name for name in names if name]
