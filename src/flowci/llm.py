"""RouterAI через OpenAI SDK: чат и распознавание голоса."""

from __future__ import annotations

from pathlib import Path

from openai import AsyncOpenAI

from flowci.config import Settings


class RouterAI:
    def __init__(self, settings: Settings) -> None:
        self._api_key = settings.routerai_api_key
        self._chat_model = settings.routerai_chat_model
        self._stt_model = settings.routerai_stt_model
        self._client = AsyncOpenAI(api_key=settings.routerai_api_key or "missing", base_url=settings.routerai_base_url)

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(self, system: str, user: str) -> str:
        self._require_key()
        try:
            return await self._chat(system, user, json_mode=True)
        except Exception as first:
            status = getattr(first, "status_code", None)
            if status not in {400, 422}:
                raise RuntimeError(f"RouterAI chat failed ({type(first).__name__}, status={status})") from None
        try:
            return await self._chat(system, user, json_mode=False)
        except Exception as second:
            status = getattr(second, "status_code", None)
            raise RuntimeError(f"RouterAI chat failed ({type(second).__name__}, status={status})") from None

    async def transcribe(self, path: Path) -> str:
        self._require_key()
        try:
            with path.open("rb") as handle:
                result = await self._client.audio.transcriptions.create(
                    model=self._stt_model,
                    file=handle,
                    language="ru",
                )
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            raise RuntimeError(f"RouterAI transcription failed ({type(exc).__name__}, status={status})") from None
        return getattr(result, "text", "") or str(result)

    async def _chat(self, system: str, user: str, *, json_mode: bool) -> str:
        kwargs: dict = {
            "model": self._chat_model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        response = await self._client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    def _require_key(self) -> None:
        if not self._api_key:
            raise RuntimeError("ROUTERAI_API_KEY is not set")
