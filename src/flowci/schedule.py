"""Напоминания по просьбе и одна утренняя сводка."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from flowci.agent import TaskAgent
from flowci.config import Settings, resolve_timezone
from flowci.formatting import to_utc_iso
from flowci.store import Store

log = logging.getLogger("flowci.schedule")
SendFunc = Callable[[str], Awaitable[object]]


class ReminderScheduler:
    def __init__(self, settings: Settings, store: Store, agent: TaskAgent, send: SendFunc) -> None:
        self._settings = settings
        self._store = store
        self._agent = agent
        self._send = send
        self._zone = resolve_timezone(settings.flowci_timezone)
        self._scheduler = AsyncIOScheduler(timezone=self._zone)

    def start(self) -> None:
        self._scheduler.add_job(
            self._morning,
            CronTrigger(hour=self._settings.flowci_morning_hour, minute=0, timezone=self._zone),
            id="flowci-morning",
            replace_existing=True,
        )
        self._scheduler.add_job(
            self._dispatch_reminders,
            IntervalTrigger(seconds=30),
            id="flowci-reminders",
            replace_existing=True,
        )
        self._scheduler.start()
        log.info("scheduler morning_hour=%s", self._settings.flowci_morning_hour)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    async def _morning(self) -> None:
        try:
            text = await self._agent.morning_digest()
            await self._send(text)
        except Exception:
            log.warning("morning digest failed")

    async def _dispatch_reminders(self) -> None:
        await self._resume_stale()
        await self._dispatch_outbox()
        now = to_utc_iso(self._agent._now())
        for reminder_id, text, _attempts in self._store.due_reminders(now):
            outcome, _message_id = await self._outcome(text)
            if outcome == "sent":
                self._store.mark_reminder_sent(reminder_id)
                continue
            if outcome == "skip":
                continue
            attempts = self._store.bump_reminder_attempt(reminder_id)
            if attempts >= 3:
                self._store.mark_reminder_failed(reminder_id)

    async def _resume_stale(self) -> None:
        for item in self._store.stale_events():
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else None
            try:
                await self._agent.handle(
                    str(payload.get("text") or item.get("user_message") or ""),
                    source=str(payload.get("source") or "cursor"),
                    deduplication_key=str(item.get("key") or ""),
                    allow_completion=bool(payload.get("allow_completion")),
                    extra=extra,
                    confirm=False,
                )
            except Exception:
                log.warning("stale event resume failed")

    async def _dispatch_outbox(self) -> None:
        for item in self._store.pending_outbox():
            if not self._store.claim_outbox(int(item["id"])):
                continue
            outcome, message_id = await self._outcome(str(item["text"]))
            if outcome == "sent":
                self._store.ack_outbox(int(item["id"]))
                if message_id is not None and item.get("event_key"):
                    self._store.bind_question_message(str(item["event_key"]), message_id)
            elif outcome == "error":
                self._store.release_outbox(int(item["id"]), bump=True)
            else:
                self._store.release_outbox(int(item["id"]))

    async def _outcome(self, text: str) -> tuple[str, int | None]:
        try:
            delivered = await self._send(text)
        except Exception:
            log.warning("delivery failed")
            return "error", None
        if delivered is None:
            return "skip", None
        message_id = _message_ref(delivered)
        if delivered is True or message_id is not None:
            return "sent", message_id
        return "error", None


def _message_ref(delivered: object) -> int | None:
    if isinstance(delivered, int) and not isinstance(delivered, bool):
        return delivered
    return None
