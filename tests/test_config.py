from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from flowci.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


def test_config_repr_hides_secrets(tmp_path, monkeypatch) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ROUTERAI_API_KEY=planted-router-key",
                "ROUTERAI_BASE_URL=https://router.example/v1",
                "TG_BOT_TOKEN=planted-tg-token",
                "VIKUNJA_TOKEN=planted-vikunja-token",
                "FLOWCI_INGEST_TOKEN=planted-ingest-token",
                "VIKUNJA_URL=http://127.0.0.1:3456",
                "FLOWCI_DATA_DIR=data",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("FLOWCI_REPO_ROOT", str(tmp_path))
    for name in (
        "ROUTERAI_API_KEY",
        "ROUTERAI_BASE_URL",
        "ROUTERAI_CHAT_MODEL",
        "ROUTERAI_STT_MODEL",
        "TG_BOT_TOKEN",
        "TG_ALLOWED_USER_IDS",
        "VIKUNJA_URL",
        "VIKUNJA_TOKEN",
        "VIKUNJA_PROJECT_ID",
        "FLOWCI_INGEST_TOKEN",
        "FLOWCI_INGEST_HOST",
        "FLOWCI_INGEST_PORT",
        "FLOWCI_TIMEZONE",
        "FLOWCI_MORNING_HOUR",
        "FLOWCI_DATA_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = load_settings()
    assert settings.routerai_api_key == "planted-router-key", "expected the temporary .env value"
    blob = repr(settings) + json.dumps(settings.public_dict(), ensure_ascii=False)
    for secret in (
        "planted-router-key",
        "planted-tg-token",
        "planted-vikunja-token",
        "planted-ingest-token",
    ):
        assert secret not in blob
    assert settings.routerai_base_url == "https://router.example/v1"
    assert settings.routerai_chat_model == "openai/gpt-6.1-sol"
    assert settings.flowci_ingest_port == 8787
    assert settings.flowci_morning_hour == 9
    assert settings.db_path == (tmp_path / "data" / "flowci.db").resolve()


def test_packages_import() -> None:
    import flowci
    import flowci.agent
    import flowci.cli
    import flowci.ingest
    import flowci.llm
    import flowci.schedule
    import flowci.telegram
    import flowci.vikunja

    assert flowci.__version__ == "0.1.0"
    assert flowci.agent.MIN_CONFIDENCE == 0.67
    assert (ROOT / "src" / "flowci" / "__main__.py").is_file()
    assert datetime.now(timezone.utc).tzinfo is not None
