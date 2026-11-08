"""Тот же агент из консоли: python -m flowci.cli \"что мне сегодня делать\"."""

from __future__ import annotations

import argparse
import asyncio
import json

from flowci.agent import AgentResult, TaskAgent
from flowci.config import load_settings
from flowci.llm import RouterAI
from flowci.store import Store
from flowci.vikunja import VikunjaClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Спросить Task Agent из консоли")
    parser.add_argument("text", nargs="+", help="Текст поручения или вопроса")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Подтвердить завершение или удаление",
    )
    parser.add_argument("--json", action="store_true", help="Напечатать машинный результат")
    parser.add_argument("--source", default="cli")
    parser.add_argument("--dedupe", default=None)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(
            _run(
                " ".join(args.text),
                confirm=args.confirm,
                source=args.source,
                dedupe=args.dedupe,
            )
        )
    except RuntimeError as exc:
        result = AgentResult(action="ask", user_message=str(exc), ok=False)
    _emit(result, as_json=args.json)
    if not result.ok:
        return 1
    return 0


def _emit(result: AgentResult, *, as_json: bool) -> None:
    if as_json:
        print(
            json.dumps(
                {
                    "ok": result.ok,
                    "action": result.action,
                    "task_id": result.task_id,
                    "user_message": result.user_message,
                },
                ensure_ascii=False,
            )
        )
        return
    print(result.user_message)


async def _run(text: str, *, confirm: bool, source: str, dedupe: str | None) -> AgentResult:
    settings = load_settings()
    store = Store(settings.db_path)
    vikunja = VikunjaClient(settings)
    llm = RouterAI(settings)
    agent = TaskAgent(settings, store, vikunja, llm)
    try:
        return await agent.handle(
            text,
            source=source,
            confirm=confirm,
            deduplication_key=dedupe,
        )
    finally:
        await vikunja.aclose()
        await llm.aclose()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
