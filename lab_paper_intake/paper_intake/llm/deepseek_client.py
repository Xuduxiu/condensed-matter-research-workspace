from __future__ import annotations

import json
import re
from typing import Any


from .base import LLMError


class DeepSeekClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValueError("DeepSeek API key is required")
        if not model_name:
            raise ValueError("DeepSeek model name is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.timeout_seconds = timeout_seconds

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_or_instruction: str,
    ) -> dict[str, Any]:
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - dependency setup issue
            raise LLMError('httpx is required for DeepSeek requests') from exc

        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        system_prompt
                        + "\n\nReturn only valid JSON. "
                        + schema_or_instruction
                    ),
                },
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            raise LLMError(f"DeepSeek request failed: {exc}") from exc

        try:
            content = response.json()["choices"][0]["message"]["content"]
            return _loads_json_content(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise LLMError("DeepSeek returned invalid JSON content") from exc


def _loads_json_content(content: str) -> dict[str, Any]:
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    parsed = json.loads(cleaned)
    if not isinstance(parsed, dict):
        raise json.JSONDecodeError("Expected JSON object", cleaned, 0)
    return parsed
