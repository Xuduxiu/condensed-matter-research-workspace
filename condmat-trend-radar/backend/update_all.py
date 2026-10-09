from __future__ import annotations

import argparse
import json
import random
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from backend.analytics.cooccurrence import cooccurrence_network
from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.stats import rebuild_paper_terms, rebuild_term_month_stats, shift_month
from backend.db.database import connect, init_db, normalized_title_key, processed_dir, upsert_paper, utc_now
from backend.ingest.arxiv_client import ArxivClient
from backend.ingest.manual_import import load_manual_file
from backend.ingest.openalex_client import OpenAlexClient
from backend.nlp.condmat_filter import is_condensed_matter_record
from backend.nlp.dictionaries import CONTEXT_JOURNALS, CORE_JOURNALS


def run_update(args: argparse.Namespace) -> dict[str, Any]:
    started_at = utc_now()
    windows = resolve_windows(args)
    manual_files = [Path(item) for item in getattr(args, "manual", []) or []]
    requested_live = not args.skip_network and not args.mock
    papers: list[dict[str, Any]] = []
    failed_requests = 0
    used_mock = False

    if requested_live:
        journals = journals_for_scope(args.scope)
        client = OpenAlexClient(timeout=args.timeout)
        live_papers, failures = client.fetch_many_journals(
            from_date=windows["baseline_from"],
            to_date=windows["baseline_to"],
            journals=journals,
            per_page=args.max_results_per_source,
            max_pages=args.max_pages,
        )
        papers.extend(live_papers)
        failed_requests += failures

        if args.scope == "all" or args.include_arxiv:
            arxiv_papers, failures = ArxivClient(timeout=args.timeout).fetch(
                windows["baseline_from"],
                windows["baseline_to"],
                max_results=args.max_results_per_source,
            )
            papers.extend(arxiv_papers)
            failed_requests += failures

    for path in manual_files:
        papers.extend(load_manual_file(path))

    filtered = []
    for paper in papers:
        keep, reasons = is_condensed_matter_record(paper)
        if keep:
            paper["filter_reasons"] = reasons
            filtered.append(paper)

    if args.mock or (args.mock_if_empty and not filtered):
        filtered.extend(generate_mock_papers(windows["baseline_from"], windows["baseline_to"]))
        used_mock = True

    inserted = 0
    updated = 0
    duplicates = 0
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    with connect() as conn:
        init_db(conn)
        if args.force_refresh:
            clear_corpus(conn)
        run_id = start_update_run(conn, started_at, windows, args.scope, requested_live)
        for paper in filtered:
            doi = (paper.get("doi") or "").lower()
            title_key = normalized_title_key(paper.get("title") or "")
            if doi and doi in seen_doi:
                duplicates += 1
                continue
            if not doi and title_key and title_key in seen_title:
                duplicates += 1
                continue
            if doi:
                seen_doi.add(doi)
            if title_key:
                seen_title.add(title_key)
            status = upsert_paper(conn, paper)
            if status == "inserted":
                inserted += 1
            else:
                updated += 1
        extracted = rebuild_paper_terms(conn)
        stats_rows = rebuild_term_month_stats(conn)
        lifecycle_rows = rebuild_lifecycle(
            conn,
            windows["baseline_from"],
            windows["baseline_to"],
            windows["trend_from"],
            windows["trend_to"],
        )
        _ = cooccurrence_network(conn, min_count=2)
        finish_update_run(
            conn,
            run_id,
            inserted=inserted,
            updated=updated,
            duplicates=duplicates,
            failed_requests=failed_requests,
            used_mock=used_mock,
            message=f"extracted={extracted}; stats_rows={stats_rows}; lifecycle_rows={lifecycle_rows}",
        )

    result = {
        "inserted_papers": inserted,
        "updated_papers": updated,
        "duplicate_papers": duplicates,
        "failed_requests": failed_requests,
        "used_mock_data": used_mock,
        "baseline_from": windows["baseline_from"],
        "baseline_to": windows["baseline_to"],
        "trend_from": windows["trend_from"],
        "trend_to": windows["trend_to"],
        "scope": args.scope,
        "started_at": started_at,
        "finished_at": utc_now(),
    }
    write_update_log(result)
    return result


