"""Один процесс: бот, ingest и планировщик."""

from __future__ import annotations

import asyncio
import logging

from flowci.agent import TaskAgent
from flowci.config import load_settings
from flowci.ingest import start_ingest
from flowci.llm import RouterAI
from flowci.schedule import ReminderScheduler
from flowci.store import Store
from flowci.telegram import build_bot, build_dispatcher
from flowci.vikunja import VikunjaClient


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_async_main())
    except KeyboardInterrupt:
        return


async def _async_main() -> None:
    settings = load_settings()
    log = logging.getLogger("flowci")
    log.info("starting %s", settings.public_dict())
    if not settings.tg_bot_token:
        log.error("TG_BOT_TOKEN is not set")
        raise SystemExit(1)
    store = Store(settings.db_path)
    vikunja = VikunjaClient(settings)
    llm = RouterAI(settings)
    agent = TaskAgent(settings, store, vikunja, llm)
    bot = build_bot(settings)
    dispatcher = build_dispatcher(settings, store, agent)

    async def send(text: str) -> int | bool | None:
        chats = store.delivery_chats(settings.tg_allowed_user_ids)
        if not chats:
            log.info("delivery skipped: no owner")
            return None
        message_id: int | None = None
        for chat_id in chats:
            message = await bot.send_message(chat_id, text[:4000])
            message_id = int(message.message_id)
        if len(chats) == 1 and message_id is not None:
            return message_id
        return True

    agent.outbox_sender = send
    scheduler = ReminderScheduler(settings, store, agent, send)
    runner = None
    scheduler.start()
    try:
        runner = await start_ingest(agent, settings)
        await dispatcher.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        scheduler.shutdown()
        if runner is not None:
            await runner.cleanup()
        await vikunja.aclose()
        await llm.aclose()
        await bot.session.close()
        store.close()


if __name__ == "__main__":
    main()
