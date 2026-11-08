"""Шапка описания, комментарии, план и недельный отчёт."""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

STATUS_TODO = "Нужно сделать"
STATUS_DOING = "В работе"
STATUS_BLOCKED = "Заблокировано"
STATUS_DONE = "Завершено"
STATUS_LABELS = (STATUS_TODO, STATUS_DOING, STATUS_BLOCKED, STATUS_DONE)
PLAN_LABELS = ("Сегодня", "Завтра", "Неделя", "Позже")
HEADER_KEYS = (
    "Ожидаемый результат",
    "Следующий шаг",
    "Последний подтверждённый результат",
    "Когда планируется",
)
COMMENT_KINDS = ("факт", "предположение", "ответ человека")
HARNESS_SOURCES = frozenset({"cursor", "task-sync", "ingest", "git", "hook"})
MIN_CONFIDENCE = 0.67

_HEADER_PAYLOAD = {
    "expected_result": "Ожидаемый результат",
    "next_step": "Следующий шаг",
    "confirmed_result": "Последний подтверждённый результат",
    "planned_on": "Когда планируется",
}
_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_MONTHS = (
    (re.compile(r"январ[ья]", re.IGNORECASE), 1),
    (re.compile(r"феврал[ья]", re.IGNORECASE), 2),
    (re.compile(r"марта|март\b", re.IGNORECASE), 3),
    (re.compile(r"апрел[ья]", re.IGNORECASE), 4),
    (re.compile(r"мая|май\b", re.IGNORECASE), 5),
    (re.compile(r"июн[ья]", re.IGNORECASE), 6),
    (re.compile(r"июл[ья]", re.IGNORECASE), 7),
    (re.compile(r"августа|август\b", re.IGNORECASE), 8),
    (re.compile(r"сентябр[ья]", re.IGNORECASE), 9),
    (re.compile(r"октябр[ья]", re.IGNORECASE), 10),
    (re.compile(r"ноябр[ья]", re.IGNORECASE), 11),
    (re.compile(r"декабр[ья]", re.IGNORECASE), 12),
)
_NO_DEADLINE = re.compile(
    r"срок(?:а|ов|и)?\s+нет|без\s+срока|бессроч|дедлайн\s+не\s+(?:нужен|надо|ставить)|не\s+ставить\s+дедлайн",
    re.IGNORECASE,
)
_KEEP_DUE = re.compile(
    r"не\s+меня(?:й|ть|йте)\s+(?:срок|дедлайн)|"
    r"(?:срок|дедлайн)\s+не\s+меня(?:й|ть|йте)|"
    r"не\s+трога(?:й|ть)\s+(?:срок|дедлайн)|"
    r"(?:срок|дедлайн)\s+оставь|оставь\s+(?:срок|дедлайн)|"
    r"не\s+перенос\w*\s+(?:срок|дедлайн)",
    re.IGNORECASE,
)
_INLINE_TAGS = r"(?:\s*</?(?:strong|b|em|i|u|span|code|mark|small|sup|sub)\b[^>]*>)*"
_HTML_BLOCK = re.compile(r"(?is)<\s*(p|div|li|h[1-6]|blockquote)\b[^>]*>.*?</\s*\1\s*>")
_ANSWER_LINE = re.compile(r"^Ответ\s+\d+\s*:\s*(.*)$", re.IGNORECASE)
_PAYLOAD_LINE = re.compile(r"^(?:Название|Ожидаемый результат|Следующий шаг)\s*:", re.IGNORECASE)
_PLAN_QUESTION = re.compile(
    r"что\s+(?:мне\s+)?(?:сегодня|завтра|послезавтра)\b|план\s+на\s+(?:сегодня|завтра|неделю|позже)",
    re.IGNORECASE,
)
_HORIZONS = {
    "today": ("Сегодня",),
    "tomorrow": ("Завтра",),
    "week": ("Сегодня", "Завтра", "Неделя"),
    "later": ("Позже",),
    "all": PLAN_LABELS,
}
_HORIZON_TITLE = {
    "today": "сегодня",
    "tomorrow": "завтра",
    "week": "неделю",
    "later": "позже",
    "all": "все горизонты",
}
_HORIZON_ALIASES = {
    "today": "today",
    "сегодня": "today",
    "tomorrow": "tomorrow",
    "завтра": "tomorrow",
    "week": "week",
    "неделя": "week",
    "weekly": "week",
    "later": "later",
    "позже": "later",
    "all": "all",
}
_TAG = re.compile(r"^\[(факт|предположение|ответ человека)\]\s*(.*)$", re.IGNORECASE)
_SOURCE = re.compile(r"источник:\s*(.*)$", re.IGNORECASE)


