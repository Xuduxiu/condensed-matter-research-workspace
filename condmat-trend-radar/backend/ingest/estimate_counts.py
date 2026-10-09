from __future__ import annotations

import argparse
import csv
import json
from datetime import date
from typing import Any

from backend.config import logs_dir
from backend.db.database import export_dir
from backend.ingest.openalex_client import OpenAlexClient, OpenAlexHTTPError
from backend.nlp.dictionaries import CONTEXT_JOURNALS, CORE_JOURNALS


def journals_for_scope(scope: str) -> list[str]:
    if scope == "core":
        return CORE_JOURNALS
    if scope == "core_context":
        return CORE_JOURNALS + CONTEXT_JOURNALS
    return CORE_JOURNALS + CONTEXT_JOURNALS + ["arXiv cond-mat.*"]


def estimate_openalex_journal(client: OpenAlexClient, journal: str, from_date: str, to_date: str) -> tuple[int | None, str]:
    try:
        return client.count_works(journal, from_date, to_date), "ok"
    except OpenAlexHTTPError as exc:
        return None, f"failed:HTTP {exc.status}; retries={exc.retry_count}; log_dir={logs_dir()}"
    except Exception as exc:  # network access is optional for local-first use.
        return None, f"failed:{type(exc).__name__}: {exc}"


def run(args: argparse.Namespace) -> dict[str, Any]:
    client = OpenAlexClient(timeout=args.timeout)
    rows = []
    total = 0
    for journal in journals_for_scope(args.scope):
        if journal.startswith("arXiv"):
            count = None
            status = "preprint_count_not_queried"
        else:
            count, status = estimate_openalex_journal(client, journal, args.baseline_from, args.baseline_to)
        estimated_count = int(count or 0)
        total += estimated_count
        condmat_estimate = int(estimated_count * 0.75) if estimated_count else 0
        rows.append(
            {
                "scope": args.scope,
                "journal": journal,
                "estimated_works_count": estimated_count,
                "estimated_condmat_filtered_count": condmat_estimate,
                "metadata_storage_mb": round(estimated_count * 0.006, 2),
                "pdf_storage_2mb_gb": round(estimated_count * 2 / 1024, 2),
                "pdf_storage_5mb_gb": round(estimated_count * 5 / 1024, 2),
                "pdf_storage_10mb_gb": round(estimated_count * 10 / 1024, 2),
                "status": status,
            }
        )
    result = {
        "baseline_from": args.baseline_from,
        "baseline_to": args.baseline_to,
        "scope": args.scope,
        "estimated_total_works": total,
        "estimated_condmat_filtered_total": sum(row["estimated_condmat_filtered_count"] for row in rows),
        "rows": rows,
    }
    output_dir = export_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "corpus_count_estimate.csv"
    json_path = output_dir / "corpus_count_estimate.json"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate corpus size without downloading full metadata.")
    parser.add_argument("--baseline-from", default="2015-01-01")
    parser.add_argument("--baseline-to", default=date.today().isoformat())
    parser.add_argument("--scope", choices=["core", "core_context", "all"], default="core")
    parser.add_argument("--timeout", type=int, default=12)
    return parser.parse_args()


def main() -> None:
    print(json.dumps(run(parse_args()), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
