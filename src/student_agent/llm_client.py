from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    raw_json = match.group(1).strip() if match else text

    # Try standard json load
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        # Try finding first { and last }
        first_brace = raw_json.find("{")
        last_brace = raw_json.rfind("}")
        if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
            return json.loads(raw_json[first_brace : last_brace + 1])
        raise


class OllamaLLMClient:
    """LLM Client for local Ollama models via OpenAI-compatible endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 90.0,
    ) -> None:
        load_dotenv()
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")).strip()
        self.model = (model or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")).strip()
        self.timeout = timeout
        self._client = AsyncOpenAI(
            base_url=self.base_url,
            api_key="ollama",
            timeout=self.timeout,
        )

    async def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        """Generate structured JSON using Ollama with retries."""
        last_err: Exception | None = None

        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(retries):
            try:
                response = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )

                choice = response.choices[0] if response.choices else None
                content = choice.message.content if choice and choice.message else None

                if content:
                    return _extract_json(content)
                raise ValueError(f"Empty response from model {self.model}")

            except Exception as exc:
                last_err = exc
                logger.warning(
                    "Attempt %d with %s failed: %s", attempt + 1, self.model, str(exc)[:120]
                )
                if attempt < retries - 1:
                    await asyncio.sleep(1.0 * (attempt + 1))

        raise RuntimeError(
            f"Ollama model {self.model} failed to generate valid JSON: {last_err}"
        ) from last_err


# Backwards compatibility alias
GeminiLLMClient = OllamaLLMClient
