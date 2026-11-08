---
name: task-sync
description: >-
  Records FlowCI task progress before a Cursor session ends. Writes
  .flowci/session.json with task_hint, facts, next_step, and artifacts so the
  stop hook can POST one task-sync event. Use at the end of a coding session,
  when work on a task pauses, or when the user mentions FlowCI, Vikunja,
  task-sync, or a session summary. A git commit is not task completion.
---

# task-sync

Перед концом сессии, если в ней был прогресс по задаче, запиши заметку для FlowCI. Хук `stop` сам отправит её на локальный ingest и при ошибке сети не блокирует агента.

Если сессия ничего не изменила в задачах, файл не создавай.

## Что записать

Создай или замени файл `.flowci/session.json` в корне репозитория:

```json
{
  "task_hint": "Короткое название существующей или новой задачи",
  "facts": ["Что реально произошло"],
  "next_step": "Следующее действие",
  "artifacts": ["путь или ссылка"],
  "project": "FlowCI",
  "confidence": 0.8,
  "session_id": "cursor-session"
}
```

- `task_hint` — по какой задаче шла работа.
- `facts` — только наблюдаемое: файлы, команды, тесты, результаты. Догадку помечай текстом `предположение:`.
- `next_step` — что делать дальше.
- `artifacts` — пути и ссылки, без содержимого секретов.
- `deduplication_key` можно не писать: хук построит устойчивый ключ из заметки. Повтор той же заметки не создаёт вторую задачу. Если факты изменились, ключ должен измениться вместе с ними.

## Чего не делать

- Не ставь статус «Завершено» и не проси удалить задачу. Коммит, diff и это резюме завершением не являются.
- Не записывай в файл значения из `.env`, токены, ключи и пароли.
- Не вызывай Vikunja напрямую и не запускай бота.
- Если ingest недоступен, хук завершится сам и не должен останавливать сессию. Повторять запрос вручную не нужно.
