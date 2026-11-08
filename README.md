# FlowCI Task Agent

Локальный агент задач: Telegram, Vikunja и RouterAI в одном процессе Python 3.11+. Vikunja остаётся источником правды по задачам. Свой журнал — `data/flowci.db`.

## Что нужно заранее

- Python 3.11 или новее. На этой машине подходит установленный `python` (3.12 предпочтителен, если он есть).
- Vikunja на `http://127.0.0.1:3456`, проект с названием ровно `FlowCI`.
- Метки статуса: `Нужно сделать`, `В работе`, `Заблокировано`, `Завершено`.
- Метки плана: `Сегодня`, `Завтра`, `Неделя`, `Позже`.
- Файл `.env` в корне репозитория. Образец имён — `.env.example`. Значения секретов в репозиторий не класть и не печатать.

Срок задачи — это `due_date`. Метка плана его не заменяет. Показ плана срок не меняет. «Завершено» и удаление возможны только после кнопки в Telegram или флага `--confirm` в CLI. Коммит и резюме сессии задачу не закрывают.

## Переменные

Обязательны для живого запуска:

- `ROUTERAI_API_KEY`
- `TG_BOT_TOKEN`
- `VIKUNJA_TOKEN`
- `FLOWCI_INGEST_TOKEN`

Остальные имеют значения по умолчанию, их можно переопределить:

- `ROUTERAI_BASE_URL` — `https://routerai.ru/api/v1`
- `ROUTERAI_CHAT_MODEL` — `openai/gpt-6.1-sol`
- `ROUTERAI_STT_MODEL` — `openai/whisper-1`
- `TG_ALLOWED_USER_IDS` — пусто, тогда первый `/start` записывает владельца в `data/flowci.db`
- `VIKUNJA_URL` — `http://127.0.0.1:3456`
- `VIKUNJA_PROJECT_ID` — пусто, тогда ищется проект `FlowCI`
- `FLOWCI_INGEST_HOST` — в конфигурации есть, сервер всё равно слушает только `127.0.0.1`
- `FLOWCI_INGEST_PORT` — `8787`
- `FLOWCI_TIMEZONE` — `Europe/Moscow`
- `FLOWCI_MORNING_HOUR` — `9`
- `FLOWCI_DATA_DIR` — `data`

## Установка

Из корня репозитория:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:PYTHONPATH = "src"
```

## Запуск

Бот (long polling), приём событий и планировщик:

```powershell
$env:PYTHONPATH = "src"
python -m flowci
```

Тот же агент из консоли:

```powershell
$env:PYTHONPATH = "src"
python -m flowci.cli "что мне сегодня делать"
python -m flowci.cli "закрой задачу 12" --confirm
```

Первый `/start` от владельца включает бота, если `TG_ALLOWED_USER_IDS` пуст. Чужие чаты игнорируются. Голосовое сообщение сохраняется в `data/artifacts/` и расшифровывается моделью `ROUTERAI_STT_MODEL` (язык `ru`). Текст расшифровки и путь к файлу попадают в комментарий задачи.

Утреннее сообщение одно, в час `FLOWCI_MORNING_HOUR` по `FLOWCI_TIMEZONE`. Другие напоминания — только если их попросили.

## Ingest

`POST http://127.0.0.1:8787/v1/events`

Заголовок: `Authorization: Bearer <FLOWCI_INGEST_TOKEN>`

Поля тела: `source`, `session_id`, `timestamp`, `project`, `task_hint`, `facts`, `artifacts`, `next_step`, `confidence`, `deduplication_key`.

Повтор с тем же `deduplication_key` вторую задачу не создаёт. Проверка без секретов: `GET http://127.0.0.1:8787/health`.

## Cursor

Навык `.cursor/skills/task-sync/SKILL.md` просит записать `.flowci/session.json` перед концом сессии. Хук `stop` в `.cursor/hooks.json` читает заметку и `git status`, отправляет событие на ingest и при любой ошибке отпускает агента. Для хука `python` должен быть в `PATH`.

## Тесты

Юнит-тесты с подставными Vikunja и моделью:

```powershell
pytest
```

Живой Vikunja, Telegram и RouterAI этими тестами не вызываются.

## Дальнейшая доработка

BookStack обязателен в ближайшей целевой версии. Интеграция BookStack, переход разговорной оркестрации на DeepSeek Harness и согласованный Kanban описаны в [ТЗ](features/feature_flowci_agent_platform.md). Это план доработки: в текущем коде BookStack и DeepSeek Harness ещё не подключены.

Redmine, обязательный Docker, несколько IDE, Symphony, сложная ролевая модель и отдельный админ-интерфейс остаются за рамками этой доработки.

## Состав публичного репозитория

Репозиторий содержит исходники, тесты, документацию и тексты ревью. Файл `.env`, локальные базы, служебные журналы и сырые архивы доказательств ревью (`reviews/evidence/`, `reviews/*-evidence/`) остаются локально и не публикуются. Ссылки из ревью на такие архивы доступны только в исходном рабочем каталоге. Для настройки используйте `.env.example` со своими значениями.
