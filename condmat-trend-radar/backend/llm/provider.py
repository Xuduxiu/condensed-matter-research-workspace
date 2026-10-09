from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from backend.config import deepseek_api_key, deepseek_base_url, deepseek_model_fast, deepseek_model_pro, mask_secret


@dataclass(frozen=True)
class DeepSeekProvider:
    model: str
    base_url: str
    api_key_masked: str
    dry_run: bool = True

    @classmethod
    def from_config(cls, tier: str = "fast") -> "DeepSeekProvider":
        key = deepseek_api_key()
        model = deepseek_model_pro() if tier == "pro" else deepseek_model_fast()
        return cls(model=model, base_url=deepseek_base_url(), api_key_masked=mask_secret(key), dry_run=not bool(key))

    def cache_key(self, payload: dict[str, Any]) -> str:
        text = json.dumps({"model": self.model, "payload": payload}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def build_dry_run_response(self, job_type: str, evidence: dict[str, Any]) -> dict[str, Any]:
        return {
            "provider": "deepseek",
            "model": self.model,
            "dry_run": self.dry_run,
            "api_key_masked": self.api_key_masked,
            "job_type": job_type,
            "evidence_bound": True,
            "evidence_item_count": len(evidence.get("key_papers") or []),
            "result": {
                "summary": "LLM call is disabled until DeepSeek key and explicit execution are configured.",
                "allowed_inputs": ["local metadata", "local paper_terms", "local evidence_items"],
                "blocked_inputs": ["unstored PDF text", "external claims without local paper evidence"],
            },
        }