from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

import httpx2


class LLMReasoner:
    """Specialized reasoning engine tailored for models <= 10B parameters.

    Supports local Ollama/vLLM endpoints (e.g. Qwen 2.5 7B, Llama 3.1 8B, Gemma 2 9B)
    or external OpenAI-compatible providers. Enforces strict token limits (<=1000 tokens)
    and provides seamless fallback to symbolic deterministic logic when offline.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float = 3.0,
    ) -> None:
        self.model = model or os.getenv("LLM_MODEL", "qwen2.5:7b")
        self.api_base = (api_base or os.getenv("LLM_API_BASE", "http://localhost:11434/v1")).rstrip(
            "/"
        )
        self.api_key = api_key or os.getenv("LLM_API_KEY", "ollama")
        self.timeout = timeout
        self.enabled = os.getenv("ENABLE_LLM_REASONER", "false").lower() in (
            "true",
            "1",
            "yes",
        )

    async def is_available(self) -> bool:
        """Check if local or remote model endpoint is reachable."""
        if not self.enabled:
            return False
        try:
            async with httpx2.AsyncClient(timeout=1.0) as client:
                res = await client.get(
                    f"{self.api_base}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return res.status_code == 200
        except Exception:
            return False

    async def analyze_root_cause(
        self,
        *,
        case_id: str,
        order_summary: Mapping[str, Any],
        shipment_verdict: str,
        payment_verdict: str,
    ) -> dict[str, Any] | None:
        """Query the <=10B model with structured prompt and strict guardrail."""
        if not self.enabled:
            return None

        prompt = (
            f"You are an e-commerce dispute analyst. Analyze Case: {case_id}\n"
            f"Order Data: {json.dumps(order_summary, ensure_ascii=False)}\n"
            f"Shipment Analysis: {shipment_verdict}\n"
            f"Payment Analysis: {payment_verdict}\n\n"
            "Respond ONLY with a JSON object containing:\n"
            "{\n"
            '  "primary_issue": string,\n'
            '  "responsible_party": "seller"|"platform"|"logistics_provider"'
            '|"payment_provider"|"customer"|"unknown",\n'
            '  "confidence": number between 0 and 1,\n'
            '  "rationale": string\n'
            "}"
        )

        try:
            async with httpx2.AsyncClient(timeout=self.timeout) as client:
                payload = {
                    "model": self.model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a concise e-commerce dispute investigator.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 500,
                }
                res = await client.post(
                    f"{self.api_base}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                if res.status_code != 200:
                    return None
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                # Extract JSON if enclosed in markdown code fences
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                return json.loads(content)
        except Exception:
            # Safe fallback: never crash execution if LLM fails
            return None
