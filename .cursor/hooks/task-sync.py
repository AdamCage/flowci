"""Cursor stop hook. Posts a task-sync note and never blocks the agent."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def find_root(start: Path) -> Path:
    start = start.resolve()
    if (start / ".env").is_file() or (start / ".flowci" / "session.json").is_file():
        return start
    for parent in start.parents:
        if (parent / ".flowci" / "session.json").is_file():
            return parent
        if (parent / "src" / "flowci").is_dir() and ((parent / ".git").exists() or (parent / ".env").is_file()):
            return parent
    return start


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setting(file_env: dict[str, str], name: str, default: str = "") -> str:
    if file_env.get(name):
        return str(file_env[name])
    return os.environ.get(name, default) or default


def git_status(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return completed.stdout or ""


def _safe_git_status(status: str) -> str:
    kept: list[str] = []
    for line in status.splitlines():
        lowered = line.lower()
        if ".env" in lowered or "credential" in lowered or "secret" in lowered:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def build_event(note: dict, status: str, stdin_payload: dict, now: datetime) -> dict | None:
    task_hint = str(note.get("task_hint") or "").strip()
    facts_raw = note.get("facts") or []
    if isinstance(facts_raw, str):
        facts = [facts_raw.strip()] if facts_raw.strip() else []
    elif isinstance(facts_raw, list):
        facts = [str(item).strip() for item in facts_raw if str(item).strip()]
    else:
        facts = []
    next_step = str(note.get("next_step") or "").strip()
    artifacts_raw = note.get("artifacts") or []
    if isinstance(artifacts_raw, str):
        artifacts = [artifacts_raw] if artifacts_raw.strip() else []
    elif isinstance(artifacts_raw, list):
        artifacts = [str(item) for item in artifacts_raw if str(item).strip()]
    else:
        artifacts = []
    note_artifacts = list(artifacts)
    safe_status = _safe_git_status(status)
    if safe_status:
        artifacts.append("git status:\n" + safe_status[:4000])
    if not task_hint and not facts and not next_step and not note_artifacts:
        return None
    session_id = str(note.get("session_id") or stdin_payload.get("conversation_id") or "cursor")
    project = str(note.get("project") or "")
    canonical = json.dumps(
        {
            "artifacts": sorted(note_artifacts),
            "facts": facts,
            "next_step": next_step,
            "project": project,
            "session_id": session_id,
            "task_hint": task_hint,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    key = str(note.get("deduplication_key") or f"cursor:{session_id}:{digest}")
    try:
        confidence = float(note.get("confidence") if note.get("confidence") is not None else 0.6)
    except (TypeError, ValueError):
        confidence = 0.6
    confidence = max(0.0, min(1.0, confidence))
    return {
        "source": "cursor",
        "session_id": session_id,
        "timestamp": now.astimezone(timezone.utc).replace(microsecond=0).isoformat(),
        "project": str(note.get("project") or ""),
        "task_hint": task_hint,
        "facts": facts,
        "artifacts": artifacts,
        "next_step": next_step,
        "confidence": confidence,
        "deduplication_key": key,
    }


def post_event(url: str, token: str, event: dict) -> dict:
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        raw = response.read()
    try:
        data = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError("event was not accepted")
    return data


def event_settled(data: dict) -> bool:
    """Файл outbox удаляется только когда событие доведено или вопрос уже в очереди."""
    if data.get("busy") is True:
        return False
    message = str(data.get("user_message") or "")
    if message == "already processing":
        return False
    action = str(data.get("action") or "")
    if action == "ask" and data.get("needs_confirmation"):
        return False
    return True


def _outbox_dir(root: Path) -> Path:
    path = root / ".flowci" / "outbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _outbox_file(root: Path, event: dict) -> Path:
    digest = hashlib.sha256(str(event.get("deduplication_key") or "").encode("utf-8")).hexdigest()[:20]
    return _outbox_dir(root) / f"{digest}.json"


def load_outbox(root: Path) -> list[dict]:
    directory = root / ".flowci" / "outbox"
    if not directory.is_dir():
        return []
    events: list[dict] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("deduplication_key"):
            events.append(data)
    return events


def write_outbox(root: Path, event: dict) -> None:
    _outbox_file(root, event).write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")


def discard_outbox(root: Path, event: dict) -> None:
    path = _outbox_file(root, event)
    if path.is_file():
        path.unlink()


def deliver() -> None:
    raw = sys.stdin.read()
    try:
        stdin_payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        stdin_payload = {}
    if not isinstance(stdin_payload, dict):
        stdin_payload = {}
    root = find_root(Path.cwd())
    file_env = load_env(root / ".env")
    token = setting(file_env, "FLOWCI_INGEST_TOKEN")
    if not token:
        return
    port_raw = setting(file_env, "FLOWCI_INGEST_PORT", "8787") or "8787"
    try:
        port = int(port_raw)
    except ValueError:
        port = 8787
    url = f"http://127.0.0.1:{port}/v1/events"
    events = load_outbox(root)
    seen = {str(item.get("deduplication_key") or "") for item in events}
    note_path = root / ".flowci" / "session.json"
    if note_path.is_file():
        note = None
        try:
            loaded = json.loads(note_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, dict):
            note = loaded
        if isinstance(note, dict):
            event = build_event(note, git_status(root), stdin_payload, datetime.now(timezone.utc))
            if event is not None and str(event.get("deduplication_key") or "") not in seen:
                events.append(event)
    if not events:
        return
    failed = False
    for event in events:
        try:
            data = post_event(url, token, event)
            if event_settled(data):
                discard_outbox(root, event)
            else:
                write_outbox(root, event)
                failed = True
        except Exception:
            write_outbox(root, event)
            failed = True
    if failed:
        raise RuntimeError("undelivered")


def main() -> int:
    try:
        deliver()
    except Exception:
        sys.stderr.write("task-sync: failed open\n")
    sys.stdout.write("{}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
