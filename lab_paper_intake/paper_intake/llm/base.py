from __future__ import annotations

from typing import Any, Protocol


class LLMError(RuntimeError):
    """Raised when an LLM provider cannot return valid structured output."""


class LLMClient(Protocol):
    model_name: str

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema_or_instruction: str,
    ) -> dict[str, Any]:
        """Return parsed JSON from a provider-specific chat completion."""
