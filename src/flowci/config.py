"""Чтение настроек из .env в корне репозитория. Секреты не печатаются."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

_SECRET_FIELDS = frozenset(
    {
        "routerai_api_key",
        "tg_bot_token",
        "vikunja_token",
        "flowci_ingest_token",
    }
)


def find_repo_root() -> Path:
    override = os.environ.get("FLOWCI_REPO_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file() and (parent / "src" / "flowci").is_dir():
            return parent
    return here.parents[2]


def resolve_timezone(name: str) -> timezone | ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        if name == "Europe/Moscow":
            return timezone(timedelta(hours=3), name="Europe/Moscow")
        return timezone.utc


def _raw(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _int(name: str, default: int) -> int:
    raw = _raw(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _user_ids(raw: str) -> tuple[int, ...]:
    ids: list[int] = []
    for part in raw.split(","):
        piece = part.strip()
        if not piece:
            continue
        try:
            ids.append(int(piece))
        except ValueError:
            continue
    return tuple(ids)


@dataclass(frozen=True)
class Settings:
    routerai_api_key: str
    routerai_base_url: str
    routerai_chat_model: str
    routerai_stt_model: str
    tg_bot_token: str
    tg_allowed_user_ids: tuple[int, ...]
    vikunja_url: str
    vikunja_token: str
    vikunja_project_id: int | None
    flowci_ingest_token: str
    flowci_ingest_host: str
    flowci_ingest_port: int
    flowci_timezone: str
    flowci_morning_hour: int
    flowci_data_dir: Path
    repo_root: Path

    def __repr__(self) -> str:
        parts: list[str] = []
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name in _SECRET_FIELDS:
                shown = "set" if value else "missing"
            else:
                shown = repr(value)
            parts.append(f"{name}={shown}")
        return "Settings(" + ", ".join(parts) + ")"

    @property
    def db_path(self) -> Path:
        return self.flowci_data_dir / "flowci.db"

    @property
    def artifacts_dir(self) -> Path:
        return self.flowci_data_dir / "artifacts"

    def public_dict(self) -> dict[str, object]:
        return {
            "routerai_base_url": self.routerai_base_url,
            "routerai_chat_model": self.routerai_chat_model,
            "routerai_stt_model": self.routerai_stt_model,
            "routerai_api_key_set": bool(self.routerai_api_key),
            "tg_bot_token_set": bool(self.tg_bot_token),
            "tg_allowed_user_count": len(self.tg_allowed_user_ids),
            "vikunja_url": self.vikunja_url,
            "vikunja_token_set": bool(self.vikunja_token),
            "vikunja_project_id": self.vikunja_project_id,
            "ingest_token_set": bool(self.flowci_ingest_token),
            "ingest_host": "127.0.0.1",
            "ingest_port": self.flowci_ingest_port,
            "timezone": self.flowci_timezone,
            "morning_hour": self.flowci_morning_hour,
            "data_dir": str(self.flowci_data_dir),
        }


def load_settings() -> Settings:
    root = find_repo_root()
    load_dotenv(root / ".env", override=False)
    data_dir = Path(_raw("FLOWCI_DATA_DIR", "data") or "data")
    if not data_dir.is_absolute():
        data_dir = (root / data_dir).resolve()
    project_raw = _raw("VIKUNJA_PROJECT_ID")
    project_id = int(project_raw) if project_raw.isdigit() else None
    morning = _int("FLOWCI_MORNING_HOUR", 9)
    if morning < 0 or morning > 23:
        morning = 9
    port = _int("FLOWCI_INGEST_PORT", 8787)
    if port < 1 or port > 65535:
        port = 8787
    host = _raw("FLOWCI_INGEST_HOST", "127.0.0.1") or "127.0.0.1"
    return Settings(
        routerai_api_key=_raw("ROUTERAI_API_KEY"),
        routerai_base_url=_raw("ROUTERAI_BASE_URL", "https://routerai.ru/api/v1")
        or "https://routerai.ru/api/v1",
        routerai_chat_model=_raw("ROUTERAI_CHAT_MODEL", "openai/gpt-6.1-sol")
        or "openai/gpt-6.1-sol",
        routerai_stt_model=_raw("ROUTERAI_STT_MODEL", "openai/whisper-1") or "openai/whisper-1",
        tg_bot_token=_raw("TG_BOT_TOKEN"),
        tg_allowed_user_ids=_user_ids(_raw("TG_ALLOWED_USER_IDS")),
        vikunja_url=_raw("VIKUNJA_URL", "http://127.0.0.1:3456") or "http://127.0.0.1:3456",
        vikunja_token=_raw("VIKUNJA_TOKEN"),
        vikunja_project_id=project_id,
        flowci_ingest_token=_raw("FLOWCI_INGEST_TOKEN"),
        flowci_ingest_host=host,
        flowci_ingest_port=port,
        flowci_timezone=_raw("FLOWCI_TIMEZONE", "Europe/Moscow") or "Europe/Moscow",
        flowci_morning_hour=morning,
        flowci_data_dir=data_dir,
        repo_root=root,
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)