def contains_html(text: str | None) -> bool:
    return bool(text and _HTML_TAG.search(text))


def strip_tags(text: str | None) -> str:
    """Текст описания без HTML-тегов. Модель читает его, карточка хранит разметку."""
    raw = text or ""
    if not raw:
        return ""
    plain = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", raw)
    plain = re.sub(r"(?i)</\s*(p|div|li|tr|h[1-6])\s*>", "\n", plain)
    plain = re.sub(r"<[^>]+>", "", plain)
    return html.unescape(plain).strip()


def has_plain_header(text: str | None) -> bool:
    if not text:
        return False
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        if raw.startswith("<"):
            return False
        return any(raw.startswith(key + ":") for key in HEADER_KEYS)
    return False


def parse_description(text: str | None) -> tuple[dict[str, str], str]:
    fields = {key: "" for key in HEADER_KEYS}
    if not text:
        return fields, ""
    source = text
    if contains_html(text) and not has_plain_header(text):
        source = strip_tags(text)
    lines = source.splitlines()
    index = 0
    seen = 0
    while index < len(lines) and seen < len(HEADER_KEYS):
        raw = lines[index].strip()
        if raw == "" and seen == 0:
            index += 1
            continue
        matched = False
        for key in HEADER_KEYS:
            prefix = key + ":"
            if raw.startswith(prefix):
                fields[key] = raw[len(prefix) :].strip()
                matched = True
                seen += 1
                break
        if not matched:
            break
        index += 1
    while index < len(lines) and lines[index].strip() == "":
        index += 1
    body = "\n".join(lines[index:]).strip()
    return fields, body


def render_description(fields: dict[str, str], body: str) -> str:
    lines = [f"{key}: {(fields.get(key) or '').strip() or '—'}" for key in HEADER_KEYS]
    free = (body or "").strip()
    if free:
        return "\n".join(lines) + "\n\n" + free
    return "\n".join(lines)


