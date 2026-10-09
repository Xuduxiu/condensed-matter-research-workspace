from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .config import DB_PATH, Settings
from .db import get_llm_cache, make_cache_key, prompt_hash, set_llm_cache
from .llm import LLMClient, LLMError
from .llm.schemas import SUMMARY_SCHEMA
from .models import ChineseSummary, Paper, SummaryResult
from .query_planner import build_llm_client


def summarize_papers(
    papers: list[Paper],
    research_prompt: str,
    settings: Settings,
    enabled: bool = True,
    db_path: Path = DB_PATH,
    llm_client: LLMClient | None = None,
) -> list[Paper]:
    client = llm_client if llm_client is not None else build_llm_client(settings)
    summarized: list[Paper] = []
    for paper in papers:
        if enabled and client is not None:
            try:
                summarized.append(
                    summarize_with_llm(paper, research_prompt, client, db_path)
                )
                continue
            except LLMError:
                pass
        summarized.append(summarize_without_llm(paper, research_prompt))
    return summarized


def summarize_with_llm(
    paper: Paper,
    research_prompt: str,
    client: LLMClient,
    db_path: Path = DB_PATH,
) -> Paper:
    system_prompt = (
        "You score and summarize real paper metadata for a physics/materials lab. "
        "Use only the provided title, abstract, and metadata. Do not invent facts. "
        "Write chinese_summary fields in Chinese."
    )
    user_prompt = (
        f"Research prompt:\n{research_prompt}\n\n"
        f"Title: {paper.title}\n"
        f"Authors: {', '.join(paper.authors)}\n"
        f"Year: {paper.year}\n"
        f"Journal: {paper.journal}\n"
        f"DOI: {paper.doi}\n"
        f"arXiv: {paper.arxiv_id}\n"
        f"Abstract: {paper.abstract or 'No abstract available.'}\n"
    )
    payload: dict[str, Any] = {
        "system_prompt": system_prompt,
        "user_prompt": user_prompt,
        "schema": SUMMARY_SCHEMA,
    }
    cache_key = make_cache_key("summary", client.model_name, payload)
    cached = get_llm_cache(cache_key, db_path)
    if cached is None:
        cached = client.complete_json(system_prompt, user_prompt, SUMMARY_SCHEMA)
        set_llm_cache(
            cache_key=cache_key,
            model_name=client.model_name,
            payload_hash=prompt_hash(payload),
            response_json=cached,
            db_path=db_path,
        )

    result = SummaryResult.model_validate(cached)
    return _apply_summary(paper, result)


def summarize_without_llm(paper: Paper, research_prompt: str) -> Paper:
    prompt_terms = _terms(research_prompt)
    text_terms = _terms(" ".join([paper.title, paper.abstract or "", paper.journal or ""]))
    overlap = prompt_terms & text_terms
    base = 2.0 + min(len(overlap) * 1.2, 6.0)
    if paper.pdf_url:
        base += 0.8
    if paper.abstract:
        base += 0.8
    score = max(0.0, min(10.0, round(base, 1)))
    priority = "High" if score >= 7 else "Medium" if score >= 4 else "Low"

    tags = sorted(list(overlap))[:8]
    if paper.arxiv_id and "arxiv" not in tags:
        tags.append("arxiv")
    if paper.pdf_url and "open-access" not in tags:
        tags.append("open-access")

    summary = ChineseSummary(
        problem="No LLM summary available; review title and abstract.",
        method="Keyword-overlap fallback scoring was used.",
        key_results="Not extracted in no-LLM mode.",
        relation_to_lab=(
            "Potentially relevant because it shares terms with the research prompt."
            if overlap
            else "Low-confidence match from metadata search."
        ),
        reading_priority=priority,
    )
    return _apply_summary(
        paper,
        SummaryResult(
            relevance_score=score,
            relevance_reason=(
                f"Matched {len(overlap)} prompt terms: {', '.join(sorted(overlap)[:8])}"
                if overlap
                else "No LLM available; score is based on sparse metadata."
            ),
            tags=tags,
            chinese_summary=summary,
        ),
    )


def _apply_summary(paper: Paper, result: SummaryResult) -> Paper:
    data = paper.model_dump(mode="python")
    data["relevance_score"] = float(
        max(0.0, min(10.0, result.relevance_score if math.isfinite(result.relevance_score) else 0.0))
    )
    data["relevance_reason"] = result.relevance_reason
    data["tags"] = result.tags
    data["chinese_summary"] = result.chinese_summary
    return Paper(**data)


def _terms(text: str) -> set[str]:
    stop = {
        "about",
        "after",
        "also",
        "and",
        "are",
        "can",
        "for",
        "from",
        "into",
        "paper",
        "papers",
        "recent",
        "that",
        "the",
        "this",
        "with",
    }
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", text)
        if token.lower() not in stop
    }
