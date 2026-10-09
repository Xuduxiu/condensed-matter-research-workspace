from __future__ import annotations

import argparse
import json

from backend.analytics.lifecycle import rebuild_lifecycle
from backend.analytics.published_preprint import rebuild_published_preprint_stats
from backend.analytics.stats import latest_windows, rebuild_paper_terms, rebuild_preset_term_month_stats
from backend.db.database import connect, init_db
from backend.db.strict_condmat import VALID_CORPUS_PRESETS, normalize_corpus_preset, refresh_strict_condmat_flags, stats_mode_for_preset, strict_counts


def run(corpus_preset: str = "strict_core_published", rebuild_terms: bool = False) -> dict:
    preset = normalize_corpus_preset(corpus_preset)
    with connect() as conn:
        init_db(conn)
        extracted = rebuild_paper_terms(conn) if rebuild_terms else 0
        strict_refresh = refresh_strict_condmat_flags(conn) if preset in {"strict_condmat", "strict_core_published", "strict_context_published", "strict_core_plus_context_published", "arxiv_preprint", "published_vs_preprint"} else None
        published_preprint_rows = 0
        if preset == "published_vs_preprint":
            published_rows = rebuild_preset_term_month_stats(conn, "strict_core_published", refresh_flags=False)
            preprint_rows = rebuild_preset_term_month_stats(conn, "arxiv_preprint", refresh_flags=False)
            stats_rows = published_rows + preprint_rows
            published_preprint_rows = rebuild_published_preprint_stats(conn)
            lifecycle_mode = stats_mode_for_preset("strict_core_published")
        else:
            stats_rows = rebuild_preset_term_month_stats(conn, preset, refresh_flags=False)
            lifecycle_mode = stats_mode_for_preset(preset)
        windows = latest_windows(conn)
        lifecycle_rows = rebuild_lifecycle(
            conn,
            str(windows["baseline_from"]),
            str(windows["baseline_to"]),
            str(windows["trend_from"]),
            str(windows["trend_to"]),
            data_mode=lifecycle_mode,
        )
        counts = strict_counts(conn)
    return {
        "corpus_preset": preset,
        "stats_data_mode": stats_mode_for_preset(preset),
        "lifecycle_data_mode": lifecycle_mode,
        "rebuild_terms": extracted,
        "strict_refresh": strict_refresh,
        "term_month_stats_rows": stats_rows,
        "published_preprint_rows": published_preprint_rows,
        "lifecycle_rows": lifecycle_rows,
        "counts": counts,
    }


def main() -> dict:
    parser = argparse.ArgumentParser(description="Recompute term/month stats for a corpus preset.")
    parser.add_argument("--corpus-preset", default="strict_core_published", choices=list(VALID_CORPUS_PRESETS))
    parser.add_argument("--rebuild-terms", action="store_true", help="Re-extract paper_terms before computing stats.")
    args = parser.parse_args()
    result = run(args.corpus_preset, rebuild_terms=args.rebuild_terms)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()