def _filled_header(fields: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for key in HEADER_KEYS:
        value = (fields.get(key) or "").strip()
        if value and value != "—":
            lines.append(f"{key}: {value}")
    return lines


def _replace_labeled_html(source: str, label: str, value: str) -> str:
    """Заменяет значение метки, даже если подпись обёрнута inline-тегом."""
    pattern = re.compile(
        rf"({re.escape(label)}\s*:{_INLINE_TAGS}\s*)([^<]*)",
        re.IGNORECASE,
    )
    if pattern.search(source):
        return pattern.sub(lambda match: match.group(1) + value, source, count=1)
    snippet = f"<p>{label}: {value}</p>"
    return snippet + "\n" + source if source else snippet


def _is_header_html(fragment: str) -> bool:
    plain = strip_tags(fragment).strip()
    if not plain:
        return False
    first = plain.splitlines()[0].strip()
    return any(first.startswith(key + ":") for key in HEADER_KEYS)


def _replace_html_body(source: str, body: str) -> str:
    """Подменяет свободный текст, оставляя HTML шапки."""
    matches = list(_HTML_BLOCK.finditer(source))
    kept: list[str] = []
    if matches:
        cursor = 0
        for match in matches:
            between = source[cursor : match.start()]
            if between.strip() and _is_header_html(between):
                kept.append(between.strip())
            if _is_header_html(match.group(0)):
                kept.append(match.group(0))
            cursor = match.end()
        tail = source[cursor:]
        if tail.strip() and _is_header_html(tail):
            kept.append(tail.strip())
    else:
        for line in source.splitlines():
            if _is_header_html(line):
                kept.append(line)
        if not kept and _is_header_html(source):
            kept.append(source.strip())
    base = "\n".join(kept).strip()
    new_body = (body or "").strip()
    if not new_body:
        return base
    piece = new_body if contains_html(new_body) else f"<p>{new_body}</p>"
    if not base:
        return (source.rstrip() + "\n" + piece).strip()
    return (base + "\n" + piece).strip()


def _apply_html_fields(
    description: str,
    payload: dict,
    body_append: str | None,
    body: str | None = None,
) -> str:
    updated = description
    for key, label in _HEADER_PAYLOAD.items():
        if key in payload and payload[key] is not None:
            updated = _replace_labeled_html(updated, label, str(payload[key]).strip())
    if body is not None:
        updated = _replace_html_body(updated, body)
    extra = (body_append or "").strip()
    if extra and extra not in strip_tags(updated):
        updated = updated.rstrip() + f"\n<p>{extra}</p>"
    return updated.strip()


def apply_header(
    description: str,
    payload: dict,
    *,
    body: str | None = None,
    body_append: str | None = None,
) -> str:
    if contains_html(description) and not has_plain_header(description):
        return _apply_html_fields(description, payload, body_append, body)
    fields, existing = parse_description(description)
    for key, label in _HEADER_PAYLOAD.items():
        if key in payload and payload[key] is not None:
            fields[label] = str(payload[key]).strip()
    if body is not None:
        existing = body
    extra = (body_append or "").strip()
    if extra:
        existing = (existing + "\n\n" + extra).strip() if existing else extra
    rich = contains_html(description) or contains_html(existing)
    if rich:
        lines = _filled_header(fields)
        preserved = existing.strip()
        if not preserved and description and not has_plain_header(description):
            preserved = description.strip()
        if not lines:
            return (description or preserved).strip()
        if preserved:
            return "\n".join(lines) + "\n\n" + preserved
        return "\n".join(lines)
    return render_description(fields, existing)


def with_group(labels: list[str], group: tuple[str, ...], selected: str | None) -> list[str]:
    blocked = set(group)
    kept = [label for label in labels if label not in blocked]
    if selected:
        kept.append(selected)
    return kept


def build_comment(kind: str, source: str, text: str) -> str:
    clean = kind if kind in COMMENT_KINDS else "предположение"
    body = (text or "").strip() or "—"
    origin = (source or "unknown").strip()
    return f"[{clean}] источник: {origin}\n{body}"


def parse_comment(text: str) -> tuple[str, str, str]:
    lines = (text or "").splitlines()
    if not lines:
        return "неизвестно", "", ""
    match = _TAG.match(lines[0].strip())
    if not match:
        return "неизвестно", "", text.strip()
    kind = match.group(1).lower()
    tail = match.group(2).strip()
    source_match = _SOURCE.match(tail)
    if source_match:
        source = source_match.group(1).strip()
    else:
        source = tail
    body = "\n".join(lines[1:]).strip()
    return kind, source, body


def parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if len(text) == 10:
        text = text + "T00:00:00+00:00"
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def parse_local_dt(value: str | None, zone: timezone | object) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0001-01-01"):
        return None
    if len(text) == 10:
        text = text + "T00:00:00"
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)  # type: ignore[arg-type]
    return parsed


def relative_day(text: str, today: date) -> date | None:
    lowered = (text or "").lower().replace("ё", "е")
    if "послезавтра" in lowered:
        return today + timedelta(days=2)
    if re.search(r"\bзавтра\b", lowered):
        return today + timedelta(days=1)
    if re.search(r"\bсегодня\b", lowered):
        return today
    return None


def russian_date(text: str, today: date) -> str | None:
    for pattern, month in _MONTHS:
        match = re.search(rf"(\d{{1,2}})\s+(?:{pattern.pattern})", text, re.IGNORECASE)
        if not match:
            continue
        day = int(match.group(1))
        year_match = re.search(rf"(?:{pattern.pattern})\s+(\d{{4}})", text, re.IGNORECASE)
        year = int(year_match.group(1)) if year_match else today.year
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if year_match is None and found < today:
            try:
                found = date(year + 1, month, day)
            except ValueError:
                return None
        return found.isoformat()
    return None


def _due_source(text: str) -> str:
    """Дата внутри текста вложения не становится сроком. Подпись пользователя остаётся."""
    source = text or ""
    for marker in ("\nСодержимое:", "\nСодержимое файла:"):
        position = source.find(marker)
        if position >= 0:
            source = source[:position]
    return source


def human_due_text(text: str) -> str:
    """Исходное сообщение и ответы человека. Текст вопроса бота в разрешение срока не входит."""
    source = text or ""
    if "Исходное сообщение:" not in source:
        return source
    parts: list[str] = []
    mode = ""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("Исходное сообщение:"):
            mode = "human"
            rest = stripped.split(":", 1)[1].strip()
            if rest:
                parts.append(rest)
            continue
        answer = _ANSWER_LINE.match(stripped)
        if answer:
            mode = "human"
            rest = answer.group(1).strip()
            if rest:
                parts.append(rest)
            continue
        if stripped.startswith("Вопрос:"):
            mode = "question"
            continue
        if stripped.startswith("Продолжай ту же операцию") or _PAYLOAD_LINE.match(stripped):
            mode = "skip"
            continue
        if mode == "human":
            parts.append(line)
    return "\n".join(parts)


