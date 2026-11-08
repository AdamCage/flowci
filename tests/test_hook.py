from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / ".cursor" / "hooks" / "task-sync.py"


def load_hook():
    spec = importlib.util.spec_from_file_location("flowci_task_sync_hook", HOOK)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def clean_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(word in upper for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")):
            continue
        env[key] = value
    env["PYTHONUTF8"] = "1"
    return env


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.captured.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization", ""),
                "body": body,
            }
        )
        payload = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args) -> None:
        return


class _Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.captured: list[dict] = []


def test_build_event_filters_env_paths() -> None:
    hook = load_hook()
    event = hook.build_event(
        {
            "task_hint": "Сводка",
            "facts": ["тест зелёный"],
            "next_step": "ещё раз прогнать тесты",
            "project": "FlowCI",
        },
        " M src/flowci/agent.py\n?? .env\n",
        {"conversation_id": "conv-1"},
        datetime(2026, 11, 8, tzinfo=timezone.utc),
    )
    assert event is not None
    assert event["source"] == "cursor"
    assert event["task_hint"] == "Сводка"
    assert event["session_id"] == "conv-1"
    encoded = json.dumps(event, ensure_ascii=False)
    assert ".env" not in encoded
    assert "agent.py" in encoded
    assert event["deduplication_key"].startswith("cursor:conv-1:")
    assert hook.build_event({}, "", {}, datetime(2026, 11, 8, tzinfo=timezone.utc)) is None


def test_find_root_stops_at_local_env(tmp_path) -> None:
    (tmp_path / ".env").write_text("FLOWCI_INGEST_TOKEN=local-test\n", encoding="utf-8")
    assert load_hook().find_root(tmp_path) == tmp_path.resolve()


def test_hook_posts_event_and_fails_open(tmp_path) -> None:
    server = _Server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        _write_note(tmp_path, port)
        completed = _run_hook(tmp_path)
    finally:
        server.shutdown()
        server.server_close()
    assert completed.returncode == 0
    assert completed.stdout.strip() == "{}"
    assert "hook-test-token" not in completed.stdout
    assert "hook-test-token" not in completed.stderr
    assert server.captured, "hook did not post"
    posted = server.captured[0]
    assert posted["path"] == "/v1/events"
    assert posted["authorization"] == "Bearer hook-test-token", "authorization header did not match the test token"
    body = json.loads(posted["body"].decode("utf-8"))
    assert body["task_hint"] == "Починить отчёт"
    assert body["source"] == "cursor"
    assert body["facts"] == ["тест прошёл"]
    assert "hook-test-token" not in posted["body"].decode("utf-8")

    closed = _free_port()
    _write_note(tmp_path, closed)
    failed = _run_hook(tmp_path)
    assert failed.returncode == 0
    assert failed.stdout.strip() == "{}"
    assert "hook-test-token" not in failed.stdout
    assert "hook-test-token" not in failed.stderr
    assert "failed open" in failed.stderr


def _write_note(tmp_path: Path, port: int) -> None:
    (tmp_path / ".flowci").mkdir(exist_ok=True)
    (tmp_path / ".env").write_text(
        f"FLOWCI_INGEST_TOKEN=hook-test-token\nFLOWCI_INGEST_PORT={port}\n",
        encoding="utf-8",
    )
    (tmp_path / ".flowci" / "session.json").write_text(
        json.dumps(
            {
                "task_hint": "Починить отчёт",
                "facts": ["тест прошёл"],
                "next_step": "ничего",
                "project": "FlowCI",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _run_hook(tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOK)],
        cwd=tmp_path,
        input="{}\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=clean_env(),
        timeout=20,
        check=False,
    )


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port