def resolve_windows(args: argparse.Namespace) -> dict[str, str | int]:
    today = date.today().isoformat()
    baseline_from = args.baseline_from or args.from_date or "2015-01-01"
    baseline_to = args.baseline_to or args.to_date or today
    trend_to = args.trend_to or baseline_to
    if args.trend_from:
        trend_from = args.trend_from
        trend_months = args.trend_months or month_distance(trend_from[:7], trend_to[:7]) + 1
    else:
        trend_months = args.trend_months or 24
        trend_from = f"{shift_month(trend_to[:7], -(trend_months - 1))}-01"
    return {
        "baseline_from": baseline_from,
        "baseline_to": baseline_to,
        "trend_from": trend_from,
        "trend_to": trend_to,
        "trend_months": trend_months,
    }


def month_distance(start_month: str, end_month: str) -> int:
    sy, sm = [int(part) for part in start_month.split("-")]
    ey, em = [int(part) for part in end_month.split("-")]
    return (ey - sy) * 12 + (em - sm)


def journals_for_scope(scope: str) -> list[str]:
    if scope == "core":
        return list(CORE_JOURNALS)
    return list(CORE_JOURNALS + CONTEXT_JOURNALS)


def clear_corpus(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM paper_terms")
    conn.execute("DELETE FROM papers")
    conn.execute("DELETE FROM term_month_stats")
    conn.execute("DELETE FROM monthly_corpus_stats")
    conn.execute("DELETE FROM concept_lifecycle")
    conn.execute("DELETE FROM update_runs")


def start_update_run(conn: sqlite3.Connection, started_at: str, windows: dict[str, str | int], scope: str, requested_live: bool) -> int:
    cursor = conn.execute(
        """
        INSERT INTO update_runs
          (started_at, from_date, to_date, baseline_from, baseline_to, trend_from, trend_to, trend_months, scope, requested_live)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            started_at,
            windows["baseline_from"],
            windows["baseline_to"],
            windows["baseline_from"],
            windows["baseline_to"],
            windows["trend_from"],
            windows["trend_to"],
            windows["trend_months"],
            scope,
            1 if requested_live else 0,
        ),
    )
    return int(cursor.lastrowid)


def finish_update_run(
    conn: sqlite3.Connection,
    run_id: int,
    inserted: int,
    updated: int,
    duplicates: int,
    failed_requests: int,
    used_mock: bool,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE update_runs
        SET finished_at = ?, inserted_papers = ?, updated_papers = ?, duplicate_papers = ?,
            failed_requests = ?, used_mock_data = ?, message = ?
        WHERE id = ?
        """,
        (
            utc_now(),
            inserted,
            updated,
            duplicates,
            failed_requests,
            1 if used_mock else 0,
            message,
            run_id,
        ),
    )


def write_update_log(result: dict[str, Any]) -> None:
    log_dir = processed_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (log_dir / f"update_{stamp}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def generate_mock_papers(from_date: str, to_date: str) -> list[dict[str, Any]]:
    rng = random.Random(20260710)
    months = months_between(from_date[:7], to_date[:7])
    papers: list[dict[str, Any]] = []
    counter = 0

    core_topics = [
        ("quantum Hall effect", "Landau level", "graphene", "magnetotransport"),
        ("topological insulator", "Berry curvature", "MnBi2Te4", "ARPES"),
        ("charge density wave", "superconductivity", "NbSe2", "STM"),
        ("exciton", "valley polarization", "WSe2", "photoluminescence"),
    ]
    context_topics = [
        ("van der Waals heterostructure", "encapsulation", "hBN", "transport"),
        ("flat band", "moiré superlattice", "twisted bilayer graphene", "transport"),
        ("spin liquid", "Kitaev material", "RuCl3", "neutron scattering"),
        ("Weyl semimetal", "quantum oscillation", "Cd3As2", "transport"),
    ]

    def add(month: str, concept_a: str, concept_b: str, material: str, method: str, journal: str, cited: int, profile: str, source: str = "mock") -> None:
        nonlocal counter
        counter += 1
        day = 1 + rng.randrange(27)
        title_templates = [
            f"{concept_a} in {material}",
            f"{material} evidence for {concept_a}",
            f"{method} study of {concept_a} and {concept_b} in {material}",
            f"Tunable {concept_a} signatures in {material}",
        ]
        title = rng.choice(title_templates)
        abstract = (
            f"We report {concept_a} and {concept_b} in {material}. "
            f"The study uses {method} and compares monthly condensed matter metadata signals without implying real trends. "
            f"The record is deterministic mock data for UI validation."
        )
        if material == "hBN":
            abstract += " Hexagonal boron nitride (h-BN) encapsulation provides a long-lived platform for graphene and TMD devices."
        papers.append(
            {
                "id": f"mock:{month}-{counter:06d}",
                "doi": f"10.0000/condmat-radar.{month.replace('-', '.')}.{counter:06d}",
                "title": title,
                "abstract": abstract,
                "publication_date": f"{month}-{day:02d}",
                "journal": journal,
                "source": source,
                "url": f"https://example.org/condmat-radar/{counter}",
                "cited_by_count": cited,
                "concepts": [concept_a, concept_b],
                "raw_json": {"mock": True, "profile": profile},
            }
        )

    def repeat(month: str, n: int, topic: tuple[str, str, str, str], journals: list[str], cited_base: int, profile: str) -> None:
        for _ in range(max(0, n)):
            concept_a, concept_b, material, method = topic
            add(month, concept_a, concept_b, material, method, rng.choice(journals), cited_base + rng.randrange(70), profile)

    for idx, month in enumerate(months):
        year = int(month[:4])
        months_from_start = idx
        core_journals = CORE_JOURNALS
        context_journals = CONTEXT_JOURNALS

        repeat(month, rng.randint(4, 7), ("van der Waals heterostructure", "encapsulation", "hBN", "transport"), context_journals, 70, "stable_hbn")
        repeat(month, rng.randint(3, 6), ("quantum Hall effect", "Landau level", "graphene", "magnetotransport"), core_journals, 80, "stable_graphene")
        repeat(month, rng.randint(2, 5), rng.choice(core_topics), core_journals, 45, "core_background")
        repeat(month, rng.randint(2, 5), ("exciton", "van der Waals", rng.choice(["WSe2", "MoS2", "MoTe2"]), "photoluminescence"), context_journals, 25, "tmd_background")

        if year >= 2018:
            ramp = min(6, 1 + (months_from_start - 36) // 18)
            repeat(month, rng.randint(1, max(2, ramp)), ("moiré superlattice", "flat band", "twisted bilayer graphene", "transport"), core_journals, 40, "moire_ramp")
        if year >= 2023:
            repeat(month, rng.randint(3, 8), ("fractional Chern insulator", "Chern insulator", "MoTe2", "magnetotransport"), core_journals, 18, "fci_burst")
        if year >= 2022:
            repeat(month, rng.randint(1, 4), ("altermagnetism", "Berry curvature", "MnBi2Te4", "DFT"), context_journals, 12, "altermagnetism_rise")

        if rng.random() < 0.18:
            repeat(month, rng.randint(1, 4), ("Weyl semimetal", "quantum oscillation", "ZrTe5", "transport"), core_journals, 20, "zrte5_intermittent")
        if rng.random() < 0.10:
            repeat(month, rng.randint(1, 2), ("Dirac semimetal", "quantum oscillation", "HfTe5", "transport"), context_journals, 12, "hfte5_low_frequency")
        if rng.random() < 0.35:
            repeat(month, rng.randint(1, 3), rng.choice(context_topics), context_journals, 18, "context_noise")

    return papers

def months_between(start_month: str, end_month: str) -> list[str]:
    year, month = [int(part) for part in start_month.split("-")]
    end_year, end_mon = [int(part) for part in end_month.split("-")]
    months: list[str] = []
    while (year, month) <= (end_year, end_mon):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return months


def parse_args() -> argparse.Namespace:
    today = date.today().isoformat()
    parser = argparse.ArgumentParser(description="Update condensed matter trend radar data.")
    parser.add_argument("--from", dest="from_date", default=None, help="Legacy alias for --baseline-from.")
    parser.add_argument("--to", dest="to_date", default=None, help="Legacy alias for --baseline-to / --trend-to.")
    parser.add_argument("--baseline-from", default="2015-01-01")
    parser.add_argument("--baseline-to", default=today)
    parser.add_argument("--trend-from", default=None)
    parser.add_argument("--trend-to", default=None)
    parser.add_argument("--trend-months", type=int, default=24)
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--max-results-per-source", type=int, default=120)
    parser.add_argument("--force-refresh", action="store_true")
    parser.add_argument("--skip-network", action="store_true", help="Do not call external APIs.")
    parser.add_argument("--mock", action="store_true", help="Use deterministic offline historical mock data.")
    parser.add_argument("--mock-if-empty", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--include-arxiv", action="store_true")
    parser.add_argument("--manual", action="append", default=[])
    parser.add_argument("--per-page", type=int, default=None, help="Legacy alias for --max-results-per-source.")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--timeout", type=int, default=12)
    args = parser.parse_args()
    if args.per_page:
        args.max_results_per_source = args.per_page
    return args


def main() -> None:
    result = run_update(parse_args())
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
