"""Telegram long polling: текст, голос, файл и подтверждение."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from flowci.agent import AgentResult, TaskAgent
from flowci.config import Settings
from flowci.store import Store

log = logging.getLogger("flowci.telegram")
_SAFE = re.compile(r"[^0-9A-Za-zА-Яа-я._-]+")


def build_bot(settings: Settings) -> Bot:
    return Bot(token=settings.tg_bot_token)


_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json"}
_TEXT_LIMIT = 64 * 1024


def build_dispatcher(settings: Settings, store: Store, agent: TaskAgent) -> Dispatcher:
    dispatcher = Dispatcher()

    def allowed(user_id: int | None, chat: object | None) -> bool:
        if user_id is None or chat is None:
            return False
        if getattr(chat, "type", None) != "private":
            return False
        if not store.is_owner(user_id, settings.tg_allowed_user_ids):
            return False
        bound = store.chat_for_user(user_id)
        return bound is not None and int(bound) == int(getattr(chat, "id"))

    @dispatcher.message(CommandStart())
    async def on_start(message: Message) -> None:
        user = message.from_user
        chat = message.chat
        if user is None or chat is None or getattr(chat, "type", None) != "private":
            return
        if not store.bind_owner(user.id, settings.tg_allowed_user_ids):
            return
        store.bind_owner_chat(int(chat.id), user.id)
        await message.answer("FlowCI на связи. Пишите текстом, голосом или файлом.")

    @dispatcher.callback_query(F.data.startswith("confirm:"))
    async def on_confirm(query: CallbackQuery) -> None:
        user = query.from_user
        message = query.message
        if user is None or message is None or not allowed(user.id, message.chat):
            await query.answer()
            return
        token = (query.data or "").split(":", 1)[1]
        try:
            result = await agent.confirm(token, user_id=user.id)
        except Exception:
            log.warning("confirm handling failed")
            await message.answer("Не получилось обработать сообщение.")
            await query.answer()
            return
        await message.answer(_clip(result.user_message))
        await query.answer("Готово" if result.ok else "Ошибка")

    @dispatcher.callback_query(F.data.startswith("cancel:"))
    async def on_cancel(query: CallbackQuery) -> None:
        user = query.from_user
        message = query.message
        if user is None or message is None or not allowed(user.id, message.chat):
            await query.answer()
            return
        token = (query.data or "").split(":", 1)[1]
        try:
            result = await agent.cancel(token, user_id=user.id)
        except Exception:
            log.warning("cancel handling failed")
            await message.answer("Не получилось обработать сообщение.")
            await query.answer()
            return
        await message.answer(_clip(result.user_message))
        await query.answer()

    @dispatcher.message(F.voice)
    async def on_voice(message: Message) -> None:
        user = message.from_user
        if user is None or not allowed(user.id, message.chat) or message.voice is None:
            return
        try:
            target = await _download(message, settings, message.voice.file_id, message.voice.file_unique_id + ".ogg")
            transcript = await agent.transcribe(target)
        except Exception:
            log.warning("voice handling failed")
            await message.answer("Не удалось распознать голос.")
            return
        text = f"Голосовое сообщение.\nФайл: {target}\nТранскрипт: {transcript}"
        try:
            result = await agent.handle(
                text,
                source="telegram",
                attachments=[{"path": str(target), "transcript": transcript, "kind": "voice"}],
                chat_id=message.chat.id,
                user_id=user.id,
            )
        except Exception:
            log.warning("voice handling failed")
            await message.answer("Не получилось обработать сообщение.")
            return
        await _reply(message, result)

    @dispatcher.message(F.document)
    async def on_document(message: Message) -> None:
        user = message.from_user
        if user is None or not allowed(user.id, message.chat) or message.document is None:
            return
        original = message.document.file_name or "file"
        safe = _SAFE.sub("_", original)[:80] or "file"
        filename = f"{message.document.file_unique_id}_{safe}"
        try:
            target = await _download(message, settings, message.document.file_id, filename)
        except Exception:
            log.warning("document download failed")
            await message.answer("Не удалось сохранить файл.")
            return
        caption = message.caption or original
        extracted, notice = _read_text_attachment(target)
        content = f"\nСодержимое:\n{extracted}" if extracted is not None else ""
        text = f"Файл от пользователя.\nИмя: {original}\nФайл: {target}\nКомментарий: {caption}{content}"
        try:
            result = await agent.handle(
                text,
                source="telegram",
                attachments=[{"path": str(target), "transcript": extracted or "", "kind": "document"}],
                chat_id=message.chat.id,
                user_id=user.id,
            )
        except Exception:
            log.warning("document handling failed")
            await message.answer("Не получилось обработать сообщение.")
            return
        if notice:
            result = AgentResult(
                action=result.action,
                user_message=(result.user_message + "\n" + notice).strip(),
                task_id=result.task_id,
                needs_confirmation=result.needs_confirmation,
                confirmation_token=result.confirmation_token,
                noop=result.noop,
                created_task=result.created_task,
                ok=result.ok,
            )
        await _reply(message, result)

    @dispatcher.message(F.text)
    async def on_text(message: Message) -> None:
        user = message.from_user
        if user is None or not allowed(user.id, message.chat):
            return
        text = (message.text or "").strip()
        if not text:
            return
        reply = message.reply_to_message
        reply_to = int(reply.message_id) if reply is not None and reply.message_id is not None else None
        try:
            result = await agent.handle(
                text,
                source="telegram",
                chat_id=message.chat.id,
                user_id=user.id,
                reply_to=reply_to,
            )
        except Exception:
            log.warning("text handling failed")
            await message.answer("Не получилось обработать сообщение.")
            return
        await _reply(message, result)

    return dispatcher


async def _download(message: Message, settings: Settings, file_id: str, filename: str) -> Path:
    directory = settings.artifacts_dir
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    bot = message.bot
    remote = await bot.get_file(file_id)
    await bot.download_file(remote.file_path, destination=target)
    return target


async def _reply(message: Message, result: AgentResult) -> None:
    text = _clip(result.user_message)
    if result.needs_confirmation and result.confirmation_token:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Подтвердить",
                        callback_data=f"confirm:{result.confirmation_token}",
                    ),
                    InlineKeyboardButton(
                        text="Отмена",
                        callback_data=f"cancel:{result.confirmation_token}",
                    ),
                ]
            ]
        )
        await message.answer(text, reply_markup=keyboard)
        return
    await message.answer(text)


def _read_text_attachment(path: Path) -> tuple[str | None, str | None]:
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return None, "Содержимое файла не прочитано. Поддерживаются txt, md, csv и json до 64 КБ."
    try:
        size = path.stat().st_size
    except OSError:
        return None, "Содержимое файла не прочитано."
    if size > _TEXT_LIMIT:
        return None, "Содержимое файла не прочитано: размер больше 64 КБ."
    try:
        return path.read_text(encoding="utf-8"), None
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return None, "Содержимое файла не прочитано."


def _clip(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
