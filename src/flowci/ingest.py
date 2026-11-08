"""Локальный приём событий task-sync. Слушает только 127.0.0.1."""

from __future__ import annotations

import hmac
import json
import logging
from datetime import datetime, timezone

from aiohttp import web

from flowci.config import Settings

log = logging.getLogger("flowci.ingest")
_AGENT_KEY: web.AppKey = web.AppKey("flowci_agent")
_TOKEN_KEY: web.AppKey = web.AppKey("flowci_token")


def loopback_host(host: str) -> str:
    if host.strip().lower() in {"127.0.0.1", "localhost", "::1"}:
        return "127.0.0.1"
    return "127.0.0.1"


def normalize_event(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    source = str(data.get("source") or "").strip()
    key = str(data.get("deduplication_key") or "").strip()
    if not source:
        raise ValueError("missing source")
    if not key:
        raise ValueError("missing deduplication_key")
    confidence = data.get("confidence")
    try:
        confidence_value = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence_value = 0.0
    timestamp = str(data.get("timestamp") or "").strip()
    if not timestamp:
        timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    return {
        "source": source,
        "session_id": str(data.get("session_id") or ""),
        "timestamp": timestamp,
        "project": str(data.get("project") or ""),
        "task_hint": str(data.get("task_hint") or ""),
        "facts": _string_list(data.get("facts")),
        "artifacts": _string_list(data.get("artifacts")),
        "next_step": str(data.get("next_step") or ""),
        "confidence": max(0.0, min(1.0, confidence_value)),
        "deduplication_key": key,
    }


def create_app(agent: object, token: str) -> web.Application:
    app = web.Application()
    app[_AGENT_KEY] = agent
    app[_TOKEN_KEY] = token
    app.router.add_get("/health", _health)
    app.router.add_post("/v1/events", _events)
    return app


async def start_ingest(agent: object, settings: Settings) -> web.AppRunner:
    host = loopback_host(settings.flowci_ingest_host)
    app = create_app(agent, settings.flowci_ingest_token)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host=host, port=settings.flowci_ingest_port).start()
    if not settings.flowci_ingest_token:
        log.warning("FLOWCI_INGEST_TOKEN is not set; ingest will reject requests")
    log.info("ingest listening on %s:%s", host, settings.flowci_ingest_port)
    return runner


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _events(request: web.Request) -> web.Response:
    token = str(request.app[_TOKEN_KEY] or "")
    expected = f"Bearer {token}" if token else ""
    got = request.headers.get("Authorization", "")
    if not token or not _equals(got, expected):
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "invalid json"}, status=400)
    try:
        event = normalize_event(data)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    result = await request.app[_AGENT_KEY].handle_event(event)
    log.info("event action=%s noop=%s task_id=%s", result.action, result.noop, result.task_id)
    return web.json_response(
        {
            "ok": result.ok,
            "action": result.action,
            "task_id": result.task_id,
            "noop": result.noop,
            "busy": bool(getattr(result, "busy", False)),
            "needs_confirmation": result.needs_confirmation,
            "user_message": result.user_message,
        }
    )


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            else:
                text = json.dumps(item, ensure_ascii=False)
            if text:
                result.append(text)
        return result
    return [str(value)]


def _equals(left: str, right: str) -> bool:
    left_bytes = left.encode("utf-8")
    right_bytes = right.encode("utf-8")
    if len(left_bytes) != len(right_bytes):
        return False
    return hmac.compare_digest(left_bytes, right_bytes)
