from __future__ import annotations

from dataclasses import dataclass, field

from .config import Settings
from .db import record_search_run, upsert_papers
from .dedup import deduplicate_papers
from .models import Paper, SearchPlan
from .pdf_downloader import download_pdfs
from .pdf_resolver import resolve_pdfs
from .query_planner import plan_queries
from .search import search_arxiv, search_crossref, search_openalex
from .summarizer import summarize_papers


@dataclass
class IntakeResult:
    plan: SearchPlan
    papers: list[Paper]
    errors: list[str] = field(default_factory=list)


def run_intake(
    prompt: str,
    settings: Settings,
    max_papers: int = 25,
    year_from: int | None = None,
    year_to: int | None = None,
    enable_llm_summaries: bool = True,
    download_oa_pdfs: bool = False,
) -> IntakeResult:
    plan = plan_queries(prompt, settings, year_from=year_from, year_to=year_to)
    errors: list[str] = []
    raw_papers: list[Paper] = []
    queries = plan.queries[:6] or [prompt]
    per_backend = max(5, min(25, max_papers // max(1, len(queries)) + 3))

    for query in queries:
        try:
            raw_papers.extend(
                search_openalex(
                    query,
                    settings,
                    max_results=per_backend,
                    year_from=plan.year_from,
                    year_to=plan.year_to,
                )
            )
        except Exception as exc:
            errors.append(f"OpenAlex failed for '{query}': {exc}")

        try:
            raw_papers.extend(
                search_arxiv(
                    query,
                    settings,
                    max_results=per_backend,
                    year_from=plan.year_from,
                    year_to=plan.year_to,
                )
            )
        except Exception as exc:
            errors.append(f"arXiv failed for '{query}': {exc}")

    if len(raw_papers) < max(5, max_papers // 3):
        try:
            raw_papers.extend(
                search_crossref(
                    queries[0],
                    settings,
                    max_results=10,
                    year_from=plan.year_from,
                    year_to=plan.year_to,
                )
            )
        except Exception as exc:
            errors.append(f"Crossref fallback failed: {exc}")

    papers = deduplicate_papers(raw_papers)
    papers = papers[:max_papers]
    papers = resolve_pdfs(papers, settings)
    papers = summarize_papers(
        papers,
        prompt,
        settings,
        enabled=enable_llm_summaries,
    )
    papers = sorted(
        papers,
        key=lambda paper: (paper.relevance_score or 0.0, paper.year or 0),
        reverse=True,
    )

    if download_oa_pdfs:
        papers = download_pdfs(papers, settings)

    upsert_papers(papers)
    record_search_run(prompt, plan.model_dump(mode="json"), len(papers))
    return IntakeResult(plan=plan, papers=papers, errors=errors)
