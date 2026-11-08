"""Журнал агента: аудит, диалог, события, напоминания, активности."""

from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

_JOURNAL = frozenset({"claimed", "inflight", "task_ready", "comment_pending", "awaiting_input", "done"})
_LEASE_SECONDS = 120
_RESUME_STAGES = frozenset({"claimed", "inflight", "task_ready", "comment_pending"})
_held_claims: contextvars.ContextVar[dict[str, int] | None] = contextvars.ContextVar(
    "flowci_held_claims",
    default=None,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _expires(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = db_path
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._lock = threading.Lock()
        self._owner_token = f"{uuid.uuid4().hex}"
        self._init()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _init(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dedupe (
                    deduplication_key TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    task_id INTEGER,
                    user_message TEXT,
                    created_at TEXT NOT NULL,
                    stage TEXT,
                    payload_json TEXT,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    source TEXT,
                    action TEXT,
                    task_id INTEGER,
                    summary TEXT
                );
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fire_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    task_id INTEGER,
                    sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    path TEXT,
                    kind TEXT,
                    task_id INTEGER,
                    transcript TEXT,
                    source TEXT
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    text TEXT NOT NULL,
                    source TEXT,
                    session_id TEXT
                );
                CREATE TABLE IF NOT EXISTS pending_confirmations (
                    token TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    task_id INTEGER,
                    source TEXT,
                    payload_json TEXT NOT NULL,
                    user_id INTEGER,
                    expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                );
                CREATE TABLE IF NOT EXISTS dialogs (
                    chat_key TEXT PRIMARY KEY,
                    original_text TEXT NOT NULL,
                    question TEXT NOT NULL,
                    expected_slot TEXT NOT NULL,
                    event_id TEXT,
                    operation TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    task_id INTEGER,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_key TEXT,
                    text TEXT NOT NULL,
                    delivered INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    task_id INTEGER,
                    source TEXT,
                    author TEXT,
                    message TEXT NOT NULL,
                    evidence_kind TEXT NOT NULL,
                    file_path TEXT,
                    action TEXT
                );
                CREATE TABLE IF NOT EXISTS task_write_leases (
                    task_id INTEGER PRIMARY KEY,
                    owner TEXT NOT NULL,
                    lease_until TEXT NOT NULL
                );
                """
            )
            self._migrate()
            self._conn.commit()

    def _columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}

    def _add_column(self, table: str, column: str, ddl: str) -> None:
        if column not in self._columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate(self) -> None:
        self._add_column("dedupe", "stage", "TEXT")
        self._add_column("dedupe", "payload_json", "TEXT")
        self._add_column("dedupe", "updated_at", "TEXT")
        self._add_column("dedupe", "owner", "TEXT")
        self._add_column("dedupe", "lease_until", "TEXT")
        self._add_column("reminders", "status", "TEXT NOT NULL DEFAULT 'pending'")
        self._add_column("pending_confirmations", "user_id", "INTEGER")
        self._add_column("pending_confirmations", "expires_at", "TEXT")
        self._add_column("pending_confirmations", "status", "TEXT NOT NULL DEFAULT 'pending'")
        self._add_column("dialogs", "source", "TEXT")
        self._add_column("dialogs", "extra_json", "TEXT")
        self._add_column("dialogs", "allow_completion", "INTEGER")
        self._add_column("dialogs", "answers_json", "TEXT")
        self._add_column("dialogs", "question_message_id", "INTEGER")
        self._add_column("dialogs", "question_version", "INTEGER")
        self._add_column("dedupe", "generation", "INTEGER NOT NULL DEFAULT 0")
        self._conn.execute(
            "UPDATE pending_confirmations SET status = 'pending' WHERE status = 'executing'"
        )
        self._conn.execute("UPDATE outbox SET delivered = 0 WHERE delivered = -1")
        self._conn.execute(
            "UPDATE reminders SET status = 'sent' "
            "WHERE sent = 1 AND (status IS NULL OR status = '' OR status = 'pending')"
        )

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self._conn.commit()

    def bind_owner(self, user_id: int, allowed: tuple[int, ...]) -> bool:
        if allowed:
            return user_id in allowed
        current = self.get_meta("owner_telegram_id")
        if current is None:
            self.set_meta("owner_telegram_id", str(user_id))
            return True
        return current == str(user_id)

    def bind_owner_chat(self, chat_id: int, user_id: int | None = None) -> None:
        if user_id is not None:
            self.set_meta(f"owner_chat:{int(user_id)}", str(int(chat_id)))
            return
        current = self.get_meta("owner_telegram_chat_id")
        if current is None:
            self.set_meta("owner_telegram_chat_id", str(chat_id))
        owner = self.get_meta("owner_telegram_id")
        if owner and owner.isdigit():
            self.set_meta(f"owner_chat:{owner}", str(chat_id))

    def chat_for_user(self, user_id: int) -> int | None:
        current = self.get_meta(f"owner_chat:{int(user_id)}")
        if current is not None:
            try:
                return int(current)
            except ValueError:
                return None
        owner = self.get_meta("owner_telegram_id")
        if owner is not None and owner == str(user_id):
            return self.owner_chat_id()
        return None

    def delivery_chats(self, allowed: tuple[int, ...]) -> list[int]:
        chats: list[int] = []
        for user_id in self.owner_ids(allowed):
            chat_id = self.chat_for_user(user_id)
            if chat_id is not None and chat_id not in chats:
                chats.append(chat_id)
        return chats

    def is_owner(self, user_id: int, allowed: tuple[int, ...]) -> bool:
        if allowed:
            return user_id in allowed
        current = self.get_meta("owner_telegram_id")
        return current is not None and current == str(user_id)

    def owner_chat_id(self) -> int | None:
        current = self.get_meta("owner_telegram_chat_id")
        if current is None:
            return None
        try:
            return int(current)
        except ValueError:
            return None

    def owner_ids(self, allowed: tuple[int, ...]) -> list[int]:
        if allowed:
            return list(allowed)
        current = self.get_meta("owner_telegram_id")
        if current is None:
            return []
        try:
            return [int(current)]
        except ValueError:
            return []

    def claim_dedupe(self, key: str) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO dedupe(deduplication_key, action, task_id, user_message, created_at, stage, payload_json, updated_at) "
                    "VALUES (?, 'inflight', NULL, '', ?, 'claimed', '{}', ?)",
                    (key, _utcnow(), _utcnow()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def _stage_of(self, row: sqlite3.Row) -> str:
        stage = row["stage"] if "stage" in row.keys() and row["stage"] else ""
        if stage:
            return str(stage)
        action = str(row["action"] or "")
        if action in {"inflight", "claimed"}:
            return "claimed"
        if action in _JOURNAL:
            return action
        return "done"

    def get_dedupe(self, key: str) -> tuple[str, int | None] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action, task_id, stage FROM dedupe WHERE deduplication_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        task_id = row["task_id"]
        stage = self._stage_of(row)
        label = str(row["action"]) if stage == "done" else stage
        return label, None if task_id is None else int(task_id)

    def event_stage(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action, stage FROM dedupe WHERE deduplication_key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        return self._stage_of(row)

    def _held_map(self) -> dict[str, int]:
        current = _held_claims.get()
        if not isinstance(current, dict):
            current = {}
            _held_claims.set(current)
        return current

    def _remember_claim(self, key: str, generation: int) -> None:
        self._held_map()[key] = int(generation)

    def _context_generation(self, key: str) -> int | None:
        current = _held_claims.get()
        if not isinstance(current, dict) or key not in current:
            return None
        return int(current[key])

    def release_held_claims(self) -> None:
        current = _held_claims.get()
        if not isinstance(current, dict) or not current:
            return
        with self._lock:
            for key, generation in list(current.items()):
                self._conn.execute(
                    "UPDATE dedupe SET lease_until = NULL "
                    "WHERE deduplication_key = ? AND owner = ? AND generation = ?",
                    (key, self._owner_token, int(generation)),
                )
            self._conn.commit()
        current.clear()

    def renew_claim(self, key: str) -> bool:
        """Продлевает lease, только если поколение всё ещё принадлежит этому вызову."""
        generation = self._context_generation(key)
        if generation is None:
            return False
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT generation, owner, stage, action FROM dedupe WHERE deduplication_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    return False
                if int(row["generation"] or 0) != int(generation):
                    self._conn.rollback()
                    return False
                if str(row["owner"] or "") != self._owner_token:
                    self._conn.rollback()
                    return False
                if self._stage_of(row) == "done":
                    self._conn.rollback()
                    return False
                updated = self._conn.execute(
                    "UPDATE dedupe SET lease_until = ?, updated_at = ? "
                    "WHERE deduplication_key = ? AND generation = ? AND owner = ?",
                    (self._lease_until(), _utcnow(), key, int(generation), self._owner_token),
                )
                if updated.rowcount != 1:
                    self._conn.rollback()
                    return False
                self._conn.commit()
                return True
            except Exception:
                self._conn.rollback()
                raise

    def claim_if_free(self, key: str) -> bool:
        """Берёт свободное событие. Живой чужой lease не отдаёт."""
        if self._context_generation(key) is not None:
            return self.renew_claim(key)
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT action, stage, owner, lease_until, generation FROM dedupe WHERE deduplication_key = ?",
                    (key,),
                ).fetchone()
                if row is None or self._stage_of(row) == "done":
                    self._conn.commit()
                    return True
                generation = int(row["generation"] or 0)
                if self._lease_alive(row["lease_until"]):
                    self._conn.commit()
                    return False
                next_generation = generation + 1 if generation > 0 else 1
                updated = self._conn.execute(
                    "UPDATE dedupe SET owner = ?, lease_until = ?, generation = ?, updated_at = ? "
                    "WHERE deduplication_key = ? AND generation = ?",
                    (self._owner_token, self._lease_until(), next_generation, _utcnow(), key, generation),
                )
                if updated.rowcount != 1:
                    self._conn.commit()
                    return False
                self._conn.commit()
                self._remember_claim(key, next_generation)
                return True
            except Exception:
                self._conn.rollback()
                raise

    def _lease_until(self) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=_LEASE_SECONDS)).replace(microsecond=0).isoformat()

    def _lease_alive(self, value: object) -> bool:
        if value is None:
            return False
        text = str(value).strip()
        if not text:
            return False
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)

    def open_event(self, key: str, payload: dict) -> dict:
        encoded = json.dumps(payload, ensure_ascii=False)
        now = _utcnow()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT action, task_id, user_message, stage, payload_json, owner, lease_until, generation "
                    "FROM dedupe WHERE deduplication_key = ?",
                    (key,),
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO dedupe(deduplication_key, action, task_id, user_message, created_at, stage, payload_json, updated_at, owner, lease_until, generation) "
                        "VALUES (?, 'claimed', NULL, '', ?, 'claimed', ?, ?, ?, ?, 1)",
                        (key, now, encoded, now, self._owner_token, self._lease_until()),
                    )
                    self._conn.commit()
                    self._remember_claim(key, 1)
                    return {
                        "stage": "claimed",
                        "task_id": None,
                        "payload": payload,
                        "user_message": "",
                        "action": "claimed",
                        "fresh": True,
                        "busy": False,
                        "resume": False,
                        "generation": 1,
                    }
                stage = self._stage_of(row)
                try:
                    stored = json.loads(row["payload_json"] or "{}")
                except json.JSONDecodeError:
                    stored = {}
                if not isinstance(stored, dict):
                    stored = {}
                task_id = None if row["task_id"] is None else int(row["task_id"])
                generation = int(row["generation"] or 0)
                result = {
                    "stage": stage,
                    "task_id": task_id,
                    "payload": stored or payload,
                    "user_message": str(row["user_message"] or ""),
                    "action": str(row["action"] or stage),
                    "fresh": False,
                    "busy": False,
                    "resume": False,
                    "generation": generation,
                }
                if stage == "done":
                    self._conn.commit()
                    return result
                alive = self._lease_alive(row["lease_until"])
                owns = (
                    alive
                    and str(row["owner"] or "") == self._owner_token
                    and self._context_generation(key) == generation
                )
                if alive and not owns:
                    result["busy"] = True
                    result["stage"] = "busy"
                    self._conn.commit()
                    return result
                if stage == "awaiting_input":
                    self._conn.commit()
                    return result
                if stage not in _RESUME_STAGES and stage != "claimed":
                    self._conn.commit()
                    return result
                next_generation = generation if owns else generation + 1
                if next_generation <= 0:
                    next_generation = 1
                updated = self._conn.execute(
                    "UPDATE dedupe SET owner = ?, lease_until = ?, generation = ?, updated_at = ? "
                    "WHERE deduplication_key = ? AND generation = ?",
                    (self._owner_token, self._lease_until(), next_generation, now, key, generation),
                )
                if updated.rowcount != 1:
                    result["busy"] = True
                    result["stage"] = "busy"
                    self._conn.commit()
                    return result
                self._conn.commit()
                self._remember_claim(key, next_generation)
                result["resume"] = True
                result["generation"] = next_generation
                return result
            except Exception:
                self._conn.rollback()
                raise

    def set_event(
        self,
        key: str,
        *,
        stage: str,
        action: str | None = None,
        task_id: int | None = None,
        message: str | None = None,
        payload: dict | None = None,
    ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action, task_id, user_message, payload_json FROM dedupe WHERE deduplication_key = ?",
                (key,),
            ).fetchone()
            if row is None:
                return
            try:
                stored = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                stored = {}
            if not isinstance(stored, dict):
                stored = {}
            if payload:
                stored.update(payload)
            self._conn.execute(
                "UPDATE dedupe SET action = ?, task_id = ?, user_message = ?, stage = ?, payload_json = ?, updated_at = ? "
                "WHERE deduplication_key = ?",
                (
                    action if action is not None else str(row["action"] or stage),
                    task_id if task_id is not None else row["task_id"],
                    (message if message is not None else str(row["user_message"] or ""))[:500],
                    stage,
                    json.dumps(stored, ensure_ascii=False),
                    _utcnow(),
                    key,
                ),
            )
            self._conn.commit()

    def finish_dedupe(self, key: str, action: str, task_id: int | None, message: str) -> None:
        self.set_event(key, stage="done", action=action, task_id=task_id, message=message)

    def stale_events(self) -> list[dict]:
        """Незавершённые события без живого lease. Планировщик доводит их сам."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT deduplication_key, action, task_id, user_message, stage, payload_json, lease_until "
                "FROM dedupe"
            ).fetchall()
        chosen: list[dict] = []
        for row in rows:
            stage = self._stage_of(row)
            if stage not in _RESUME_STAGES:
                continue
            if self._lease_alive(row["lease_until"]):
                continue
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            task_id = row["task_id"]
            chosen.append(
                {
                    "key": str(row["deduplication_key"]),
                    "stage": stage,
                    "task_id": None if task_id is None else int(task_id),
                    "payload": payload,
                    "user_message": str(row["user_message"] or ""),
                }
            )
        return chosen

    def release_dedupe(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM dedupe WHERE deduplication_key = ? AND action = 'inflight'",
                (key,),
            )
            self._conn.commit()

    def save_dialog(
        self,
        chat_key: str,
        original_text: str,
        question: str,
        expected_slot: str,
        event_id: str | None,
        operation: str,
        payload: dict,
        task_id: int | None = None,
        *,
        source: str | None = None,
        extra: dict | None = None,
        allow_completion: bool | None = None,
        answers: list | None = None,
        question_message_id: int | None = None,
        question_version: int | None = None,
    ) -> None:
        with self._lock:
            existing = self._conn.execute("SELECT * FROM dialogs WHERE chat_key = ?", (chat_key,)).fetchone()
            previous_event = str(existing["event_id"] or "") if existing is not None else ""
            current_event = str(event_id or "")
            same_context = existing is not None and previous_event == current_event
            if isinstance(extra, dict) and current_event:
                owner = str(extra.get("deduplication_key") or "")
                if owner and owner != current_event:
                    extra = {key: value for key, value in extra.items() if key != "artifacts"}
                    extra["deduplication_key"] = current_event
            if same_context and str(existing["original_text"] or "").strip():
                original_text = str(existing["original_text"])
            if same_context:
                if str(existing["source"] or "").strip():
                    source = str(existing["source"])
                if existing["extra_json"]:
                    try:
                        loaded = json.loads(existing["extra_json"])
                    except json.JSONDecodeError:
                        loaded = None
                    if isinstance(loaded, dict):
                        extra = loaded
                if existing["allow_completion"] is not None:
                    allow_completion = bool(existing["allow_completion"])
                if answers is None and existing["answers_json"]:
                    try:
                        loaded_answers = json.loads(existing["answers_json"])
                    except json.JSONDecodeError:
                        loaded_answers = []
                    if isinstance(loaded_answers, list):
                        answers = [str(item) for item in loaded_answers]
                same_question = str(existing["question"] or "") == question
                if question_version is None:
                    previous = int(existing["question_version"] or 1)
                    question_version = previous if same_question else previous + 1
                if question_message_id is None and same_question:
                    stored_id = existing["question_message_id"]
                    question_message_id = None if stored_id is None else int(stored_id)
            if answers is None:
                answers = []
            if question_version is None:
                question_version = 1
            self._conn.execute(
                "INSERT INTO dialogs("
                "chat_key, original_text, question, expected_slot, event_id, operation, payload_json, task_id, updated_at, "
                "source, extra_json, allow_completion, answers_json, question_message_id, question_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(chat_key) DO UPDATE SET "
                "original_text = excluded.original_text, question = excluded.question, "
                "expected_slot = excluded.expected_slot, event_id = excluded.event_id, "
                "operation = excluded.operation, payload_json = excluded.payload_json, "
                "task_id = excluded.task_id, updated_at = excluded.updated_at, "
                "source = excluded.source, extra_json = excluded.extra_json, "
                "allow_completion = excluded.allow_completion, answers_json = excluded.answers_json, "
                "question_message_id = excluded.question_message_id, question_version = excluded.question_version",
                (
                    chat_key,
                    original_text,
                    question,
                    expected_slot,
                    event_id,
                    operation,
                    json.dumps(payload, ensure_ascii=False),
                    task_id,
                    _utcnow(),
                    source or "",
                    json.dumps(extra or {}, ensure_ascii=False),
                    None if allow_completion is None else int(bool(allow_completion)),
                    json.dumps(answers, ensure_ascii=False),
                    question_message_id,
                    int(question_version),
                ),
            )
            self._conn.commit()

    def _dialog_row(self, row: sqlite3.Row) -> dict:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        task_id = row["task_id"]
        keys = row.keys()
        answers: list[str] = []
        if "answers_json" in keys and row["answers_json"]:
            try:
                loaded_answers = json.loads(row["answers_json"])
            except json.JSONDecodeError:
                loaded_answers = []
            if isinstance(loaded_answers, list):
                answers = [str(item) for item in loaded_answers]
        extra: dict = {}
        if "extra_json" in keys and row["extra_json"]:
            try:
                loaded_extra = json.loads(row["extra_json"])
            except json.JSONDecodeError:
                loaded_extra = {}
            if isinstance(loaded_extra, dict):
                extra = loaded_extra
        message_id = row["question_message_id"] if "question_message_id" in keys else None
        allow = row["allow_completion"] if "allow_completion" in keys else None
        return {
            "chat_key": str(row["chat_key"]),
            "original_text": str(row["original_text"] or ""),
            "question": str(row["question"] or ""),
            "expected_slot": str(row["expected_slot"] or ""),
            "event_id": str(row["event_id"] or "") or None,
            "operation": str(row["operation"] or ""),
            "payload": payload,
            "task_id": None if task_id is None else int(task_id),
            "source": str(row["source"] or "") if "source" in keys else "",
            "extra": extra,
            "allow_completion": None if allow is None else bool(allow),
            "answers": answers,
            "question_message_id": None if message_id is None else int(message_id),
            "question_version": int(row["question_version"] or 1) if "question_version" in keys else 1,
        }

    def get_dialog(self, chat_key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM dialogs WHERE chat_key = ?", (chat_key,)).fetchone()
        if row is None:
            return None
        return self._dialog_row(row)

    def find_event_dialog(self) -> dict | None:
        waiting = self.waiting_event_dialogs()
        if len(waiting) == 1:
            return waiting[0]
        return None

    def waiting_event_dialogs(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dialogs WHERE event_id IS NOT NULL AND event_id != '' ORDER BY updated_at"
            ).fetchall()
        chosen: dict[str, dict] = {}
        for row in rows:
            dialog = self._dialog_row(row)
            event_id = str(dialog.get("event_id") or "")
            if not event_id:
                continue
            stage = self.event_stage(event_id)
            if stage != "awaiting_input":
                continue
            current = chosen.get(event_id)
            if current is None or str(dialog["chat_key"]) == f"event:{event_id}":
                chosen[event_id] = dialog
        return list(chosen.values())

    def find_dialog_by_message(self, message_id: int) -> dict | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM dialogs WHERE question_message_id = ?",
                (int(message_id),),
            ).fetchall()
        if not rows:
            return None
        dialogs = [self._dialog_row(row) for row in rows]
        for dialog in dialogs:
            if str(dialog["chat_key"]).startswith("event:"):
                return dialog
        return dialogs[0]

    def bind_question_message(self, event_key: str, message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE dialogs SET question_message_id = ? "
                "WHERE event_id = ? AND (question_message_id IS NULL OR question_message_id = ?)",
                (int(message_id), event_key, int(message_id)),
            )
            self._conn.commit()

    def clear_dialog(self, chat_key: str, event_id: str | None = None) -> None:
        """Снимает активный диалог. Привязка question_message_id канонической строки остаётся."""
        with self._lock:
            if not str(chat_key).startswith("event:"):
                self._conn.execute("DELETE FROM dialogs WHERE chat_key = ?", (chat_key,))
            if event_id:
                canonical = f"event:{event_id}"
                self._conn.execute(
                    "DELETE FROM dialogs WHERE event_id = ? AND chat_key != ?",
                    (event_id, canonical),
                )
            self._conn.commit()

    def audit(self, source: str, action: str, task_id: int | None, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO audit(created_at, source, action, task_id, summary) VALUES (?, ?, ?, ?, ?)",
                (_utcnow(), source, action, task_id, summary[:500]),
            )
            self._conn.commit()

    def list_audit(self) -> list[tuple[str, str, int | None]]:
        with self._lock:
            rows = self._conn.execute("SELECT action, summary, task_id FROM audit ORDER BY id").fetchall()
        return [
            (str(row["action"]), str(row["summary"] or ""), None if row["task_id"] is None else int(row["task_id"]))
            for row in rows
        ]

    def add_note(self, text: str, source: str, session_id: str | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO notes(created_at, text, source, session_id) VALUES (?, ?, ?, ?)",
                (_utcnow(), text, source, session_id),
            )
            self._conn.commit()

    def list_notes(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute("SELECT text FROM notes ORDER BY id").fetchall()
        return [str(row["text"]) for row in rows]

    def list_notes_full(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT created_at, text, source FROM notes ORDER BY id").fetchall()
        return [
            {"created_at": str(row["created_at"]), "text": str(row["text"]), "source": str(row["source"] or "")}
            for row in rows
        ]

    def add_artifact(
        self,
        path: str,
        kind: str,
        task_id: int | None,
        transcript: str,
        source: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO artifacts(created_at, path, kind, task_id, transcript, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_utcnow(), path, kind, task_id, transcript, source),
            )
            self._conn.commit()

    def list_artifacts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, path, kind, task_id, transcript, source FROM artifacts ORDER BY id"
            ).fetchall()
        return [
            {
                "created_at": str(row["created_at"]),
                "path": str(row["path"] or ""),
                "kind": str(row["kind"] or ""),
                "task_id": None if row["task_id"] is None else int(row["task_id"]),
                "transcript": str(row["transcript"] or ""),
                "source": str(row["source"] or ""),
            }
            for row in rows
        ]

    def add_activity(
        self,
        *,
        task_id: int | None,
        source: str,
        author: str,
        message: str,
        evidence_kind: str,
        file_path: str = "",
        action: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO activities(created_at, task_id, source, author, message, evidence_kind, file_path, action) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_utcnow(), task_id, source, author, message, evidence_kind, file_path, action),
            )
            self._conn.commit()

    def list_activities(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT created_at, task_id, source, author, message, evidence_kind, file_path, action "
                "FROM activities ORDER BY id"
            ).fetchall()
        return [
            {
                "created_at": str(row["created_at"]),
                "task_id": None if row["task_id"] is None else int(row["task_id"]),
                "source": str(row["source"] or ""),
                "author": str(row["author"] or ""),
                "message": str(row["message"] or ""),
                "evidence_kind": str(row["evidence_kind"] or ""),
                "file_path": str(row["file_path"] or ""),
                "action": str(row["action"] or ""),
            }
            for row in rows
        ]

    def add_reminder(self, fire_at: str, text: str, task_id: int | None) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO reminders(fire_at, text, task_id, sent, attempts, created_at, status) "
                "VALUES (?, ?, ?, 0, 0, ?, 'pending')",
                (fire_at, text, task_id, _utcnow()),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_reminders(self) -> list[tuple[int, str, str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, fire_at, text, sent FROM reminders ORDER BY id"
            ).fetchall()
        return [(int(row["id"]), str(row["fire_at"]), str(row["text"]), int(row["sent"])) for row in rows]

    def reminder_status(self, reminder_id: int) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT status, sent FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
        if row is None:
            return ""
        status = str(row["status"] or "")
        if status:
            return status
        return "sent" if int(row["sent"]) else "pending"

    def due_reminders(self, now_iso: str) -> list[tuple[int, str, int]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, text, attempts FROM reminders WHERE fire_at <= ? AND "
                "(status = 'pending' OR ((status IS NULL OR status = '') AND sent = 0)) "
                "ORDER BY fire_at",
                (now_iso,),
            ).fetchall()
        return [(int(row["id"]), str(row["text"]), int(row["attempts"])) for row in rows]

    def mark_reminder_sent(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET sent = 1, status = 'sent' WHERE id = ?",
                (reminder_id,),
            )
            self._conn.commit()

    def mark_reminder_failed(self, reminder_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET sent = 0, status = 'failed' WHERE id = ?",
                (reminder_id,),
            )
            self._conn.commit()

    def bump_reminder_attempt(self, reminder_id: int) -> int:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET attempts = attempts + 1 WHERE id = ?",
                (reminder_id,),
            )
            row = self._conn.execute(
                "SELECT attempts FROM reminders WHERE id = ?",
                (reminder_id,),
            ).fetchone()
            self._conn.commit()
        return 0 if row is None else int(row["attempts"])

    def enqueue_outbox(self, event_key: str, text: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM outbox WHERE event_key = ? AND text = ? ORDER BY id DESC LIMIT 1",
                (event_key, text),
            ).fetchone()
            if row is not None:
                return False
            self._conn.execute(
                "INSERT INTO outbox(created_at, event_key, text, delivered, attempts) VALUES (?, ?, ?, 0, 0)",
                (_utcnow(), event_key, text),
            )
            self._conn.commit()
            return True

    def drop_outbox(self, event_key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM outbox WHERE event_key = ? AND delivered != 1",
                (event_key,),
            )
            self._conn.commit()

    def claim_outbox(self, outbox_id: int) -> bool:
        """Один отправитель забирает pending-вопрос. Второй вызов ничего не получает."""
        with self._lock:
            updated = self._conn.execute(
                "UPDATE outbox SET delivered = -1 WHERE id = ? AND delivered = 0",
                (int(outbox_id),),
            )
            self._conn.commit()
            return updated.rowcount == 1

    def release_outbox(self, outbox_id: int, *, bump: bool = False) -> None:
        with self._lock:
            if bump:
                self._conn.execute(
                    "UPDATE outbox SET delivered = 0, attempts = attempts + 1 WHERE id = ? AND delivered = -1",
                    (int(outbox_id),),
                )
            else:
                self._conn.execute(
                    "UPDATE outbox SET delivered = 0 WHERE id = ? AND delivered = -1",
                    (int(outbox_id),),
                )
            self._conn.commit()

    def pending_outbox(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, event_key, text, attempts FROM outbox WHERE delivered = 0 ORDER BY id"
            ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "event_key": str(row["event_key"] or ""),
                "text": str(row["text"] or ""),
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]

    def ack_outbox(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE outbox SET delivered = 1 WHERE id = ?", (outbox_id,))
            self._conn.commit()

    def bump_outbox(self, outbox_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (outbox_id,))
            self._conn.commit()

    def save_pending(
        self,
        token: str,
        action: str,
        task_id: int | None,
        source: str,
        payload: dict,
        user_id: int | None = None,
        expires_at: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending_confirmations("
                "token, created_at, action, task_id, source, payload_json, user_id, expires_at, status"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                (
                    token,
                    _utcnow(),
                    action,
                    task_id,
                    source,
                    json.dumps(payload, ensure_ascii=False),
                    user_id,
                    expires_at or _expires(),
                ),
            )
            self._conn.commit()

    def _pending_dict(self, row: sqlite3.Row) -> dict:
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        task_id = row["task_id"]
        user_id = row["user_id"] if "user_id" in row.keys() else None
        return {
            "action": str(row["action"]),
            "task_id": None if task_id is None else int(task_id),
            "source": str(row["source"] or ""),
            "payload": payload,
            "user_id": None if user_id is None else int(user_id),
            "expires_at": str(row["expires_at"] or "") if "expires_at" in row.keys() else "",
            "status": str(row["status"] or "pending") if "status" in row.keys() else "pending",
        }

    def claim_pending(self, token: str, user_id: int | None) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT action, task_id, source, payload_json, user_id, expires_at, status "
                "FROM pending_confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return None
            pending = self._pending_dict(row)
            status = pending["status"] or "pending"
            if status == "applied":
                return None
            if status not in {"pending", ""}:
                return None
            expires_at = pending["expires_at"]
            if expires_at and expires_at < _utcnow():
                return None
            bound = pending["user_id"]
            if bound is not None and user_id is not None and int(bound) != int(user_id):
                return None
            if bound is not None and user_id is None:
                return None
            updated = self._conn.execute(
                "UPDATE pending_confirmations SET status = 'executing' "
                "WHERE token = ? AND (status = 'pending' OR status IS NULL OR status = '')",
                (token,),
            )
            if updated.rowcount != 1:
                self._conn.commit()
                return None
            self._conn.commit()
        return pending

    def restore_pending(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_confirmations SET status = 'pending' WHERE token = ? AND status = 'executing'",
                (token,),
            )
            self._conn.commit()

    def finish_pending(self, token: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE pending_confirmations SET status = 'applied' WHERE token = ?",
                (token,),
            )
            self._conn.commit()

    def cancel_pending(self, token: str, user_id: int | None = None) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT user_id, status FROM pending_confirmations WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                return False
            status = str(row["status"] or "pending")
            if status == "applied":
                return False
            bound = row["user_id"]
            if bound is not None and user_id is not None and int(bound) != int(user_id):
                return False
            self._conn.execute("DELETE FROM pending_confirmations WHERE token = ?", (token,))
            self._conn.commit()
        return True

    def pop_pending(self, token: str) -> dict | None:
        return self.claim_pending(token, None)
