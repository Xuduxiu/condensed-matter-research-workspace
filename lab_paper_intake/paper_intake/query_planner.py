from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import DB_PATH, Settings
from .db import get_llm_cache, make_cache_key, prompt_hash, set_llm_cache
from .llm import DeepSeekClient, LLMClient, LLMError
from .llm.schemas import QUERY_PLAN_SCHEMA
from .models import SearchPlan


STOPWORDS = {
    "about",
    "and",
    "are",
    "device",
    "devices",
    "find",
    "for",
    "in",
    "latest",
    "new",
    "of",
    "on",
    "paper",
    "papers",
    "recent",
    "research",
    "study",
    "the",
    "to",
    "with",
}


def build_llm_client(settings: Settings) -> LLMClient | None:
    if not settings.llm_available:
        return None
    return DeepSeekClient(
        api_key=settings.deepseek_api_key or "",
        base_url=settings.deepseek_base_url,
        model_name=settings.deepseek_model,
        timeout_seconds=settings.request_timeout_seconds,
    )


def plan_queries(
    prompt: str,
    settings: Settings,
    year_from: int | None = None,
    year_to: int | None = None,
    db_path: Path = DB_PATH,
    llm_client: LLMClient | None = None,
) -> SearchPlan:
    current_year = datetime.now().year
    default_year_from = year_from or current_year - 5
    default_year_to = year_to or current_year

    client = llm_client if llm_client is not None else build_llm_client(settings)
    if client is not None:
        try:
            return _plan_with_llm(
                prompt=prompt,
                client=client,
                year_from=default_year_from,
                year_to=default_year_to,
                db_path=db_path,
            )
        except LLMError:
            pass

    return fallback_plan(prompt, default_year_from, default_year_to)


def _plan_with_llm(
    prompt: str,
    client: LLMClient,
    year_from: int,
    year_to: int,
    db_path: Path,
) -> SearchPlan:
    system_prompt = (
        "You convert a laboratory literature-intake request into conservative, "
        "realistic academic metadata search queries. Do not invent paper facts. "
        "Write the search query strings in English for OpenAlex and arXiv."
    )
    user_prompt = (
        f"Research prompt: {prompt}\n"
        f"Preferred year range: {year_from}-{year_to}\n"
        "Return 3 to 6 concise English search queries suitable for OpenAlex and arXiv."
    )
    payload: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "schema": QUERY_PLAN_SCHEMA,
    }
    cache_key = make_cache_key("query_plan", client.model_name, payload)
    cached = get_llm_cache(cache_key, db_path)
    if cached is not None:
        return _coerce_plan(cached, prompt, year_from, year_to)

    response = client.complete_json(system_prompt, user_prompt, QUERY_PLAN_SCHEMA)
    set_llm_cache(
        cache_key=cache_key,
        model_name=client.model_name,
        payload_hash=prompt_hash(payload),
        response_json=response,
        db_path=db_path,
    )
    return _coerce_plan(response, prompt, year_from, year_to)


def _coerce_plan(
    data: dict[str, Any],
    prompt: str,
    year_from: int,
    year_to: int,
) -> SearchPlan:
    plan = SearchPlan.model_validate(data)
    if not plan.queries:
        plan = fallback_plan(prompt, year_from, year_to)
    plan.year_from = plan.year_from or year_from
    plan.year_to = plan.year_to or year_to
    return plan


def fallback_plan(prompt: str, year_from: int | None, year_to: int | None) -> SearchPlan:
    phrases = _extract_phrases(prompt)
    keywords = _extract_keywords(prompt)
    queries: list[str] = []

    if prompt.strip():
        queries.append(prompt.strip())
    for phrase in phrases:
        if phrase and phrase not in queries:
            queries.append(phrase)
    if keywords:
        joined = " ".join(keywords[:8])
        if joined not in queries:
            queries.append(joined)
    if len(keywords) >= 4:
        queries.append(" ".join(keywords[:4]))

    deduped = list(dict.fromkeys(q for q in queries if q.strip()))
    return SearchPlan(
        main_topics=keywords[:8],
        queries=deduped[:3] or [prompt.strip()],
        year_from=year_from,
        year_to=year_to,
        exclude_terms=[],
    )


def _extract_phrases(prompt: str) -> list[str]:
    pieces = re.split(r"[,;]|\band\b", prompt, flags=re.I)
    return [re.sub(r"\s+", " ", piece).strip(" .") for piece in pieces if piece.strip()]


def _extract_keywords(prompt: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", prompt)
    lowered = [token.lower() for token in tokens]
    return list(dict.fromkeys(token for token in lowered if token not in STOPWORDS))