def _date_at_start(fragment: str, today: date) -> str | None:
    """Дата стоит сразу после предлога, а не в соседнем предложении."""
    window = (fragment or "").lstrip()
    if not window:
        return None
    iso = re.match(r"(\d{4}-\d{2}-\d{2})", window)
    if iso:
        try:
            date.fromisoformat(iso.group(1))
        except ValueError:
            return None
        return iso.group(1)
    dotted = re.match(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?(?![\d.])", window)
    if dotted:
        day = int(dotted.group(1))
        month = int(dotted.group(2))
        year = int(dotted.group(3)) if dotted.group(3) else today.year
        try:
            found = date(year, month, day)
        except ValueError:
            return None
        if dotted.group(3) is None and found < today:
            try:
                found = date(year + 1, month, day)
            except ValueError:
                return None
        return found.isoformat()
    lowered = window.lower().replace("ё", "е")
    if lowered.startswith("послезавтра"):
        return (today + timedelta(days=2)).isoformat()
    if re.match(r"завтра\b", lowered):
        return (today + timedelta(days=1)).isoformat()
    if re.match(r"сегодня\b", lowered):
        return today.isoformat()
    for pattern, _month in _MONTHS:
        if re.match(rf"(\d{{1,2}})\s+(?:{pattern.pattern})", window, re.IGNORECASE):
            return russian_date(window, today)
    return None


def _set_targets(source: str, today: date) -> list[tuple[int, str]]:
    """Одна целевая дата: после «на», «дедлайн» или «до/к». В «с A на B» цель — B."""
    found: list[tuple[int, str]] = []
    for match in re.finditer(r"перенес\w*", source, re.IGNORECASE):
        tail = source[match.end() :]
        onto = re.search(r"(?i)(?:^|\s)на\s+", tail)
        if not onto:
            continue
        iso = _date_at_start(tail[onto.end() :], today)
        if iso:
            found.append((onto.start() + match.end(), iso))
    for match in re.finditer(r"дедлайн\w*", source, re.IGNORECASE):
        iso = _date_at_start(source[match.end() :], today)
        if iso:
            found.append((match.start(), iso))
    for match in re.finditer(r"срок\w*", source, re.IGNORECASE):
        tail = source[match.end() :]
        prep = re.match(r"(?i)\s+(?:до|к)\s+", tail)
        if not prep:
            continue
        iso = _date_at_start(tail[prep.end() :], today)
        if iso:
            found.append((match.start(), iso))
    for match in re.finditer(r"(?i)(?<![\w])(?:до|к)\s+", source):
        iso = _date_at_start(source[match.end() :], today)
        if iso:
            found.append((match.end(), iso))
    return found


def classify_due(text: str, today: date) -> tuple[str, str | None]:
    """keep | set | clear | absent. Разрешение считается только по репликам человека."""
    source = _due_source(human_due_text(text or ""))
    signals: list[tuple[int, str, str | None]] = []
    clear = _NO_DEADLINE.search(source)
    if clear:
        signals.append((clear.start(), "clear", None))
    keep = _KEEP_DUE.search(source)
    if keep:
        signals.append((keep.start(), "keep", None))
    for position, iso in _set_targets(source, today):
        signals.append((position, "set", iso))
    if not signals:
        return "absent", None
    signals.sort(key=lambda item: item[0])
    _position, kind, iso = signals[-1]
    if kind == "set":
        return ("set", iso) if iso else ("absent", None)
    return kind, None


def plan_from_text(text: str) -> str | None:
    lowered = (text or "").lower().replace("ё", "е")
    if "план" not in lowered:
        return None
    if "позже" in lowered:
        return "Позже"
    if "недел" in lowered:
        return "Неделя"
    if "послезавтра" in lowered:
        return None
    if re.search(r"завтра", lowered):
        return "Завтра"
    if "сегодня" in lowered:
        return "Сегодня"
    return None


def normalize_horizon(value: str | None) -> str:
    raw = (value or "today").strip().lower()
    return _HORIZON_ALIASES.get(raw, "today")


def planned_date_for(plan: str, today: date) -> str | None:
    if plan == "Сегодня":
        return today.isoformat()
    if plan == "Завтра":
        return (today + timedelta(days=1)).isoformat()
    if plan == "Неделя":
        return (today + timedelta(days=3)).isoformat()
    if plan == "Позже":
        return (today + timedelta(days=14)).isoformat()
    return None


def _calendar_date(value: str | None) -> date | None:
    parsed = parse_dt(value)
    if parsed is None:
        return None
    return parsed.date()


def planned_moment(task: object) -> date | None:
    description = getattr(task, "description", "") or ""
    fields, _body = parse_description(description)
    planned = (fields.get("Когда планируется") or "").strip()
    if not planned or planned == "—":
        return None
    return _calendar_date(planned)


def legacy_plan_labels(task: object) -> list[str]:
    if planned_moment(task) is not None:
        return []
    return [label for label in getattr(task, "labels", []) or [] if label in PLAN_LABELS]


def task_dates(task: object) -> list:
    """Дата плана. Дедлайн сам по себе метку плана не заменяет."""
    found = []
    planned = planned_moment(task)
    if planned is not None:
        found.append(planned)
        return found
    if legacy_plan_labels(task):
        return found
    due = _calendar_date(getattr(task, "due_date", None))
    if due is not None:
        found.append(due)
    return found


def date_in_horizon(moments: list, horizon: str, today: date) -> bool:
    if horizon == "today":
        return today in moments
    if horizon == "tomorrow":
        return (today + timedelta(days=1)) in moments
    if horizon == "week":
        end = today + timedelta(days=6)
        return any(today <= item <= end for item in moments)
    if horizon == "later":
        end = today + timedelta(days=6)
        return any(item > end for item in moments)
    return True


def personal_task(task: object, owner: str | None) -> bool:
    names = [str(item).casefold() for item in (getattr(task, "assignees", None) or []) if str(item).strip()]
    if not names:
        return True
    if not owner:
        return False
    return owner.casefold() in names


def _due_sort_key(task: object) -> tuple:
    due = _calendar_date(getattr(task, "due_date", None))
    if due is None:
        return (1, datetime.max.date(), int(getattr(task, "id", 0) or 0))
    return (0, due, int(getattr(task, "id", 0) or 0))


def format_plan(tasks: list, horizon: str, *, today: date | None = None, owner: str | None = None) -> str:
    key = normalize_horizon(horizon)
    if today is None:
        today = datetime.now(timezone.utc).date()
    lines = [f"План на {_HORIZON_TITLE[key]}:"]
    chosen = []
    for task in tasks:
        if STATUS_DONE in getattr(task, "labels", []) or getattr(task, "done", False):
            continue
        if not personal_task(task, owner):
            continue
        planned = planned_moment(task)
        if planned is not None:
            matched = date_in_horizon([planned], key, today)
        else:
            legacy = legacy_plan_labels(task)
            if legacy:
                wanted = set(_HORIZONS[key])
                matched = any(label in wanted for label in legacy)
            else:
                due = _calendar_date(getattr(task, "due_date", None))
                matched = due is not None and date_in_horizon([due], key, today)
        if matched:
            chosen.append(task)
    chosen.sort(key=_due_sort_key)
    if not chosen:
        lines.append("Нет задач на этот период.")
    for task in chosen:
        fields, _body = parse_description(getattr(task, "description", ""))
        step = fields["Следующий шаг"] or "—"
        due = getattr(task, "due_date", None) or "без дедлайна"
        lines.append(f"- #{task.id} {task.title} — шаг: {step}; дедлайн: {due}")
    blocked = [
        task
        for task in tasks
        if STATUS_BLOCKED in getattr(task, "labels", [])
        and STATUS_DONE not in getattr(task, "labels", [])
        and personal_task(task, owner)
    ]
    blocked.sort(key=_due_sort_key)
    lines.append("Блокеры:")
    if not blocked:
        lines.append("—")
    else:
        for task in blocked:
            lines.append(f"- #{task.id} {task.title}")
    return "\n".join(lines)


def overdue_lines(tasks: list, now: datetime) -> list[str]:
    today = now.date()
    lines: list[str] = []
    for task in tasks:
        if STATUS_DONE in task.labels or task.done:
            continue
        due = parse_dt(task.due_date)
        if due is None or due.date() >= today:
            continue
        lines.append(f"- #{task.id} {task.title} ({task.due_date})")
    return lines


def _in_report_window(created: str | None, window_start: datetime) -> bool:
    parsed = parse_dt(created)
    if parsed is None:
        return True
    return parsed >= window_start


def format_report(
    tasks: list,
    comments_by_task: dict[int, list],
    now: datetime,
    *,
    notes: list | None = None,
    activities: list | None = None,
    artifacts: list | None = None,
) -> str:
    window_start = now - timedelta(days=7)
    buckets: dict[str, list[str]] = {
        "факт": [],
        "предположение": [],
        "ответ человека": [],
        "неизвестно": [],
    }
    sides: dict[str, list[str]] = {
        "Поручения": [],
        "Заявления": [],
        "Файлы": [],
        "События": [],
        "Заметки": [],
    }
    titles = {task.id: task.title for task in tasks}
    for task_id, comments in comments_by_task.items():
        for comment in comments:
            created = getattr(comment, "created", None)
            if not _in_report_window(created, window_start):
                continue
            kind, source, body = parse_comment(getattr(comment, "text", ""))
            if not body:
                continue
            source_bit = f" (источник: {source})" if source else ""
            title = titles.get(task_id, "")
            line = f"- #{task_id} {title}: {body}{source_bit}"
            buckets.get(kind, buckets["неизвестно"]).append(line)
    for activity in activities or []:
        created = activity.get("created_at") if isinstance(activity, dict) else None
        if not _in_report_window(created, window_start):
            continue
        kind = str(activity.get("evidence_kind") or "")
        message = str(activity.get("message") or "").strip()
        if not message:
            continue
        source = str(activity.get("source") or "")
        file_path = str(activity.get("file_path") or "")
        source_bit = f" (источник: {source})" if source else ""
        file_bit = f" файл: {file_path}" if file_path else ""
        line = f"- {message}{source_bit}{file_bit}"
        file_exists = bool(file_path) and Path(file_path).is_file()
        if kind == "заявление":
            buckets["факт"].append(line)
            sides["Заявления"].append(line)
        elif kind == "файл" and file_exists:
            buckets["факт"].append(line)
            sides["Файлы"].append(line)
        elif kind == "событие" and file_exists:
            buckets["факт"].append(line)
            sides["События"].append(line)
        elif kind == "поручение":
            sides["Поручения"].append(line)
        elif kind == "событие":
            sides["События"].append(line)
        elif kind == "файл":
            sides["Файлы"].append(line)
    for note in notes or []:
        if isinstance(note, dict):
            created = note.get("created_at")
            text = str(note.get("text") or "")
        else:
            created = None
            text = str(note)
        if not text.strip() or not _in_report_window(created, window_start):
            continue
        sides["Заметки"].append(f"- {text.strip()}")
    for artifact in artifacts or []:
        if not isinstance(artifact, dict):
            continue
        if not _in_report_window(artifact.get("created_at"), window_start):
            continue
        path = str(artifact.get("path") or "")
        transcript = str(artifact.get("transcript") or "").strip()
        if not path and not transcript:
            continue
        piece = path or transcript
        if transcript and path:
            piece = f"{path}: {transcript}"
        sides["Файлы"].append(f"- {piece}")
    sections = (
        ("Подтверждённые факты", "факт"),
        ("Предположения", "предположение"),
        ("Ответы человека", "ответ человека"),
        ("Неизвестно", "неизвестно"),
    )
    lines = ["Недельный отчёт"]
    for title, key in sections:
        lines.append(f"{title}:")
        lines.extend(buckets[key] or ["—"])
    for title in ("Поручения", "Заявления", "Файлы", "События", "Заметки"):
        lines.append(f"{title}:")
        lines.extend(sides[title] or ["—"])
    return "\n".join(lines)


def render_event(event: dict) -> str:
    facts = event.get("facts") or []
    if isinstance(facts, list):
        facts_text = "\n".join(f"- {item}" for item in facts) or "—"
    else:
        facts_text = str(facts)
    artifacts = event.get("artifacts") or []
    return (
        "Структурированное событие task-sync. "
        "Коммит, резюме сессии и этот текст не завершают задачу.\n"
        f"Источник: {event.get('source') or ''}\n"
        f"Сессия: {event.get('session_id') or ''}\n"
        f"Время: {event.get('timestamp') or ''}\n"
        f"Проект: {event.get('project') or ''}\n"
        f"Подсказка задачи: {event.get('task_hint') or ''}\n"
        f"Факты:\n{facts_text}\n"
        f"Артефакты: {json.dumps(artifacts, ensure_ascii=False)}\n"
        f"Следующий шаг: {event.get('next_step') or ''}\n"
        f"Уверенность источника: {event.get('confidence')}\n"
    )
