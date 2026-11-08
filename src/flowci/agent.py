"""Решение модели и проверка перед записью в Vikunja."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from flowci.config import Settings, resolve_timezone
from flowci.formatting import (
    COMMENT_KINDS,
    HARNESS_SOURCES,
    MIN_CONFIDENCE,
    PLAN_LABELS,
    STATUS_DONE,
    STATUS_LABELS,
    STATUS_TODO,
    apply_header,
    build_comment,
    classify_due,
    contains_html,
    format_plan,
    format_report,
    overdue_lines,
    parse_description,
    parse_local_dt,
    personal_task,
    plan_from_text,
    planned_date_for,
    relative_day,
    render_event,
    strip_tags,
    to_utc_iso,
)
from flowci.store import Store
from flowci.vikunja import Task, VikunjaError, marker_matches

log = logging.getLogger("flowci.agent")

ACTIONS = frozenset(
    {"find", "create", "append", "change_status", "plan", "remind", "report", "note", "ask"}
)
_SINGLE_TASK_ACTIONS = frozenset({"create", "append", "change_status", "find"})
_HUMAN_SOURCES = frozenset({"telegram", "cli"})
_SELF = frozenset({"я", "мне", "меня", "owner", "self", "я сам"})
_STOP = frozenset(
    {
        "что",
        "мне",
        "меня",
        "сегодня",
        "завтра",
        "делать",
        "задача",
        "задачу",
        "задачи",
        "пожалуйста",
        "нужно",
        "сделать",
        "есть",
        "это",
        "как",
        "для",
        "или",
        "при",
        "уже",
        "еще",
        "только",
        "тебе",
    }
)
_WORD = re.compile(r"[0-9A-Za-zА-Яа-яЁё]{4,}")
_TASK_ID = re.compile(r"#(\d+)|(?:задач\w*|task|номер)\s*#?\s*(\d+)", re.IGNORECASE)
_INSTRUCTION = re.compile(
    r"(созда(?:й|йте|ть)\s+задач|поставь\s+задач|напомни|удали|закрой|заверши|перенеси\s+(?:срок|дедлайн)|что\s+мне)",
    re.IGNORECASE,
)
_PAST_OK = re.compile(r"в прошлом|уже прошло|вс[её]\s+равно|задним числом|даже если", re.IGNORECASE)
_CUES = (
    re.compile(r"созда(?:й|йте|ть)\s+задач", re.IGNORECASE),
    re.compile(r"\bнапомни", re.IGNORECASE),
    re.compile(r"\b(?:закрой|заверши|удали)\b", re.IGNORECASE),
    re.compile(r"перенеси\s+(?:срок|дедлайн|дату)", re.IGNORECASE),
    re.compile(r"поставь\s+статус|переведи\s+в\s+работ", re.IGNORECASE),
)
_CANDIDATE_LIMIT = 12

SYSTEM_PROMPT = """Ты Task Agent FlowCI. Отвечай только одним JSON-объектом без Markdown.
Схема:
{
  "action": "find|create|append|change_status|plan|remind|report|note|ask",
  "task_id": null,
  "confidence": 0.0,
  "needs_confirmation": false,
  "payload": {},
  "user_message": ""
}
Правила:
- Одно сообщение выполняет одну операцию. Если в тексте несколько независимых действий, action=ask и спроси, какое сделать первым.
- find только ищет, ничего не меняет.
- create — новая обязанность. payload: title, expected_result, next_step, confirmed_result, body, due_date (YYYY-MM-DD или null), no_deadline, plan (Сегодня|Завтра|Неделя|Позже или null), status (Нужно сделать|В работе|Заблокировано), assignee, comment, comment_kind (факт|предположение|ответ человека). Если исполнитель или срок неизвестны, action=ask. assignee="я", если обязательство на пользователе. Явное «срока нет» — no_deadline=true и due_date=null.
- append дополняет задачу и не меняет статус. Нужен task_id. Можно обновить next_step, expected_result, plan и due_date, только если человек явно перенёс срок.
- Если кандидатов две и больше, action=ask и payload.candidates — список id. Дубликат не создавай.
- change_status меняет метку статуса. Завершено только когда человек явно просит закрыть задачу, и needs_confirmation=true. Коммит, резюме сессии и событие Cursor завершением не являются: тогда append или note.
- Событие Cursor не меняет due_date, статус Завершено и confirmed_result. confirmed_result ставь только если это слова человека, а не поручение «создай задачу».
- plan показывает план. payload.horizon = today|tomorrow|week|later. Ничего не меняй, дедлайны не трогай.
- remind: payload.in_minutes или fire_at (ISO, местное время без пояса) и text.
- report — недельный отчёт. Текст отчёта соберёт код. user_message оставь пустым.
- note сохраняет информацию без новой задачи. payload.text.
- ask — один короткий вопрос.
- confidence от 0 до 1. Ниже 0.67 код задаст вопрос и ничего не запишет.
- Не выдумывай факты, сроки и исполнителей. В сообщении есть now, today и timezone — считай сегодня/завтра от них.
- Дедлайн due_date и дата плана planned_on — разные поля.
- Удаление: action=change_status, payload.delete=true, needs_confirmation=true.
"""


class LanguageModel(Protocol):
    async def complete(self, system: str, user: str) -> str: ...

    async def transcribe(self, path: Path) -> str: ...


@dataclass
class Decision:
    action: str
    task_id: int | None
    confidence: float
    needs_confirmation: bool
    payload: dict
    user_message: str


@dataclass
class AgentResult:
    action: str
    user_message: str
    task_id: int | None = None
    needs_confirmation: bool = False
    confirmation_token: str | None = None
    noop: bool = False
    created_task: bool = False
    ok: bool = True
    busy: bool = False
    dialog_slot: str | None = None
    dialog_operation: str | None = None
    dialog_payload: dict | None = None
    dialog_original: str | None = None
    dialog_task_id: int | None = None
    event_key: str | None = None
    dialog_source: str | None = None
    dialog_extra: dict | None = None
    dialog_allow_completion: bool | None = None


@dataclass
class TaskAgent:
    settings: Settings
    store: Store
    vikunja: object
    llm: LanguageModel
    clock: object | None = None
    outbox_sender: object | None = None
    _log: logging.Logger = field(default=log, repr=False)

    def _now(self) -> datetime:
        if self.clock is not None:
            return self.clock()  # type: ignore[operator]
        return datetime.now(resolve_timezone(self.settings.flowci_timezone))

    async def transcribe(self, path: Path) -> str:
        return await self.llm.transcribe(path)

    async def handle(
        self,
        text: str,
        *,
        source: str = "cli",
        deduplication_key: str | None = None,
        confirm: bool = False,
        allow_completion: bool = True,
        session_id: str | None = None,
        attachments: list[dict] | None = None,
        extra: dict | None = None,
        chat_id: int | str | None = None,
        user_id: int | None = None,
        reply_to: int | None = None,
    ) -> AgentResult:
        chat_key = str(chat_id) if chat_id is not None else source
        try:
            result = await self._dispatch(
                text,
                source=source,
                deduplication_key=deduplication_key,
                confirm=confirm,
                allow_completion=allow_completion,
                session_id=session_id,
                attachments=attachments,
                extra=extra,
                chat_key=chat_key,
                user_id=user_id,
                reply_to=reply_to,
            )
        except VikunjaError as exc:
            self._log.warning("vikunja http %s", exc.status)
            result = AgentResult(action="ask", user_message=_vikunja_message(exc), ok=False)
        except RuntimeError:
            self._log.warning("model call failed")
            result = AgentResult(action="ask", user_message="Не удалось обратиться к модели.", ok=False)
        try:
            await self._finalize(
                result,
                text=text,
                source=source,
                deduplication_key=deduplication_key,
                chat_key=chat_key,
                user_id=user_id,
                attachments=attachments,
                extra=extra,
                allow_completion=allow_completion,
            )
            return result
        finally:
            self.store.release_held_claims()

    async def handle_event(self, event: dict) -> AgentResult:
        return await self.handle(
            render_event(event),
            source=str(event.get("source") or "cursor"),
            deduplication_key=str(event.get("deduplication_key") or "") or None,
            confirm=False,
            allow_completion=False,
            session_id=str(event.get("session_id") or "") or None,
            extra=event,
        )

    async def confirm(self, token: str, user_id: int | None = None) -> AgentResult:
        pending = self.store.claim_pending(token, user_id)
        if pending is None:
            return self._ask("Подтверждение не найдено или уже использовано.")
        try:
            decision = Decision(
                action=str(pending["action"]),
                task_id=pending.get("task_id"),
                confidence=1.0,
                needs_confirmation=False,
                payload=pending.get("payload") or {},
                user_message="",
            )
            tasks = await self.vikunja.list_tasks()
            if decision.action != "change_status":
                self.store.restore_pending(token)
                return self._ask("Нечего подтверждать.")
            result = await self._change_status(
                decision,
                tasks,
                str(pending.get("source") or "telegram"),
                confirm=True,
                user_id=user_id,
                deduplication_key=None,
                text="",
                extra=None,
                attachments=None,
            )
        except VikunjaError as exc:
            self.store.restore_pending(token)
            return AgentResult(action="ask", user_message=_vikunja_message(exc), ok=False)
        except Exception:
            self._log.warning("confirm failed")
            self.store.restore_pending(token)
            return AgentResult(action="ask", user_message="Не получилось обработать сообщение.", ok=False)
        if result.action == "ask" or not result.ok:
            self.store.restore_pending(token)
            return result
        self.store.finish_pending(token)
        summary = result.user_message
        if user_id is not None:
            summary = f"user:{user_id} {summary}"
        self.store.audit(str(pending.get("source") or "telegram"), result.action, result.task_id, summary)
        return result

    async def cancel(self, token: str, user_id: int | None = None) -> AgentResult:
        if not self.store.cancel_pending(token, user_id):
            return AgentResult(action="note", user_message="Подтверждение не найдено или уже использовано.")
        return AgentResult(action="note", user_message="Отменил. Статус не менял.")

    async def morning_digest(self) -> str:
        tasks = await self.vikunja.list_tasks()
        owner = await self._owner_name()
        now = self._now()
        visible = [task for task in tasks if personal_task(task, owner)]
        lines = [
            "Доброе утро.",
            format_plan(visible, "today", today=now.date(), owner=owner),
            "Просроченный дедлайн:",
        ]
        lines.extend(overdue_lines(visible, now) or ["—"])
        return "\n".join(lines)

    async def _dispatch(
        self,
        text: str,
        *,
        source: str,
        deduplication_key: str | None,
        confirm: bool,
        allow_completion: bool,
        session_id: str | None,
        attachments: list[dict] | None,
        extra: dict | None,
        chat_key: str,
        user_id: int | None,
        reply_to: int | None = None,
    ) -> AgentResult:
        renewal: asyncio.Task[None] | None = None
        try:
            if deduplication_key:
                opened = self.store.open_event(
                    deduplication_key,
                    {
                        "text": text,
                        "source": source,
                        "extra": _public_extra(extra),
                        "allow_completion": allow_completion,
                    },
                )
                if opened.get("busy"):
                    return AgentResult(
                        action="note",
                        user_message="already processing",
                        task_id=opened.get("task_id"),
                        noop=True,
                        busy=True,
                    )
                if opened["stage"] == "done":
                    return AgentResult(
                        action=str(opened.get("action") or "note"),
                        user_message="Событие уже обработано, повторных изменений нет.",
                        task_id=opened.get("task_id"),
                        noop=True,
                    )
                if opened["stage"] == "awaiting_input":
                    question = opened.get("user_message") or "Уточните, пожалуйста."
                    return AgentResult(
                        action="ask",
                        user_message=question,
                        task_id=opened.get("task_id"),
                        event_key=deduplication_key,
                    )
                renewal = asyncio.create_task(self._keep_claim(deduplication_key))
                if opened.get("task_id") and (
                    opened["stage"] in {"task_ready", "comment_pending"} or opened.get("resume")
                ):
                    return await self._resume_external(deduplication_key, opened)
                if opened.get("resume"):
                    matches = await self._marker_matches(deduplication_key)
                    if len(matches) > 1:
                        self.store.set_event(
                            deduplication_key,
                            stage="done",
                            action="note",
                            message="Маркер события найден в нескольких задачах. Ничего не менял.",
                        )
                        return AgentResult(
                            action="note",
                            user_message="Маркер события найден в нескольких задачах. Ничего не менял.",
                            ok=False,
                        )
                    if len(matches) == 1:
                        found = matches[0]
                        spec = dict(opened.get("payload") or {})
                        spec.setdefault("action", "create")
                        self.store.set_event(
                            deduplication_key,
                            stage="task_ready",
                            task_id=found.id,
                            action=str(spec.get("action") or "create"),
                            payload=spec,
                        )
                        opened = dict(opened)
                        opened["task_id"] = found.id
                        opened["payload"] = spec
                        return await self._resume_external(deduplication_key, opened)
            else:
                if _is_cancel(text):
                    cancelled = self._cancel_dialog(chat_key, reply_to)
                    if cancelled is not None:
                        return cancelled
                selected = self._select_dialog(chat_key, reply_to)
                if isinstance(selected, AgentResult):
                    return selected
                if selected is not None:
                    return await self._resume_dialog(
                        selected,
                        text,
                        source=source,
                        chat_key=chat_key,
                        user_id=user_id,
                        session_id=session_id,
                        attachments=attachments,
                        allow_completion=allow_completion,
                    )
                if source in _HUMAN_SOURCES and _multiple_actions(text):
                    return self._ask(
                        "В сообщении несколько действий. Какое сделать первым?",
                        slot="choose",
                        operation="choose",
                        payload={"original_text": text},
                        original=text,
                    )
            return await self._from_model(
                text,
                source=source,
                extra=extra,
                confirm=confirm,
                allow_completion=allow_completion,
                session_id=session_id,
                attachments=attachments,
                user_id=user_id,
                deduplication_key=deduplication_key,
            )
        finally:
            if renewal is not None:
                renewal.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal

    async def _from_model(
        self,
        text: str,
        *,
        source: str,
        extra: dict | None,
        confirm: bool,
        allow_completion: bool,
        session_id: str | None,
        attachments: list[dict] | None,
        user_id: int | None,
        deduplication_key: str | None,
    ) -> AgentResult:
        tasks = await self.vikunja.list_tasks()
        decision = await self._decide(text, source, extra, tasks)
        decision = self._enforce(
            decision,
            text,
            source,
            allow_completion,
            extra,
            attachments=attachments,
            event_key=deduplication_key,
        )
        guarded = self._guard(decision, tasks, text)
        if guarded is not None:
            return guarded
        return await self._apply(
            decision,
            tasks,
            text=text,
            source=source,
            confirm=confirm,
            session_id=session_id,
            user_id=user_id,
            attachments=attachments,
            extra=extra,
            deduplication_key=deduplication_key,
        )

    async def _finalize(
        self,
        result: AgentResult,
        *,
        text: str,
        source: str,
        deduplication_key: str | None,
        chat_key: str,
        user_id: int | None,
        attachments: list[dict] | None,
        extra: dict | None = None,
        allow_completion: bool = True,
    ) -> None:
        if result.noop:
            return
        event_key = deduplication_key or result.event_key
        if event_key:
            stage = self.store.event_stage(event_key)
            if stage not in {"task_ready", "comment_pending", "done"}:
                if result.action == "ask" and result.ok and not result.needs_confirmation:
                    if stage != "awaiting_input" or (result.user_message and result.user_message != self._event_question(event_key)):
                        self.store.set_event(
                            event_key,
                            stage="awaiting_input",
                            action="ask",
                            task_id=result.task_id,
                            message=result.user_message,
                        )
                elif result.ok and result.action != "ask":
                    self.store.set_event(
                        event_key,
                        stage="done",
                        action=result.action,
                        task_id=result.task_id,
                        message=result.user_message,
                    )
        if result.ok and result.action != "ask" and not result.needs_confirmation:
            self.store.clear_dialog(chat_key, event_key)
        saving_question = result.action == "ask" and result.ok and not result.needs_confirmation
        if saving_question:
            stored = self._owned_dialog(event_key, chat_key)
            repeated_question = (
                result.dialog_operation is None
                and stored is not None
                and stored.get("question") == result.user_message
            )
            if not repeated_question:
                original = result.dialog_original or (stored or {}).get("original_text") or text
                payload = result.dialog_payload if result.dialog_payload is not None else {}
                operation = result.dialog_operation or "continue"
                slot = result.dialog_slot or "free"
                task_id = result.dialog_task_id if result.dialog_task_id is not None else result.task_id
                policy_source = result.dialog_source or (stored or {}).get("source") or source
                policy_extra = result.dialog_extra if result.dialog_extra is not None else (stored or {}).get("extra")
                if policy_extra is None:
                    policy_extra = _public_extra(extra)
                policy_allow = result.dialog_allow_completion
                if policy_allow is None and stored is not None:
                    policy_allow = stored.get("allow_completion")
                if policy_allow is None:
                    policy_allow = allow_completion
                linked_event = event_key or (stored or {}).get("event_id")
                self.store.save_dialog(
                    chat_key,
                    original,
                    result.user_message,
                    slot,
                    linked_event,
                    operation,
                    payload,
                    task_id,
                    source=policy_source,
                    extra=policy_extra,
                    allow_completion=bool(policy_allow),
                )
                if linked_event:
                    self.store.save_dialog(
                        f"event:{linked_event}",
                        original,
                        result.user_message,
                        slot,
                        linked_event,
                        operation,
                        payload,
                        task_id,
                        source=policy_source,
                        extra=policy_extra,
                        allow_completion=bool(policy_allow),
                    )
        if event_key and saving_question:
            self.store.enqueue_outbox(event_key, result.user_message)
            await self._deliver_outbox()
        if result.ok and attachments and self._owns_claim(event_key):
            await self._index_attachments(result, attachments, source)
        if result.ok and result.action != "ask":
            summary = result.user_message
            if user_id is not None:
                summary = f"user:{user_id} {summary}"
            self.store.audit(source, result.action, result.task_id, summary)
            self._log.info("action=%s task_id=%s", result.action, result.task_id)

    async def _deliver_outbox(self) -> None:
        sender = self.outbox_sender
        if sender is None:
            return
        for item in self.store.pending_outbox():
            if not self.store.claim_outbox(int(item["id"])):
                continue
            try:
                delivered = await sender(item["text"])  # type: ignore[operator]
            except Exception:
                delivered = False
            message_id = _message_id(delivered)
            if delivered is True or message_id is not None:
                self.store.ack_outbox(int(item["id"]))
                if message_id is not None and item.get("event_key"):
                    self.store.bind_question_message(str(item["event_key"]), message_id)
            else:
                self.store.release_outbox(int(item["id"]))

    async def _decide(self, text: str, source: str, extra: dict | None, tasks: list[Task]) -> Decision:
        now = self._now()
        snapshot = []
        for task in search_tasks(text, tasks, _CANDIDATE_LIMIT):
            fields, _body = parse_description(task.description)
            snapshot.append(
                {
                    "id": task.id,
                    "title": task.title[:200],
                    "labels": task.labels,
                    "due_date": task.due_date,
                    "done": task.done,
                    "expected_result": fields["Ожидаемый результат"][:300],
                    "next_step": fields["Следующий шаг"][:300],
                    "confirmed_result": fields["Последний подтверждённый результат"][:300],
                    "planned_on": fields["Когда планируется"][:40],
                    "description_text": strip_tags(task.description)[:500] if contains_html(task.description) else "",
                }
            )
        user = json.dumps(
            {
                "source": source,
                "text": text,
                "now": now.isoformat(),
                "today": now.date().isoformat(),
                "timezone": self.settings.flowci_timezone,
                "tasks": snapshot,
                "notes": self.store.list_notes()[-20:],
                "extra": _public_extra(extra),
            },
            ensure_ascii=False,
        )
        raw = await self.llm.complete(SYSTEM_PROMPT, user)
        return parse_decision(raw)

    def _enforce(
        self,
        decision: Decision,
        text: str,
        source: str,
        allow_completion: bool,
        extra: dict | None,
        attachments: list[dict] | None = None,
        event_key: str | None = None,
    ) -> Decision:
        harness = (not allow_completion) or source in HARNESS_SOURCES
        if harness and _is_completion(decision):
            decision = _downgrade_completion(decision, text)
        payload = dict(decision.payload)
        today = self._now().date()
        if harness:
            if decision.action != "create":
                payload.pop("due_date", None)
                payload.pop("no_deadline", None)
        else:
            kind, iso = classify_due(text, today)
            if kind == "keep":
                payload.pop("due_date", None)
                payload.pop("no_deadline", None)
            elif kind in {"clear", "none"}:
                payload["no_deadline"] = True
                payload["due_date"] = None
            elif kind in {"set", "date"} and iso:
                payload["due_date"] = iso
                payload.pop("no_deadline", None)
            else:
                payload.pop("no_deadline", None)
                payload.pop("due_date", None)
            plan = plan_from_text(text)
            if plan:
                payload["plan"] = plan
            if payload.get("plan") in PLAN_LABELS and not payload.get("planned_on"):
                payload["planned_on"] = planned_date_for(str(payload["plan"]), today)
        if not _confirmed_allowed(source, text, extra, attachments, event_key):
            payload.pop("confirmed_result", None)
        return Decision(
            decision.action,
            decision.task_id,
            decision.confidence,
            decision.needs_confirmation,
            payload,
            decision.user_message,
        )

    def _guard(self, decision: Decision, tasks: list[Task], text: str) -> AgentResult | None:
        raw = decision.payload.get("candidates")
        if isinstance(raw, list) and len(raw) >= 2 and decision.action in _SINGLE_TASK_ACTIONS:
            operation = "append" if decision.action in {"create", "append"} else decision.action
            return self._ask(
                _candidate_question(decision, tasks),
                decision.task_id,
                slot="task_id",
                operation=operation,
                payload=dict(decision.payload),
            )
        if decision.action != "ask" and decision.confidence < MIN_CONFIDENCE:
            message = decision.user_message or "Не уверен, как это записать. Уточните одним сообщением."
            return self._ask(
                message,
                decision.task_id,
                slot="free",
                operation="continue",
                payload=dict(decision.payload),
            )
        if decision.action == "create":
            return self._create_conflict(decision, tasks)
        return None

    def _create_conflict(self, decision: Decision, tasks: list[Task]) -> AgentResult | None:
        title = str(decision.payload.get("title") or "")
        ranked = rank_tasks(title, tasks)
        strong = [(score, task) for score, task in ranked if score >= 0.5]
        payload = dict(decision.payload)
        if len(strong) >= 2 and strong[0][0] - strong[1][0] < 0.2:
            left, right = strong[0][1], strong[1][1]
            return self._ask(
                f"Похоже сразу на #{left.id} «{left.title}» и #{right.id} «{right.title}». Какую дополнить?",
                slot="task_id",
                operation="append",
                payload=payload,
            )
        if strong and strong[0][0] >= 0.75:
            task = strong[0][1]
            payload["existing_task_id"] = task.id
            return self._ask(
                f"Похоже на задачу #{task.id} «{task.title}». Дополнить её? Новую пока не создаю.",
                task_id=task.id,
                slot="confirm_task",
                operation="create",
                payload=payload,
                dialog_task_id=task.id,
            )
        return None

    def _event_question(self, event_key: str) -> str:
        dialog = self.store.get_dialog(f"event:{event_key}")
        if dialog is None:
            return ""
        return str(dialog.get("question") or "")

    def _select_dialog(self, chat_key: str, reply_to: int | None) -> dict | AgentResult | None:
        if reply_to is not None:
            bound = self.store.find_dialog_by_message(int(reply_to))
            if bound is None or not self._dialog_open(bound):
                return AgentResult(action="note", user_message="Этот вопрос уже закрыт.", noop=True)
            return bound
        local = self.store.get_dialog(chat_key)
        if local is not None and local.get("operation") == "choose_event":
            return local
        if local is not None and not local.get("event_id"):
            return local
        waiting = self.store.waiting_event_dialogs()
        if local is not None and local.get("event_id") and len(waiting) <= 1:
            return local
        if len(waiting) > 1:
            return self._ask_which(waiting)
        if len(waiting) == 1:
            return waiting[0]
        return local

    def _cancel_dialog(self, chat_key: str, reply_to: int | None) -> AgentResult | None:
        if reply_to is not None:
            bound = self.store.find_dialog_by_message(int(reply_to))
            dialogs = [bound] if bound is not None else []
        else:
            local = self.store.get_dialog(chat_key)
            waiting = self.store.waiting_event_dialogs()
            if waiting:
                dialogs = waiting
            elif local is not None:
                dialogs = [local]
            else:
                dialogs = []
        if not dialogs:
            return None
        for dialog in dialogs:
            event_id = str(dialog.get("event_id") or "")
            self.store.clear_dialog(str(dialog.get("chat_key") or chat_key), event_id or None)
            if event_id:
                self.store.set_event(event_id, stage="done", action="note", message="Отменил.")
                self.store.drop_outbox(event_id)
        self.store.clear_dialog(chat_key)
        return AgentResult(action="note", user_message="Отменил. Ничего не менял.")

    def _ask_which(self, waiting: list[dict]) -> AgentResult:
        lines = ["Несколько вопросов ждут ответа. Ответьте реплаем на нужный или номером:"]
        events = []
        for index, dialog in enumerate(waiting, start=1):
            question = str(dialog.get("question") or dialog.get("original_text") or "вопрос")
            lines.append(f"{index}. {question}")
            events.append({"event_id": dialog.get("event_id"), "question": question})
        return self._ask("\n".join(lines), slot="choose_event", operation="choose_event", payload={"events": events})

    def _remember_answer(self, dialog: dict, answer: str) -> None:
        answers = [str(item) for item in (dialog.get("answers") or [])]
        clean = answer.strip()
        if clean and (not answers or answers[-1] != clean):
            answers.append(clean)
        dialog["answers"] = answers
        extra = dialog.get("extra") if isinstance(dialog.get("extra"), dict) else None
        self.store.save_dialog(
            str(dialog.get("chat_key") or ""),
            str(dialog.get("original_text") or ""),
            str(dialog.get("question") or ""),
            str(dialog.get("expected_slot") or ""),
            dialog.get("event_id"),
            str(dialog.get("operation") or ""),
            dict(dialog.get("payload") or {}),
            dialog.get("task_id"),
            source=str(dialog.get("source") or "") or None,
            extra=extra,
            allow_completion=dialog.get("allow_completion"),
            answers=answers,
            question_message_id=dialog.get("question_message_id"),
            question_version=dialog.get("question_version"),
        )

    def _stamp(self, result: AgentResult, dialog: dict) -> AgentResult:
        if not result.dialog_original:
            result.dialog_original = str(dialog.get("original_text") or "") or None
        if result.event_key is None and dialog.get("event_id"):
            result.event_key = str(dialog.get("event_id"))
        if result.dialog_source is None and dialog.get("source"):
            result.dialog_source = str(dialog.get("source"))
        if result.dialog_extra is None and isinstance(dialog.get("extra"), dict):
            result.dialog_extra = dict(dialog.get("extra") or {})
        if result.dialog_allow_completion is None and dialog.get("allow_completion") is not None:
            result.dialog_allow_completion = bool(dialog.get("allow_completion"))
        return result

    async def _keep_claim(self, key: str) -> None:
        while True:
            await asyncio.sleep(30)
            if not self.store.renew_claim(key):
                return

    def _owns_claim(self, key: str | None) -> bool:
        if not key:
            return True
        if self.store._context_generation(key) is not None:
            return self.store.renew_claim(key)
        return self.store.claim_if_free(key)

    def _lost_claim(self, action: str, task_id: int | None = None) -> AgentResult:
        return AgentResult(
            action=action,
            user_message="Событие уже обрабатывается другим процессом.",
            task_id=task_id,
            ok=False,
            noop=True,
        )

    def _owned_dialog(self, event_key: str | None, chat_key: str) -> dict | None:
        if event_key:
            stored = self.store.get_dialog(f"event:{event_key}")
            if stored and str(stored.get("event_id") or "") == str(event_key):
                return stored
            return None
        stored = self.store.get_dialog(chat_key)
        if stored is None or stored.get("event_id"):
            return None
        return stored

    def _dialog_open(self, dialog: dict) -> bool:
        event_id = str(dialog.get("event_id") or "")
        if not event_id:
            return True
        return self.store.event_stage(event_id) == "awaiting_input"

    async def _marker_matches(self, key: str) -> list[Task]:
        marker = f"flowci-event:{key}"
        tasks = await self.vikunja.list_tasks()
        return [task for task in tasks if marker_matches(task.description or "", marker)]

    async def _task_by_marker(self, key: str) -> Task | None:
        matches = await self._marker_matches(key)
        if len(matches) != 1:
            return None
        return matches[0]

    async def _resume_dialog(
        self,
        dialog: dict,
        text: str,
        *,
        source: str,
        chat_key: str,
        user_id: int | None,
        session_id: str | None,
        attachments: list[dict] | None,
        allow_completion: bool,
    ) -> AgentResult:
        if dialog.get("operation") == "choose_event":
            waiting = self.store.waiting_event_dialogs()
            matched = _match_waiting(text, waiting)
            if matched is None:
                return self._ask_which(waiting)
            dialog = matched
        self._remember_answer(dialog, text)
        result = await self._resume_saved(
            dialog,
            text,
            source=source,
            user_id=user_id,
            session_id=session_id,
            attachments=attachments,
            allow_completion=allow_completion,
        )
        return self._stamp(result, dialog)

    async def _resume_saved(
        self,
        dialog: dict,
        text: str,
        *,
        source: str,
        user_id: int | None,
        session_id: str | None,
        attachments: list[dict] | None,
        allow_completion: bool,
    ) -> AgentResult:
        origin_source = str(dialog.get("source") or source)
        origin_extra = dialog.get("extra") if dialog.get("source") else None
        if not isinstance(origin_extra, dict):
            origin_extra = None
        origin_allow = dialog.get("allow_completion")
        if origin_allow is None:
            origin_allow = allow_completion
        if dialog["operation"] in {"choose", "continue"}:
            if dialog["operation"] == "choose":
                combined = f"{dialog['original_text']}\nВыбранное действие: {text}\nВыполни только его."
            else:
                combined = _dialog_prompt(dialog, text)
            return await self._from_model(
                combined,
                source=origin_source,
                extra=origin_extra,
                confirm=False,
                allow_completion=bool(origin_allow),
                session_id=session_id,
                attachments=attachments,
                user_id=user_id,
                deduplication_key=dialog.get("event_id"),
            )
        filled = _fill_slot(dialog["expected_slot"], text, self._now().date())
        payload = dict(dialog["payload"])
        payload.update({key: value for key, value in filled.items() if key != "confirm_existing"})
        task_id = filled.get("task_id", dialog.get("task_id"))
        operation = str(dialog["operation"] or "continue")
        if dialog["expected_slot"] == "confirm_task":
            if filled.get("confirm_existing") is True:
                operation = "append"
                task_id = payload.get("existing_task_id") or task_id
            elif filled.get("confirm_existing") is False:
                operation = "create"
                task_id = None
            else:
                return self._ask(
                    dialog["question"],
                    slot="confirm_task",
                    operation="create",
                    payload=payload,
                    original=dialog["original_text"],
                    dialog_task_id=dialog.get("task_id"),
                )
        if operation == "create":
            if dialog["expected_slot"] == "title" and filled.get("title"):
                payload["title"] = filled["title"]
            missing = _missing_create(payload)
            if not str(payload.get("title") or "").strip():
                return self._ask(
                    "Как коротко назвать задачу?",
                    slot="title",
                    operation="create",
                    payload=payload,
                    original=dialog["original_text"],
                )
            if missing:
                return self._ask(
                    _missing_question(str(payload.get("title") or ""), missing),
                    slot=_slot_for(missing),
                    operation="create",
                    payload=payload,
                    original=dialog["original_text"],
                )
        if operation in {"append", "change_status", "find"} and task_id is None:
            return self._ask(
                "Назовите номер задачи.",
                slot="task_id",
                operation=operation,
                payload=payload,
                original=dialog["original_text"],
            )
        decision = Decision(operation, _as_int(task_id), 1.0, False, payload, "")
        human_text = _human_corpus(dialog, text)
        decision = self._enforce(
            decision,
            human_text,
            origin_source,
            bool(origin_allow),
            origin_extra,
            attachments=attachments,
            event_key=str(dialog.get("event_id") or "") or None,
        )
        tasks = await self.vikunja.list_tasks()
        return await self._apply(
            decision,
            tasks,
            text=str(dialog.get("original_text") or text),
            source=origin_source,
            confirm=False,
            session_id=session_id,
            user_id=user_id,
            attachments=attachments,
            extra=origin_extra,
            deduplication_key=dialog.get("event_id"),
        )

    async def _resume_external(self, key: str, opened: dict) -> AgentResult:
        spec = dict(opened.get("payload") or {})
        task_id = int(opened["task_id"])
        error = await self._follow_external(key, task_id, spec)
        if error is not None:
            return error
        action = str(spec.get("action") or "create")
        title = str(spec.get("title") or "")
        if action == "create":
            message = f"Создал задачу #{task_id}: {title}".strip()
        elif action == "change_status":
            message = f"Статус задачи #{task_id}: {spec.get('status') or 'обновлён'}."
        else:
            message = f"Дополнил задачу #{task_id} без смены статуса."
        return AgentResult(action=action, user_message=message, task_id=task_id, created_task=action == "create")

    async def _apply(
        self,
        decision: Decision,
        tasks: list[Task],
        *,
        text: str,
        source: str,
        confirm: bool,
        session_id: str | None,
        user_id: int | None,
        attachments: list[dict] | None,
        extra: dict | None,
        deduplication_key: str | None,
    ) -> AgentResult:
        action = decision.action
        if action == "ask":
            return self._ask(decision.user_message or "Уточните, пожалуйста.", decision.task_id, slot="free", operation="continue", payload=dict(decision.payload))
        if action == "create":
            return await self._create(
                decision, tasks, text, source, confirm, user_id, attachments, extra, deduplication_key
            )
        if action == "append":
            return await self._append(
                decision, tasks, text, source, user_id, attachments, extra, deduplication_key
            )
        if action == "change_status":
            return await self._change_status(
                decision, tasks, source, confirm, user_id, deduplication_key, text, extra, attachments
            )
        if action == "plan":
            horizon = decision.payload.get("horizon") or decision.payload.get("when")
            owner = await self._owner_name()
            return AgentResult(
                action="plan",
                user_message=format_plan(tasks, horizon, today=self._now().date(), owner=owner),
            )
        if action == "remind":
            return self._remind(decision, text)
        if action == "report":
            return await self._report(tasks)
        if action == "note":
            return self._note(decision, text, source, session_id)
        if action == "find":
            return self._find(decision, tasks, text)
        return self._ask("Не понял действие.")

    async def _create(
        self,
        decision: Decision,
        tasks: list[Task],
        text: str,
        source: str,
        confirm: bool,
        user_id: int | None,
        attachments: list[dict] | None,
        extra: dict | None,
        deduplication_key: str | None,
    ) -> AgentResult:
        del tasks
        payload = _guard_confirmed(dict(decision.payload), source, text, extra, attachments, deduplication_key)
        title = str(payload.get("title") or "").strip()
        if not title:
            return self._ask("Как коротко назвать задачу?", slot="title", operation="create", payload=payload)
        missing = _missing_create(payload)
        if missing:
            return self._ask(
                _missing_question(title, missing),
                slot=_slot_for(missing),
                operation="create",
                payload=payload,
            )
        assignee = str(payload.get("assignee") or "").strip()
        assignee_id: int | None = None
        if not _is_self(assignee):
            try:
                assignee_id = await self.vikunja.find_user(assignee)
            except VikunjaError as exc:
                return AgentResult(action="ask", user_message=_vikunja_message(exc), ok=False)
            if assignee_id is None:
                return self._ask(
                    f"Пользователь «{assignee}» не найден в Vikunja. Кого назначить?",
                    slot="assignee",
                    operation="create",
                    payload=payload,
                )
        status = str(payload.get("status") or STATUS_TODO).strip()
        if status not in STATUS_LABELS:
            return self._ask("Не понял статус новой задачи.")
        if status == STATUS_DONE and not confirm:
            return self._ask("Новую задачу сразу закрывать не буду. Подтвердите, если она уже выполнена.")
        plan = payload.get("plan")
        if plan not in (None, "", "null") and plan not in PLAN_LABELS:
            return self._ask("Метка плана должна быть одной из: Сегодня, Завтра, Неделя, Позже.")
        if plan in PLAN_LABELS:
            payload["planned_on"] = planned_date_for(str(plan), self._now().date())
        description = apply_header("", payload, body=str(payload.get("body") or ""))
        labels = [status]
        if plan in PLAN_LABELS:
            labels.append(str(plan))
        due = None if payload.get("no_deadline") else payload.get("due_date")
        marker = f"flowci-event:{deduplication_key}" if deduplication_key else ""
        if marker and not marker_matches(description, marker):
            description = (description.rstrip() + "\n" + marker).strip()
        due_text = None if due in (None, "", "null") else str(due)
        kind = str(payload.get("comment_kind") or _default_kind(source))
        comment = str(payload.get("comment") or text)
        if kind == "факт" and _is_instruction(comment):
            kind = "предположение"
        if kind not in COMMENT_KINDS:
            kind = _default_kind(source)
        created_spec = {
            "action": "create",
            "title": title,
            "created": True,
            "labels": list(labels),
            "labels_done": False,
            "assign_mode": "me" if _is_self(assignee) else "user",
            "assign_user_id": assignee_id,
            "assign_done": False,
            "pending_comment": build_comment(kind, source, comment),
            "comment_done": False,
        }

        async def _journal(task: Task) -> None:
            if deduplication_key:
                self.store.set_event(
                    deduplication_key,
                    stage="task_ready",
                    task_id=task.id,
                    action="create",
                    payload=created_spec,
                )

        if not self._owns_claim(deduplication_key):
            return self._lost_claim("create")
        try:
            task = await self.vikunja.create_task(
                title=title,
                description=description,
                due_date=due_text,
                labels=labels,
                priority=_priority(payload.get("priority")),
                done=status == STATUS_DONE,
                on_created=_journal,
                event_marker=marker or None,
            )
        except VikunjaError as exc:
            known = self.store.get_dedupe(deduplication_key) if deduplication_key else None
            if known and known[1]:
                return AgentResult(
                    action="create",
                    user_message=(
                        f"Задача #{known[1]} записана, но метки не сохранились. Повторите то же событие."
                    ),
                    task_id=known[1],
                    created_task=True,
                    ok=False,
                )
            return AgentResult(action="ask", user_message=_vikunja_message(exc), ok=False)
        created_spec["labels_done"] = True
        self._activity(
            task_id=task.id,
            source=source,
            author=user_id,
            message=text,
            action="create",
            attachments=attachments,
            extra=extra,
        )
        error = await self._follow_external(deduplication_key, task.id, created_spec)
        if error is not None:
            error.created_task = True
            return error
        return AgentResult(
            action="create",
            user_message=f"Создал задачу #{task.id}: {title}",
            task_id=task.id,
            created_task=True,
        )

    async def _append(
        self,
        decision: Decision,
        tasks: list[Task],
        text: str,
        source: str,
        user_id: int | None,
        attachments: list[dict] | None,
        extra: dict | None,
        deduplication_key: str | None,
    ) -> AgentResult:
        resolved = self._resolve_task(decision, tasks, text)
        if isinstance(resolved, AgentResult):
            return resolved
        payload = _guard_confirmed(
            dict(decision.payload),
            source,
            text,
            extra,
            attachments,
            deduplication_key,
        )
        header: dict[str, object] = {}
        for key in ("expected_result", "next_step", "confirmed_result", "planned_on"):
            if key in payload and payload[key] not in (None, ""):
                header[key] = payload[key]
        plan = payload.get("plan")
        label_group = None
        label_value = None
        if plan in PLAN_LABELS:
            label_group = PLAN_LABELS
            label_value = str(plan)
            if "planned_on" not in header:
                header["planned_on"] = planned_date_for(str(plan), self._now().date())
        kwargs: dict[str, object] = {}
        if header or ("body" in payload and payload.get("body") is not None) or payload.get("body_append"):
            kwargs["header"] = header
            if "body" in payload and payload.get("body") is not None:
                kwargs["body"] = str(payload.get("body"))
            if payload.get("body_append"):
                kwargs["body_append"] = str(payload.get("body_append"))
        if payload.get("no_deadline"):
            kwargs["set_due_date"] = True
            kwargs["due_date"] = None
        elif "due_date" in payload and payload.get("due_date") not in (None, "", "null"):
            kwargs["set_due_date"] = True
            kwargs["due_date"] = str(payload.get("due_date"))
        if label_group:
            kwargs["label_group"] = label_group
            kwargs["label_value"] = label_value
        if kwargs:
            if not self._owns_claim(deduplication_key):
                return self._lost_claim("append", resolved.id)
            saved = await self.vikunja.update_task(resolved.id, **kwargs)
        else:
            saved = resolved
        self._activity(
            task_id=saved.id,
            source=source,
            author=user_id,
            message=text,
            action="append",
            attachments=attachments,
            extra=extra,
        )
        comment = payload.get("comment")
        if comment:
            kind = str(payload.get("comment_kind") or _default_kind(source))
            if kind == "факт" and _is_instruction(str(comment)):
                kind = "предположение"
            if kind not in COMMENT_KINDS:
                kind = _default_kind(source)
            error = await self._follow_external(
                deduplication_key,
                saved.id,
                {
                    "action": "append",
                    "title": saved.title,
                    "pending_comment": build_comment(kind, source, str(comment)),
                    "comment_done": False,
                },
            )
            if error is not None:
                return error
        return AgentResult(
            action="append",
            user_message=f"Дополнил задачу #{saved.id} без смены статуса.",
            task_id=saved.id,
        )

    async def _change_status(
        self,
        decision: Decision,
        tasks: list[Task],
        source: str,
        confirm: bool,
        user_id: int | None,
        deduplication_key: str | None,
        text: str,
        extra: dict | None,
        attachments: list[dict] | None,
    ) -> AgentResult:
        payload = decision.payload
        if payload.get("delete"):
            if not confirm:
                return self._request_confirmation(
                    decision,
                    source,
                    f"Подтвердите удаление задачи #{decision.task_id}.",
                    user_id,
                )
            resolved = self._resolve_task(decision, tasks, "")
            if isinstance(resolved, AgentResult):
                return resolved
            if not self._owns_claim(deduplication_key):
                return self._lost_claim("change_status", resolved.id)
            await self.vikunja.delete_task(resolved.id)
            self._activity(
                task_id=resolved.id,
                source=source,
                author=user_id,
                message=text or decision.user_message or "Удаление",
                action="change_status",
                attachments=attachments,
                extra=extra,
            )
            return AgentResult(
                action="change_status",
                user_message=f"Удалил задачу #{resolved.id}.",
                task_id=resolved.id,
            )
        status = str(payload.get("status") or "").strip()
        if status not in STATUS_LABELS:
            return self._ask("Не понял, какой статус поставить.")
        if status == STATUS_DONE and not confirm:
            return self._request_confirmation(
                decision,
                source,
                f"Подтвердите завершение задачи #{decision.task_id}. Без подтверждения статус не меняю.",
                user_id,
            )
        resolved = self._resolve_task(decision, tasks, "")
        if isinstance(resolved, AgentResult):
            return resolved
        if not self._owns_claim(deduplication_key):
            return self._lost_claim("change_status", resolved.id)
        saved = await self.vikunja.update_task(
            resolved.id,
            done=status == STATUS_DONE,
            label_group=STATUS_LABELS,
            label_value=status,
        )
        self._activity(
            task_id=saved.id,
            source=source,
            author=user_id,
            message=text or decision.user_message or f"Статус: {status}",
            action="change_status",
            attachments=attachments,
            extra=extra,
        )
        comment = payload.get("comment") or decision.user_message or f"Статус: {status}"
        kind = str(payload.get("comment_kind") or _default_kind(source))
        if kind == "факт" and _is_instruction(str(comment)):
            kind = "предположение"
        if kind not in COMMENT_KINDS:
            kind = _default_kind(source)
        error = await self._follow_external(
            deduplication_key,
            saved.id,
            {
                "action": "change_status",
                "title": saved.title,
                "status": status,
                "pending_comment": build_comment(kind, source, str(comment)),
                "comment_done": False,
            },
        )
        if error is not None:
            return error
        return AgentResult(
            action="change_status",
            user_message=f"Статус задачи #{saved.id}: {status}.",
            task_id=saved.id,
        )

    def _remind(self, decision: Decision, text: str) -> AgentResult:
        now = self._now()
        fire = _fire_at(decision.payload, now)
        if fire is None:
            return self._ask("Когда напомнить? Назовите время или интервал.")
        if fire < now - timedelta(seconds=30) and not _PAST_OK.search(text or ""):
            return self._ask("Это время уже прошло. Назовите будущее время или явно попросите напомнить задним числом.")
        body = str(decision.payload.get("text") or decision.user_message or text).strip()
        if not body:
            return self._ask("О чём напомнить?")
        stamp = to_utc_iso(fire)
        self.store.add_reminder(stamp, body, decision.task_id)
        return AgentResult(
            action="remind",
            user_message=f"Напомню {stamp}: {body}",
            task_id=decision.task_id,
        )

    async def _report(self, tasks: list[Task]) -> AgentResult:
        comments: dict[int, list] = {}
        for task in tasks:
            comments[task.id] = await self.vikunja.list_comments(task.id)
        return AgentResult(
            action="report",
            user_message=format_report(
                tasks,
                comments,
                self._now(),
                notes=self.store.list_notes_full(),
                activities=self.store.list_activities(),
                artifacts=self.store.list_artifacts(),
            ),
        )

    def _note(self, decision: Decision, text: str, source: str, session_id: str | None) -> AgentResult:
        body = str(decision.payload.get("text") or decision.user_message or text).strip()
        self.store.add_note(body, source, session_id)
        return AgentResult(action="note", user_message="Записал заметку. Задачу не создавал.")

    def _find(self, decision: Decision, tasks: list[Task], text: str) -> AgentResult:
        if decision.task_id is not None:
            for task in tasks:
                if task.id == decision.task_id:
                    return AgentResult(
                        action="find",
                        user_message=f"#{task.id} {task.title}",
                        task_id=task.id,
                    )
            return self._ask(f"Задача #{decision.task_id} не найдена.")
        ranked = [item for item in rank_tasks(text, tasks) if item[0] >= 0.2][:5]
        if not ranked:
            return AgentResult(action="find", user_message="Подходящих задач не нашёл.")
        lines = ["Нашёл:"]
        for _score, task in ranked:
            lines.append(f"- #{task.id} {task.title}")
        return AgentResult(action="find", user_message="\n".join(lines))

    def _resolve_task(self, decision: Decision, tasks: list[Task], text: str) -> Task | AgentResult:
        if decision.task_id is not None:
            for task in tasks:
                if task.id == decision.task_id:
                    return task
            return self._ask(f"Задача #{decision.task_id} не найдена.")
        ranked = rank_tasks(str(decision.payload.get("title") or text), tasks)
        strong = [(score, task) for score, task in ranked if score >= 0.45]
        if len(strong) >= 2 and strong[0][0] - strong[1][0] < 0.2:
            return self._ask(
                _candidate_question(decision, [item[1] for item in strong[:2]]),
                slot="task_id",
                operation=decision.action,
                payload=dict(decision.payload),
            )
        if strong:
            return strong[0][1]
        return self._ask("Не нашёл однозначную задачу. Назовите номер или точное название.")

    def _request_confirmation(
        self,
        decision: Decision,
        source: str,
        question: str,
        user_id: int | None,
    ) -> AgentResult:
        token = secrets.token_hex(8)
        self.store.save_pending(
            token,
            decision.action,
            decision.task_id,
            source,
            decision.payload,
            user_id=user_id,
        )
        return AgentResult(
            action="ask",
            user_message=question,
            task_id=decision.task_id,
            needs_confirmation=True,
            confirmation_token=token,
        )

    def _ask(
        self,
        message: str,
        task_id: int | None = None,
        *,
        slot: str | None = None,
        operation: str | None = None,
        payload: dict | None = None,
        original: str | None = None,
        dialog_task_id: int | None = None,
    ) -> AgentResult:
        text = (message or "").strip() or "Уточните, пожалуйста."
        return AgentResult(
            action="ask",
            user_message=text,
            task_id=task_id,
            dialog_slot=slot,
            dialog_operation=operation,
            dialog_payload=payload,
            dialog_original=original,
            dialog_task_id=dialog_task_id if dialog_task_id is not None else task_id,
        )

    def _activity(
        self,
        *,
        task_id: int | None,
        source: str,
        author: int | None,
        message: str,
        action: str,
        attachments: list[dict] | None,
        extra: dict | None,
    ) -> None:
        kind, path = _evidence(action, source, message, attachments, extra)
        self.store.add_activity(
            task_id=task_id,
            source=source,
            author=str(author if author is not None else source),
            message=message,
            evidence_kind=kind,
            file_path=path,
            action=action,
        )

    async def _follow_external(self, key: str | None, task_id: int, spec: dict) -> AgentResult | None:
        if key:
            self.store.set_event(
                key,
                stage="task_ready",
                task_id=task_id,
                action=str(spec.get("action") or "create"),
                payload=spec,
            )
        labels = [str(item) for item in (spec.get("labels") or []) if str(item).strip()]
        if labels and not spec.get("labels_done"):
            if not self._owns_claim(key):
                return self._lost_claim(str(spec.get("action") or "create"), task_id)
            try:
                await self.vikunja.sync_task_labels(task_id, labels)
            except VikunjaError:
                return AgentResult(
                    action=str(spec.get("action") or "create"),
                    user_message=(
                        f"Задача #{task_id} записана, но метки не сохранились. Повторите то же событие."
                    ),
                    task_id=task_id,
                    created_task=bool(spec.get("created")),
                    ok=False,
                )
            spec["labels_done"] = True
            if key:
                self.store.set_event(key, stage="task_ready", task_id=task_id, payload=spec)
        if spec.get("assign_mode") and not spec.get("assign_done"):
            if await self._assignment_present(task_id, spec):
                spec["assign_done"] = True
                if key:
                    self.store.set_event(key, stage="task_ready", task_id=task_id, payload=spec)
            else:
                if not self._owns_claim(key):
                    return self._lost_claim(str(spec.get("action") or "create"), task_id)
                try:
                    if spec.get("assign_mode") == "me":
                        await self.vikunja.assign_me(task_id)
                    else:
                        await self.vikunja.assign_user(task_id, int(spec["assign_user_id"]))
                    spec["assign_done"] = True
                    if key:
                        self.store.set_event(key, stage="task_ready", task_id=task_id, payload=spec)
                except VikunjaError as exc:
                    return AgentResult(
                        action=str(spec.get("action") or "create"),
                        user_message=(
                            f"Задача #{task_id} записана, но назначить исполнителя не удалось: {_vikunja_message(exc)}"
                        ),
                        task_id=task_id,
                        created_task=bool(spec.get("created")),
                        ok=False,
                    )
        comment = str(spec.get("pending_comment") or "")
        if comment and not spec.get("comment_done"):
            if await self._comment_present(task_id, comment):
                spec["comment_done"] = True
            else:
                if not self._owns_claim(key):
                    return self._lost_claim(str(spec.get("action") or "create"), task_id)
                try:
                    await self.vikunja.add_comment(task_id, comment)
                    spec["comment_done"] = True
                except VikunjaError:
                    if key:
                        self.store.set_event(
                            key,
                            stage="comment_pending",
                            task_id=task_id,
                            action=str(spec.get("action") or "create"),
                            payload=spec,
                            message="Комментарий не записался.",
                        )
                    return AgentResult(
                        action=str(spec.get("action") or "create"),
                        user_message=f"Задача #{task_id} записана, но комментарий не сохранился. Повторите то же событие.",
                        task_id=task_id,
                        created_task=bool(spec.get("created")),
                        ok=False,
                    )
        if key:
            if not self._owns_claim(key):
                return self._lost_claim(str(spec.get("action") or "create"), task_id)
            self.store.set_event(
                key,
                stage="done",
                task_id=task_id,
                action=str(spec.get("action") or "create"),
                payload=spec,
            )
        return None

    async def _assignment_present(self, task_id: int, spec: dict) -> bool:
        try:
            tasks = await self.vikunja.list_tasks()
        except VikunjaError:
            return False
        task = next((item for item in tasks if item.id == task_id), None)
        if task is None:
            return False
        names = {str(name).strip().casefold() for name in (task.assignees or []) if str(name).strip()}
        if not names:
            return False
        if spec.get("assign_mode") == "me":
            owner = await self._owner_name()
            if owner:
                return owner.casefold() in names
            return "me" in names
        return True

    async def _comment_present(self, task_id: int, comment: str) -> bool:
        target = comment.strip()
        if not target:
            return False
        try:
            comments = await self.vikunja.list_comments(task_id)
        except VikunjaError:
            return False
        return any(str(getattr(item, "text", "") or "").strip() == target for item in comments)

    async def _index_attachments(self, result: AgentResult, attachments: list[dict] | None, source: str) -> None:
        for item in attachments or []:
            path = str(item.get("path") or "")
            transcript = str(item.get("transcript") or "")
            kind = str(item.get("kind") or "file")
            self.store.add_artifact(path, kind, result.task_id, transcript, source)
            if result.task_id and result.action in {"create", "append", "change_status"} and (path or transcript):
                try:
                    await self.vikunja.add_comment(
                        result.task_id,
                        build_comment("факт", source, f"Файл: {path}\nТранскрипт: {transcript}".strip()),
                    )
                except VikunjaError:
                    self._log.warning("attachment comment failed")

    async def _owner_name(self) -> str | None:
        method = getattr(self.vikunja, "current_user", None)
        if method is None:
            return None
        try:
            _user_id, name = await method()
        except Exception:
            self._log.warning("current user lookup failed")
            return None
        text = str(name or "").strip()
        return text or None


def parse_decision(raw: str) -> Decision:
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError, TypeError):
        return Decision("ask", None, 0.0, False, {}, "Не разобрал ответ. Сформулируйте короче.")
    action = str(data.get("action") or "ask").strip()
    if action not in ACTIONS:
        action = "ask"
    payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    payload = dict(payload)
    if "candidates" in data and "candidates" not in payload:
        payload["candidates"] = data["candidates"]
    task_id = _as_int(data.get("task_id"))
    if task_id is None:
        task_id = _as_int(payload.get("task_id"))
    try:
        confidence = float(data.get("confidence") if data.get("confidence") is not None else 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    message = str(data.get("user_message") or "").strip()
    return Decision(action, task_id, confidence, bool(data.get("needs_confirmation")), payload, message)


def rank_tasks(query: str, tasks: list[Task]) -> list[tuple[float, Task]]:
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    ranked: list[tuple[float, Task]] = []
    for task in tasks:
        task_tokens = _tokens(task.title)
        if not task_tokens:
            continue
        score = len(query_tokens & task_tokens) / len(query_tokens)
        ranked.append((score, task))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked


def search_tasks(query: str, tasks: list[Task], limit: int = _CANDIDATE_LIMIT) -> list[Task]:
    by_id = {task.id: task for task in tasks}
    chosen: list[Task] = []
    seen: set[int] = set()
    for match in _TASK_ID.finditer(query or ""):
        number = int(next(group for group in match.groups() if group))
        task = by_id.get(number)
        if task is not None and task.id not in seen:
            chosen.append(task)
            seen.add(task.id)
    tokens = _tokens(query)
    scored: list[tuple[float, Task]] = []
    if tokens:
        for task in tasks:
            if task.id in seen:
                continue
            fields, body = parse_description(task.description)
            blob = " ".join([task.title, body, *fields.values()])
            task_tokens = _tokens(blob)
            if not task_tokens:
                continue
            score = len(tokens & task_tokens) / len(tokens)
            if score >= 0.2:
                scored.append((score, task))
    scored.sort(key=lambda item: item[0], reverse=True)
    for _score, task in scored:
        if len(chosen) >= limit:
            break
        chosen.append(task)
        seen.add(task.id)
    return chosen[:limit]


def _extract_json(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return data


def _as_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _tokens(text: str) -> set[str]:
    words = _WORD.findall((text or "").lower().replace("ё", "е"))
    return {word[:6] for word in words if word not in _STOP}


def _is_self(value: str) -> bool:
    return value.strip().lower().replace("ё", "е") in _SELF


def _default_kind(source: str) -> str:
    if source in {"telegram", "cli"}:
        return "ответ человека"
    return "предположение"


def _priority(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, min(5, number))


def _is_completion(decision: Decision) -> bool:
    if decision.payload.get("delete"):
        return True
    status = str(decision.payload.get("status") or "").strip()
    return decision.action == "change_status" and status == STATUS_DONE


def _downgrade_completion(decision: Decision, text: str) -> Decision:
    payload = dict(decision.payload)
    payload.pop("status", None)
    payload.pop("delete", None)
    payload.pop("due_date", None)
    payload.pop("no_deadline", None)
    payload["comment_kind"] = "предположение"
    note = str(payload.get("comment") or decision.user_message or text)
    payload["comment"] = note
    if decision.task_id is not None:
        return Decision(
            action="append",
            task_id=decision.task_id,
            confidence=max(decision.confidence, MIN_CONFIDENCE),
            needs_confirmation=False,
            payload=payload,
            user_message="",
        )
    return Decision(
        action="note",
        task_id=None,
        confidence=max(decision.confidence, MIN_CONFIDENCE),
        needs_confirmation=False,
        payload={"text": note},
        user_message="",
    )


def _candidate_question(decision: Decision, tasks: list[Task]) -> str:
    if decision.user_message:
        return decision.user_message
    raw = decision.payload.get("candidates") or []
    names: list[str] = []
    by_id = {task.id: task for task in tasks}
    if isinstance(raw, list):
        for item in raw:
            number = _as_int(item.get("id")) if isinstance(item, dict) else _as_int(item)
            if number is None:
                continue
            task = by_id.get(number)
            names.append(f"#{task.id} «{task.title}»" if task else f"#{number}")
    if not names and len(tasks) >= 2 and all(isinstance(task, Task) for task in tasks[:2]):
        names = [f"#{task.id} «{task.title}»" for task in tasks[:2]]
    if not names:
        return "Нашёл несколько похожих задач. Какую иметь в виду?"
    return "Нашёл несколько задач: " + " и ".join(names) + ". Какую дополнить?"


def _fire_at(payload: dict, now: datetime) -> datetime | None:
    if payload.get("in_minutes") is not None:
        try:
            minutes = float(payload["in_minutes"])
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        return now + timedelta(minutes=minutes)
    zone = now.tzinfo or resolve_timezone("Europe/Moscow")
    for key in ("fire_at", "at", "when"):
        if payload.get(key):
            parsed = parse_local_dt(str(payload[key]), zone)
            if parsed is not None:
                return parsed
            relative = relative_day(str(payload[key]), now.date())
            if relative is not None:
                return datetime.combine(relative, now.timetz())
    return None


def _missing_create(payload: dict) -> list[str]:
    missing: list[str] = []
    if not str(payload.get("assignee") or "").strip():
        missing.append("assignee")
    if not payload.get("no_deadline") and payload.get("due_date") in (None, "", "null"):
        missing.append("due")
    return missing


def _slot_for(missing: list[str]) -> str:
    if "assignee" in missing and "due" in missing:
        return "due_assignee"
    if "assignee" in missing:
        return "assignee"
    if "due" in missing:
        return "due"
    return "free"


def _missing_question(title: str, missing: list[str]) -> str:
    names: list[str] = []
    if "assignee" in missing:
        names.append("исполнителя")
    if "due" in missing:
        names.append("срок")
    return f"Не создаю задачу «{title}», пока не ясно: {' и '.join(names)}."


def _fill_slot(slot: str, text: str, today: object) -> dict:
    filled: dict = {}
    if slot == "title":
        filled["title"] = text.strip()
        return filled
    if slot in {"due", "due_assignee", "free", "assignee"}:
        kind, iso = classify_due(text, today)  # type: ignore[arg-type]
        if kind == "keep":
            pass
        elif kind in {"clear", "none"}:
            filled["no_deadline"] = True
            filled["due_date"] = None
        elif kind in {"set", "date"} and iso:
            filled["due_date"] = iso
    if slot in {"assignee", "due_assignee", "free"}:
        who = _assignee_from_text(text)
        if who:
            filled["assignee"] = who
    if slot in {"task_id", "free"}:
        found = _ids_in_text(text)
        if len(found) == 1:
            filled["task_id"] = found[0]
    if slot in {"confirm_task", "free"}:
        lowered = text.lower().replace("ё", "е")
        if re.search(r"\b(да|дополн)", lowered):
            filled["confirm_existing"] = True
        elif re.search(r"\b(нет|новую|созда)", lowered):
            filled["confirm_existing"] = False
    plan = plan_from_text(text)
    if plan:
        filled["plan"] = plan
        filled["planned_on"] = planned_date_for(plan, today)  # type: ignore[arg-type]
    return filled


def _assignee_from_text(text: str) -> str | None:
    lowered = (text or "").lower().replace("ё", "е")
    if re.search(r"\b(я|мне|меня)\b", lowered):
        return "я"
    match = re.search(r"исполнител\w*\s+([0-9A-Za-zА-Яа-яЁё_-]{2,40})", text or "", re.IGNORECASE)
    if match:
        return match.group(1)
    stripped = (text or "").strip()
    if re.fullmatch(r"[0-9A-Za-zА-Яа-яЁё_-]{2,40}", stripped) and stripped.casefold() not in {
        "завтра",
        "сегодня",
        "послезавтра",
        "позже",
    }:
        return stripped
    return None


def _ids_in_text(text: str) -> list[int]:
    found: list[int] = []
    for match in _TASK_ID.finditer(text or ""):
        raw = next(group for group in match.groups() if group)
        found.append(int(raw))
    return found


def _multiple_actions(text: str) -> bool:
    hits = 0
    for pattern in _CUES:
        if pattern.search(text or ""):
            hits += 1
    return hits >= 2


def _is_cancel(text: str) -> bool:
    return (text or "").strip().lower().replace("ё", "е") in {"отмена", "cancel", "стоп"}


def _is_instruction(text: str) -> bool:
    return bool(_INSTRUCTION.search(text or ""))


def _is_foreign_extra(extra: dict | None, event_key: str | None) -> bool:
    if not isinstance(extra, dict) or not event_key:
        return False
    owner = str(extra.get("deduplication_key") or "")
    return bool(owner) and owner != event_key


def _attachment_file(attachments: list[dict] | None) -> bool:
    for item in attachments or []:
        path = str(item.get("path") or "").strip()
        if path and Path(path).is_file():
            return True
    return False


def _confirmed_allowed(
    source: str,
    text: str,
    extra: dict | None,
    attachments: list[dict] | None,
    event_key: str | None,
) -> bool:
    scoped = None if _is_foreign_extra(extra, event_key) else extra
    if source in HARNESS_SOURCES or _is_foreign_extra(extra, event_key):
        return _existing_artifact(scoped) or _attachment_file(attachments)
    return _allow_confirmed(source, text, scoped)


def _guard_confirmed(
    payload: dict,
    source: str,
    text: str,
    extra: dict | None,
    attachments: list[dict] | None,
    event_key: str | None,
) -> dict:
    guarded = dict(payload)
    if guarded.get("confirmed_result") in (None, ""):
        return guarded
    if not _confirmed_allowed(source, text, extra, attachments, event_key):
        guarded.pop("confirmed_result", None)
    return guarded


def _human_corpus(dialog: dict, latest: str) -> str:
    parts = [str(dialog.get("original_text") or "")]
    answers = [str(item) for item in (dialog.get("answers") or [])]
    clean = (latest or "").strip()
    if clean and (not answers or answers[-1] != clean):
        answers.append(clean)
    parts.extend(answers)
    return "\n".join(part for part in parts if str(part).strip())


def _existing_artifact(extra: dict | None) -> bool:
    if not extra:
        return False
    for item in extra.get("artifacts") or []:
        path = str(item).strip()
        if not path or path.startswith("git status:"):
            continue
        if Path(path).is_file():
            return True
    return False


def _allow_confirmed(source: str, text: str, extra: dict | None) -> bool:
    if source in HARNESS_SOURCES:
        return _existing_artifact(extra)
    if source not in _HUMAN_SOURCES:
        return False
    return not _is_instruction(text)


def _evidence(
    action: str,
    source: str,
    message: str,
    attachments: list[dict] | None,
    extra: dict | None,
) -> tuple[str, str]:
    for item in attachments or []:
        path = str(item.get("path") or "")
        if path and Path(path).is_file():
            return "файл", path
    artifact = ""
    if extra:
        for item in extra.get("artifacts") or []:
            path = str(item).strip()
            if path and not path.startswith("git status:") and Path(path).is_file():
                artifact = path
                break
    if source in HARNESS_SOURCES:
        return "событие", artifact
    if action == "create" or _is_instruction(message):
        return "поручение", ""
    return "заявление", ""


def _message_id(delivered: object) -> int | None:
    if isinstance(delivered, int) and not isinstance(delivered, bool):
        return delivered
    if isinstance(delivered, dict) and delivered.get("message_id") is not None:
        try:
            return int(delivered["message_id"])
        except (TypeError, ValueError):
            return None
    return None


def _dialog_prompt(dialog: dict, latest: str) -> str:
    lines = [f"Исходное сообщение:\n{dialog.get('original_text') or ''}"]
    payload = dialog.get("payload") if isinstance(dialog.get("payload"), dict) else {}
    for key, label in (
        ("title", "Название"),
        ("expected_result", "Ожидаемый результат"),
        ("next_step", "Следующий шаг"),
    ):
        value = str(payload.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    if dialog.get("question"):
        lines.append(f"Вопрос:\n{dialog['question']}")
    answers = [str(item) for item in (dialog.get("answers") or [])]
    if latest and (not answers or answers[-1] != latest):
        answers.append(latest)
    for index, answer in enumerate(answers, start=1):
        lines.append(f"Ответ {index}:\n{answer}")
    lines.append("Продолжай ту же операцию. Не заменяй исходную постановку ответами.")
    return "\n".join(lines)


def _match_waiting(text: str, waiting: list[dict]) -> dict | None:
    stripped = (text or "").strip()
    if stripped.isdigit():
        index = int(stripped) - 1
        if 0 <= index < len(waiting):
            return waiting[index]
    lowered = stripped.casefold()
    hits: list[dict] = []
    for dialog in waiting:
        blob = f"{dialog.get('question') or ''}\n{dialog.get('original_text') or ''}".casefold()
        if lowered and lowered in blob:
            hits.append(dialog)
    if len(hits) == 1:
        return hits[0]
    return None


def _public_extra(extra: dict | None) -> dict:
    if not extra:
        return {}
    allowed = (
        "source",
        "session_id",
        "timestamp",
        "project",
        "task_hint",
        "facts",
        "artifacts",
        "next_step",
        "confidence",
        "deduplication_key",
    )
    return {key: extra.get(key) for key in allowed if key in extra}


def _vikunja_message(exc: VikunjaError) -> str:
    if exc.status == 401:
        return "Vikunja отклонила токен. Проверьте VIKUNJA_TOKEN в .env."
    if exc.status == 404:
        return "Проект FlowCI или задача не найдены в Vikunja."
    if exc.status == 409:
        return "Не удалось сохранить задачу, не затронув остальные поля."
    return "Vikunja сейчас недоступна. Повторите позже."
