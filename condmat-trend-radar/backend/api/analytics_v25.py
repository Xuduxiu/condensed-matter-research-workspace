from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any

from backend.nlp.dictionaries import CONCEPT_CLASS_OVERRIDES, GENERAL_FIELD_TERMS
from backend.nlp.term_filters import is_display_eligible


LEGACY_SOURCE_NAMES = {"", "radar_legacy", "condmat-trend-radar"}


def _percentage(numerator: int | float, denominator: int | float) -> float:
    if denominator <= 0:
        return 0.0
    return round(float(numerator) * 100.0 / float(denominator), 1)


def _effective_source(value: Any, fallback: Any = None) -> str:
    source = str(value or "").strip().casefold().replace(" ", "_")
    fallback_source = str(fallback or "").strip().casefold().replace(" ", "_")
    if source in LEGACY_SOURCE_NAMES:
        source = fallback_source
    if "semantic" in source and "scholar" in source:
        return "semantic_scholar"
    if "openalex" in source:
        return "openalex"
    if "crossref" in source:
        return "crossref"
    if "arxiv" in source:
        return "arxiv"
    return source or "unknown"


def _prepare_eligible_canonical_papers(connection: sqlite3.Connection) -> None:
    """Freeze the canonical paper scope used by every paper-level v2.5 query.

    Review candidates and low-confidence/non-condensed-matter records remain in
    the durable tables, but cannot enter an analytics query merely because a
    related version, topic, material, monitor hit, or download task exists.
    """
    connection.execute("DROP TABLE IF EXISTS temp.analytics_eligible_canonical_papers")
    connection.execute(
        """
        CREATE TEMP TABLE analytics_eligible_canonical_papers AS
        SELECT p.id AS canonical_paper_id
        FROM papers p
        WHERE p.data_mode='real'
          AND COALESCE(p.condmat_view_eligible, 0)=1
        GROUP BY p.id
        """
    )
    connection.execute(
        "CREATE UNIQUE INDEX temp.idx_analytics_eligible_canonical "
        "ON analytics_eligible_canonical_papers(canonical_paper_id)"
    )


def _latest_stable_anchor(connection: sqlite3.Connection, today: date) -> tuple[str, str, bool]:
    row = connection.execute(
        """
        WITH version_dates AS (
          SELECT v.canonical_paper_id, date(COALESCE(
            CASE WHEN v.version_type='preprint' THEN NULLIF(v.submitted_date, '') END,
            NULLIF(v.publication_date, ''), NULLIF(v.submitted_date, ''),
            NULLIF(p.publication_date, '')
          )) AS paper_date
          FROM temp.analytics_eligible_canonical_papers e
          JOIN papers p ON p.id=e.canonical_paper_id
          JOIN paper_versions v ON v.canonical_paper_id=e.canonical_paper_id
        ), canonical_dates AS (
          SELECT canonical_paper_id, MIN(paper_date) AS research_date
          FROM version_dates
          WHERE paper_date IS NOT NULL AND paper_date<=date('now')
          GROUP BY canonical_paper_id
        )
        SELECT
          MAX(research_date) AS latest_observed,
          MAX(CASE WHEN research_date<date('now') THEN research_date END) AS latest_complete
        FROM canonical_dates
        """
    ).fetchone()
    latest_observed = str(row["latest_observed"] or today.isoformat())
    latest_complete = str(row["latest_complete"] or "")
    excluded_partial_today = latest_observed == today.isoformat() and bool(latest_complete)
    return (latest_complete if excluded_partial_today else latest_observed), latest_observed, excluded_partial_today


def _prepare_windows(
    connection: sqlite3.Connection,
    baseline_from: str,
    range_from: str,
    range_to: str,
) -> None:
    for table in ("analytics_scoped_versions", "analytics_canonical_outputs", "analytics_output_sources"):
        connection.execute(f"DROP TABLE IF EXISTS temp.{table}")

    connection.execute(
        """
        CREATE TEMP TABLE analytics_scoped_versions AS
        SELECT
          v.id AS paper_version_id,
          v.canonical_paper_id,
          v.version_type,
          date(COALESCE(
            CASE WHEN v.version_type='preprint' THEN NULLIF(v.submitted_date, '') END,
            NULLIF(v.publication_date, ''), NULLIF(v.submitted_date, ''),
            NULLIF(p.publication_date, '')
          )) AS paper_date,
          CASE
            WHEN date(COALESCE(
              CASE WHEN v.version_type='preprint' THEN NULLIF(v.submitted_date, '') END,
              NULLIF(v.publication_date, ''), NULLIF(v.submitted_date, ''),
              NULLIF(p.publication_date, '')
            ))>=date(?) THEN 'current' ELSE 'baseline'
          END AS window_name,
          CASE
            WHEN lower(trim(COALESCE(v.source, ''))) IN ('', 'radar_legacy', 'condmat-trend-radar')
              THEN COALESCE(NULLIF(lower(trim(p.source)), ''), 'unknown')
            ELSE lower(trim(v.source))
          END AS source_name,
          COALESCE(NULLIF(v.journal, ''), NULLIF(p.journal, ''), 'Unknown') AS journal_name,
          CASE WHEN trim(COALESCE(v.abstract, p.abstract, ''))<>'' THEN 1 ELSE 0 END AS has_abstract,
          CASE WHEN trim(COALESCE(v.doi, p.doi, ''))<>'' THEN 1 ELSE 0 END AS has_doi,
          CASE WHEN p.is_open_access=1 THEN 1 ELSE 0 END AS is_open_access,
          COALESCE(p.cited_by_count, 0) AS cited_by_count
        FROM temp.analytics_eligible_canonical_papers e
        JOIN papers p ON p.id=e.canonical_paper_id
        JOIN paper_versions v ON v.canonical_paper_id=e.canonical_paper_id
        WHERE date(COALESCE(
            CASE WHEN v.version_type='preprint' THEN NULLIF(v.submitted_date, '') END,
            NULLIF(v.publication_date, ''), NULLIF(v.submitted_date, ''),
            NULLIF(p.publication_date, '')
          )) BETWEEN date(?) AND date(?)
        """,
        (range_from, baseline_from, range_to),
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_scoped_paper ON analytics_scoped_versions(canonical_paper_id)"
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_scoped_date ON analytics_scoped_versions(paper_date, window_name)"
    )

    connection.execute(
        """
        CREATE TEMP TABLE analytics_canonical_outputs AS
        WITH version_dates AS (
          SELECT
            v.canonical_paper_id,
            v.version_type,
            date(COALESCE(
              CASE WHEN v.version_type='preprint' THEN NULLIF(v.submitted_date, '') END,
              NULLIF(v.publication_date, ''), NULLIF(v.submitted_date, ''),
              NULLIF(p.publication_date, '')
            )) AS paper_date,
            CASE WHEN trim(COALESCE(v.abstract, p.abstract, ''))<>'' THEN 1 ELSE 0 END AS has_abstract,
            CASE WHEN trim(COALESCE(v.doi, p.doi, ''))<>'' THEN 1 ELSE 0 END AS has_doi,
            CASE WHEN p.is_open_access=1 THEN 1 ELSE 0 END AS is_open_access,
            COALESCE(p.cited_by_count, 0) AS cited_by_count
          FROM temp.analytics_eligible_canonical_papers e
          JOIN papers p ON p.id=e.canonical_paper_id
          JOIN paper_versions v ON v.canonical_paper_id=e.canonical_paper_id
        ), canonical AS (
          SELECT
            canonical_paper_id,
            MIN(paper_date) AS research_date,
            MAX(CASE WHEN version_type='preprint' THEN 1 ELSE 0 END) AS has_preprint,
            MAX(CASE WHEN version_type IN ('publication','published') THEN 1 ELSE 0 END) AS has_publication,
            MAX(has_abstract) AS has_abstract,
            MAX(has_doi) AS has_doi,
            MAX(is_open_access) AS is_open_access,
            MAX(cited_by_count) AS cited_by_count
          FROM version_dates
          WHERE paper_date IS NOT NULL AND paper_date<=date(?)
          GROUP BY canonical_paper_id
        )
        SELECT *,
          CASE WHEN research_date>=date(?) THEN 'current' ELSE 'baseline' END AS window_name
        FROM canonical
        WHERE research_date BETWEEN date(?) AND date(?)
        """,
        (range_to, range_from, baseline_from, range_to),
    )
    connection.execute(
        "CREATE UNIQUE INDEX temp.idx_analytics_output_paper ON analytics_canonical_outputs(canonical_paper_id)"
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_output_window ON analytics_canonical_outputs(window_name, research_date)"
    )
    connection.execute(
        """
        CREATE TEMP TABLE analytics_output_sources AS
        WITH raw_sources AS (
          SELECT o.canonical_paper_id, o.window_name,
            CASE
              WHEN lower(trim(COALESCE(v.source,''))) IN ('', 'radar_legacy', 'condmat-trend-radar')
                THEN COALESCE(NULLIF(lower(trim(p.source)), ''), 'unknown')
              ELSE lower(trim(v.source))
            END AS raw_source
          FROM temp.analytics_canonical_outputs o
          JOIN papers p ON p.id=o.canonical_paper_id
          JOIN paper_versions v ON v.canonical_paper_id=o.canonical_paper_id
          UNION ALL
          SELECT o.canonical_paper_id, o.window_name,
            CASE
              WHEN lower(trim(COALESCE(obs.source_project,''))) IN ('', 'radar_legacy', 'condmat-trend-radar')
                THEN COALESCE(NULLIF(lower(trim(p.source)), ''), 'unknown')
              ELSE lower(trim(obs.source_project))
            END AS raw_source
          FROM temp.analytics_canonical_outputs o
          JOIN papers p ON p.id=o.canonical_paper_id
          JOIN paper_versions v ON v.canonical_paper_id=o.canonical_paper_id
          JOIN paper_version_observations obs ON obs.paper_version_id=v.id
        ), normalized_sources AS (
          SELECT canonical_paper_id, window_name,
            CASE
              WHEN raw_source LIKE '%semantic%' AND raw_source LIKE '%scholar%'
                THEN 'semantic_scholar'
              WHEN raw_source LIKE '%openalex%' THEN 'openalex'
              WHEN raw_source LIKE '%crossref%' THEN 'crossref'
              WHEN raw_source LIKE '%arxiv%' THEN 'arxiv'
              ELSE replace(raw_source, ' ', '_')
            END AS source_name
          FROM raw_sources
        )
        SELECT DISTINCT canonical_paper_id, window_name, source_name
        FROM normalized_sources
        WHERE source_name NOT IN (
          '', 'unknown', 'legacy_intake', 'manual', 'imported', 'repository'
        )
        """
    )
    connection.execute(
        "CREATE INDEX temp.idx_analytics_output_source ON analytics_output_sources(window_name, source_name, canonical_paper_id)"
    )


def _momentum_metrics(
    current_count: int,
    baseline_count: int,
    current_total: int,
    baseline_total: int,
    extraction_coverage: float,
    min_count: int,
) -> dict[str, Any]:
    current_share = current_count / current_total if current_total else 0.0
    baseline_share = baseline_count / baseline_total if baseline_total else 0.0
    current_smoothed = (current_count + 0.5) / (current_total + 1.0) if current_total else 0.5
    baseline_smoothed = (baseline_count + 0.5) / (baseline_total + 1.0) if baseline_total else 0.5
    log_share_ratio = math.log2(max(current_smoothed, 1e-12) / max(baseline_smoothed, 1e-12))
    support = current_count + baseline_count
    support_reliability = min(1.0, math.sqrt(support / 20.0))
    reliability = round(support_reliability * max(0.0, min(1.0, extraction_coverage)), 2)
    momentum = round(100.0 * math.tanh(log_share_ratio / 2.0) * reliability, 1)
    if current_count < min_count or current_total < 5 or baseline_total < 5:
        status = "insufficient_evidence"
    elif momentum >= 15:
        status = "emerging" if baseline_count == 0 else "rising"
    elif momentum <= -15:
        status = "cooling"
    else:
        status = "stable"
    heat_score = round(
        max(0.0, momentum) * 0.55
        + min(100.0, 25.0 * math.log1p(current_count)) * 0.45,
        1,
    )
    return {
        "current_count": current_count,
        "baseline_count": baseline_count,
        "current_share_pct": round(current_share * 100.0, 3),
        "baseline_share_pct": round(baseline_share * 100.0, 3),
        "share_change_pp": round((current_share - baseline_share) * 100.0, 3),
        "growth_ratio": round((current_count + 0.5) / (baseline_count + 0.5), 2),
        "momentum": momentum,
        "relative_momentum_score": momentum,
        "heat_score": heat_score,
        "reliability": reliability,
        "status": status,
    }

_ENTITY_SPECS = {
    "topic": {
        "joins": "JOIN paper_topics r ON r.canonical_paper_id=o.canonical_paper_id JOIN topics e ON e.id=r.topic_id",
        "id": "e.id",
        "name": "e.canonical_name",
        "class": "COALESCE(NULLIF(e.topic_type, ''), 'concept')",
        "where": "r.confidence>=0.5",
    },
    "material": {
        "joins": "JOIN paper_materials r ON r.canonical_paper_id=o.canonical_paper_id JOIN materials e ON e.id=r.material_id",
        "id": "e.id",
        "name": "e.canonical_name",
        "class": "COALESCE(NULLIF(e.material_family, ''), 'unclassified')",
        "where": "r.confidence>=0.6",
    },
    "method": {
        "joins": "JOIN paper_terms r ON r.paper_id=o.canonical_paper_id",
        "id": "r.normalized_term",
        "name": "r.normalized_term",
        "class": "'method'",
        "where": "r.term_type='method' AND r.display_eligible=1 AND r.confidence>=0.6",
    },
}


def _canonical_topic_class(name: str, default: str) -> str:
    key = name.casefold()
    for raw_name, concept_class in CONCEPT_CLASS_OVERRIDES.items():
        if raw_name.casefold() == key:
            return concept_class
    if any(raw_name.casefold() == key for raw_name in GENERAL_FIELD_TERMS):
        return "general_field"
    return default or "physics_concept"


def _entity_trends(
    connection: sqlite3.Connection,
    entity_type: str,
    current_total: int,
    baseline_total: int,
    monthly_totals: dict[str, int],
    limit: int = 20,
) -> tuple[list[dict[str, Any]], float]:
    spec = _ENTITY_SPECS[entity_type]
    rows = connection.execute(
        f"""
        SELECT {spec['id']} AS entity_id, {spec['name']} AS entity_name,
               {spec['class']} AS entity_class, o.window_name,
               COUNT(DISTINCT o.canonical_paper_id) AS paper_count
        FROM temp.analytics_canonical_outputs o
        {spec['joins']}
        WHERE {spec['where']}
        GROUP BY {spec['id']}, {spec['name']}, {spec['class']}, o.window_name
        """
    ).fetchall()
    counts: dict[str, dict[str, Any]] = {}
    for row in rows:
        entity_id = str(row["entity_id"] or "").strip()
        entity_name = str(row["entity_name"] or "").strip()
        if not entity_id or not entity_name:
            continue
        entity_class = str(row["entity_class"] or entity_type)
        if entity_type == "topic":
            entity_class = _canonical_topic_class(entity_name, entity_class)
            if entity_class in {"method", "general_field"}:
                continue
            if not is_display_eligible(entity_name, "concept", 1.0):
                continue
        item = counts.setdefault(
            entity_id,
            {"id": entity_id, "name": entity_name, "class": entity_class},
        )
        item[str(row["window_name"])] = int(row["paper_count"] or 0)

    # Coverage must use the same display/type gate as the trend entities.
    # Otherwise generic field labels or method-like topics can make topic
    # extraction look complete even though every plotted topic was rejected.
    eligible_entity_ids = tuple(counts)
    covered_by_window: dict[str, int] = {}
    if eligible_entity_ids:
        eligible_placeholders = ",".join("?" for _ in eligible_entity_ids)
        covered_by_window = {
            str(row["window_name"]): int(row["paper_count"] or 0)
            for row in connection.execute(
                f"""
                SELECT o.window_name,
                       COUNT(DISTINCT o.canonical_paper_id) AS paper_count
                FROM temp.analytics_canonical_outputs o
                {spec['joins']}
                WHERE {spec['where']}
                  AND CAST({spec['id']} AS TEXT) IN ({eligible_placeholders})
                GROUP BY o.window_name
                """,
                eligible_entity_ids,
            )
        }
    current_coverage = covered_by_window.get("current", 0) / current_total if current_total else 0.0
    baseline_coverage = covered_by_window.get("baseline", 0) / baseline_total if baseline_total else 0.0
    coverage_comparable = (
        current_coverage >= 0.60
        and baseline_coverage >= 0.60
        and abs(current_coverage - baseline_coverage) <= 0.15
    )
    reliability_coverage = min(current_coverage, baseline_coverage)
    min_count = max(2, math.ceil(current_total * 0.002))

    source_totals: dict[str, dict[str, int]] = {}
    for row in connection.execute(
        """
        SELECT source_name, window_name, COUNT(DISTINCT canonical_paper_id) AS paper_count
        FROM temp.analytics_output_sources
        GROUP BY source_name, window_name
        """
    ):
        source = _effective_source(row["source_name"])
        if source in {"unknown", "legacy_intake", "manual", "imported", "repository"}:
            continue
        source_totals.setdefault(source, {})[str(row["window_name"])] = (
            source_totals.setdefault(source, {}).get(str(row["window_name"]), 0)
            + int(row["paper_count"] or 0)
        )
    shared_floor = max(2, min(10, math.ceil(min(current_total, baseline_total) * 0.02)))
    shared_sources = {
        source
        for source, values in source_totals.items()
        if values.get("current", 0) >= shared_floor and values.get("baseline", 0) >= shared_floor
    }
    pooled_total = sum(
        source_totals[source].get("current", 0) + source_totals[source].get("baseline", 0)
        for source in shared_sources
    )
    source_weights = {
        source: (
            source_totals[source].get("current", 0) + source_totals[source].get("baseline", 0)
        ) / pooled_total
        for source in shared_sources
    } if pooled_total else {}

    source_entity_counts: dict[str, dict[str, dict[str, int]]] = {}
    for row in connection.execute(
        f"""
        SELECT {spec['id']} AS entity_id, s.source_name,
               o.window_name, COUNT(DISTINCT o.canonical_paper_id) AS paper_count
        FROM temp.analytics_canonical_outputs o
        JOIN temp.analytics_output_sources s ON s.canonical_paper_id=o.canonical_paper_id AND s.window_name=o.window_name
        {spec['joins']}
        WHERE {spec['where']}
        GROUP BY {spec['id']}, s.source_name, o.window_name
        """
    ):
        entity_id = str(row["entity_id"] or "").strip()
        if entity_id not in counts:
            continue
        source = _effective_source(row["source_name"])
        if source in {"unknown", "legacy_intake", "manual", "imported", "repository"}:
            continue
        window = str(row["window_name"])
        target = source_entity_counts.setdefault(entity_id, {}).setdefault(source, {})
        target[window] = target.get(window, 0) + int(row["paper_count"] or 0)

    monthly_rows = connection.execute(
        f"""
        SELECT {spec['id']} AS entity_id, substr(o.research_date,1,7) AS month,
               COUNT(DISTINCT o.canonical_paper_id) AS paper_count
        FROM temp.analytics_canonical_outputs o
        {spec['joins']}
        WHERE {spec['where']}
        GROUP BY {spec['id']}, substr(o.research_date,1,7)
        """
    ).fetchall()
    monthly_by_entity: dict[str, list[dict[str, Any]]] = {}
    for row in monthly_rows:
        entity_id = str(row["entity_id"])
        if entity_id not in counts:
            continue
        month = str(row["month"])
        count = int(row["paper_count"] or 0)
        monthly_by_entity.setdefault(entity_id, []).append(
            {
                "month": month,
                "count": count,
                "share_pct": round(count * 100.0 / monthly_totals.get(month, 1), 3),
            }
        )

    shared_current_total = sum(source_totals[source].get("current", 0) for source in shared_sources)
    shared_baseline_total = sum(source_totals[source].get("baseline", 0) for source in shared_sources)
    source_support_ratio = min(
        1.0,
        shared_current_total / current_total if current_total else 0.0,
        shared_baseline_total / baseline_total if baseline_total else 0.0,
    )
    results: list[dict[str, Any]] = []
    for entity_id, item in counts.items():
        current_count = int(item.get("current", 0))
        baseline_count = int(item.get("baseline", 0))
        if current_count < min_count and current_count + baseline_count < min_count * 2:
            continue
        raw = _momentum_metrics(
            current_count,
            baseline_count,
            current_total,
            baseline_total,
            reliability_coverage,
            min_count,
        )
        item.update(raw)
        item["raw_current_share_pct"] = raw["current_share_pct"]
        item["raw_baseline_share_pct"] = raw["baseline_share_pct"]
        item["raw_momentum"] = raw["momentum"]
        entity_sources = source_entity_counts.get(entity_id, {})
        comparable_current = sum(entity_sources.get(source, {}).get("current", 0) for source in shared_sources)
        comparable_baseline = sum(entity_sources.get(source, {}).get("baseline", 0) for source in shared_sources)
        item["comparable_current_count"] = comparable_current
        item["comparable_baseline_count"] = comparable_baseline
        item["comparable_sources"] = sorted(shared_sources)
        comparable_support = comparable_current + comparable_baseline
        if not source_weights or comparable_support < min_count:
            item["momentum"] = 0.0
            item["relative_momentum_score"] = 0.0
            item["reliability"] = 0.0
            item["status"] = "source_mix_confounded"
            item["heat_score"] = round(min(15.0, 3.0 * math.log1p(current_count)), 1)
        else:
            current_share = sum(
                source_weights[source]
                * entity_sources.get(source, {}).get("current", 0)
                / source_totals[source]["current"]
                for source in shared_sources
            )
            baseline_share = sum(
                source_weights[source]
                * entity_sources.get(source, {}).get("baseline", 0)
                / source_totals[source]["baseline"]
                for source in shared_sources
            )
            effective_n = sum(
                source_weights[source]
                * (source_totals[source]["current"] + source_totals[source]["baseline"])
                / 2.0
                for source in shared_sources
            )
            prior_share = 0.5 / max(1.0, effective_n)
            log_ratio = math.log2(
                max(current_share + prior_share, 1e-12)
                / max(baseline_share + prior_share, 1e-12)
            )
            reliability = min(1.0, math.sqrt(comparable_support / 20.0))
            reliability *= reliability_coverage * math.sqrt(max(0.0, source_support_ratio))
            reliability = round(min(1.0, reliability), 2)
            momentum = round(100.0 * math.tanh(log_ratio / 2.0) * reliability, 1)
            item["current_share_pct"] = round(current_share * 100.0, 3)
            item["baseline_share_pct"] = round(baseline_share * 100.0, 3)
            item["share_change_pp"] = round((current_share - baseline_share) * 100.0, 3)
            item["growth_ratio"] = round((current_share + prior_share) / (baseline_share + prior_share), 2)
            item["momentum"] = momentum
            item["relative_momentum_score"] = momentum
            item["reliability"] = reliability
            if current_count < min_count or current_total < 5 or baseline_total < 5:
                item["status"] = "insufficient_evidence"
            elif momentum >= 15:
                item["status"] = "emerging" if comparable_baseline == 0 else "rising"
            elif momentum <= -15:
                item["status"] = "cooling"
            else:
                item["status"] = "stable"
            item["heat_score"] = round(
                max(0.0, momentum) * 0.55
                + min(100.0, 25.0 * math.log1p(comparable_current)) * 0.45,
                1,
            )
        item["current_extraction_coverage_pct"] = round(current_coverage * 100.0, 1)
        item["baseline_extraction_coverage_pct"] = round(baseline_coverage * 100.0, 1)
        item["coverage_comparable"] = coverage_comparable
        if not coverage_comparable:
            item["momentum"] = 0.0
            item["relative_momentum_score"] = 0.0
            item["reliability"] = 0.0
            item["status"] = "insufficient_evidence"
            item["heat_score"] = round(min(15.0, 3.0 * math.log1p(current_count)), 1)
        item["monthly"] = sorted(monthly_by_entity.get(entity_id, []), key=lambda value: value["month"])
        results.append(item)
    results.sort(
        key=lambda value: (
            value["status"] != "source_mix_confounded",
            value["heat_score"],
            value["comparable_current_count"],
        ),
        reverse=True,
    )
    return (
        results[:limit],
        round(current_coverage * 100.0, 1),
        round(baseline_coverage * 100.0, 1),
    )


def _source_mix_metrics(connection: sqlite3.Connection) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {}
    rows = connection.execute(
        """
        SELECT source_name, window_name, COUNT(DISTINCT canonical_paper_id) AS paper_count
        FROM temp.analytics_output_sources
        GROUP BY source_name, window_name
        """
    )
    for row in rows:
        source = _effective_source(row["source_name"])
        window = str(row["window_name"])
        counts.setdefault(source, {})[window] = counts.setdefault(source, {}).get(window, 0) + int(row["paper_count"] or 0)
    current_total = sum(item.get("current", 0) for item in counts.values())
    baseline_total = sum(item.get("baseline", 0) for item in counts.values())
    distribution = []
    total_variation = 0.0
    comparable = 0
    for source in sorted(counts, key=lambda value: -counts[value].get("current", 0)):
        current_share = counts[source].get("current", 0) / current_total if current_total else 0.0
        baseline_share = counts[source].get("baseline", 0) / baseline_total if baseline_total else 0.0
        total_variation += abs(current_share - baseline_share)
        if counts[source].get("current", 0) >= 10 and counts[source].get("baseline", 0) >= 10:
            comparable += 1
        distribution.append({
            "name": source,
            "current_count": counts[source].get("current", 0),
            "baseline_count": counts[source].get("baseline", 0),
            "current_share_pct": round(current_share * 100.0, 1),
            "baseline_share_pct": round(baseline_share * 100.0, 1),
        })
    return {
        "distribution": distribution,
        "shift_pct": round(total_variation * 50.0, 1),
        "comparable_source_count": comparable,
    }

def _source_overlap(connection: sqlite3.Connection) -> dict[str, Any]:
    memberships: dict[str, set[str]] = {}
    rows = connection.execute(
        """
        SELECT canonical_paper_id, source_name
        FROM temp.analytics_output_sources
        WHERE window_name='current'
        """
    ).fetchall()
    for row in rows:
        paper_id = str(row["canonical_paper_id"])
        source = _effective_source(row["source_name"])
        if source not in {"unknown", "legacy_intake"}:
            memberships.setdefault(paper_id, set()).add(source)
    source_counts: dict[str, int] = {}
    for sources_for_paper in memberships.values():
        for source in sources_for_paper:
            source_counts[source] = source_counts.get(source, 0) + 1
    sources = [name for name, _ in sorted(source_counts.items(), key=lambda item: (-item[1], item[0]))[:8]]
    matrix = [
        [sum(1 for values in memberships.values() if left in values and right in values) for right in sources]
        for left in sources
    ]
    multi_source = sum(1 for values in memberships.values() if len(values) >= 2)
    return {
        "sources": sources,
        "matrix": matrix,
        "paper_counts": [source_counts[source] for source in sources],
        "observed_papers": len(memberships),
        "multi_source_papers": multi_source,
        "multi_source_coverage_pct": _percentage(multi_source, len(memberships)),
    }

def _publication_pathways(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    counts = dict(
        connection.execute(
            """
            WITH current_activity AS (
              SELECT canonical_paper_id,
                MAX(CASE WHEN version_type='preprint' THEN 1 ELSE 0 END) AS current_preprint,
                MAX(CASE WHEN version_type IN ('publication','published') THEN 1 ELSE 0 END) AS current_publication
              FROM temp.analytics_scoped_versions
              WHERE window_name='current'
              GROUP BY canonical_paper_id
            ), lifetime AS (
              SELECT a.canonical_paper_id, a.current_preprint, a.current_publication,
                MAX(CASE WHEN v.version_type='preprint' THEN 1 ELSE 0 END) AS has_preprint,
                MAX(CASE WHEN v.version_type IN ('publication','published') THEN 1 ELSE 0 END) AS has_publication
              FROM current_activity a
              JOIN paper_versions v ON v.canonical_paper_id=a.canonical_paper_id
              GROUP BY a.canonical_paper_id, a.current_preprint, a.current_publication
            )
            SELECT
              SUM(CASE WHEN current_preprint=1 AND has_publication=0 THEN 1 ELSE 0 END) AS preprint_only,
              SUM(CASE WHEN current_publication=1 AND has_preprint=0 THEN 1 ELSE 0 END) AS publication_only,
              SUM(CASE WHEN current_publication=1 AND has_preprint=1 THEN 1 ELSE 0 END) AS linked
            FROM lifetime
            """
        ).fetchone()
    )
    lag_rows = connection.execute(
        """
        WITH current_publications AS (
          SELECT DISTINCT canonical_paper_id
          FROM temp.analytics_scoped_versions
          WHERE window_name='current' AND version_type IN ('publication','published')
        )
        SELECT cp.canonical_paper_id,
          MIN(CASE WHEN v.version_type='preprint' THEN julianday(COALESCE(NULLIF(v.submitted_date,''), NULLIF(v.publication_date,''))) END) AS preprint_day,
          MIN(CASE WHEN v.version_type IN ('publication','published') THEN julianday(COALESCE(NULLIF(v.publication_date,''), NULLIF(v.submitted_date,''))) END) AS publication_day
        FROM current_publications cp
        JOIN paper_versions v ON v.canonical_paper_id=cp.canonical_paper_id
        GROUP BY cp.canonical_paper_id
        HAVING preprint_day IS NOT NULL AND publication_day IS NOT NULL
        """
    ).fetchall()
    lags = [
        max(0.0, float(row["publication_day"] - row["preprint_day"]))
        for row in lag_rows
        if row["preprint_day"] is not None and row["publication_day"] is not None
    ]
    linked_lag = round(median(lags), 1) if lags else None
    return [
        {"name": "preprint_only", "count": int(counts.get("preprint_only") or 0), "median_lag_days": None},
        {"name": "publication_only", "count": int(counts.get("publication_only") or 0), "median_lag_days": None},
        {"name": "linked_preprint_publication", "count": int(counts.get("linked") or 0), "median_lag_days": linked_lag},
    ]

def _quality_timeline(connection: sqlite3.Connection, days: int) -> list[dict[str, Any]]:
    if days <= 45:
        bucket = "research_date"
        key = "date"
    elif days <= 180:
        bucket = "date(research_date, printf('-%d days', (CAST(strftime('%w', research_date) AS INTEGER)+6)%7))"
        key = "date"
    else:
        bucket = "substr(research_date,1,7)"
        key = "month"
    rows = connection.execute(
        f"""
        SELECT {bucket} AS bucket, COUNT(*) AS total,
               SUM(has_abstract) AS abstracts, SUM(has_doi) AS dois, SUM(is_open_access) AS oa,
               SUM(CASE WHEN EXISTS (
                 SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=o.canonical_paper_id
               ) THEN 1 ELSE 0 END) AS pdfs
        FROM temp.analytics_canonical_outputs o
        WHERE window_name='current'
        GROUP BY {bucket}
        ORDER BY bucket
        """
    ).fetchall()
    return [
        {
            key: str(row["bucket"]),
            "total": int(row["total"] or 0),
            "abstract_coverage_pct": _percentage(row["abstracts"] or 0, row["total"] or 0),
            "doi_coverage_pct": _percentage(row["dois"] or 0, row["total"] or 0),
            "oa_coverage_pct": _percentage(row["oa"] or 0, row["total"] or 0),
            "pdf_coverage_pct": _percentage(row["pdfs"] or 0, row["total"] or 0),
        }
        for row in rows
    ]

_ARXIV_FIELD_MAP = {
    "cond-mat.mes-hall": "mesoscopic_and_electronic",
    "cond-mat.mtrl-sci": "materials_science",
    "cond-mat.str-el": "strongly_correlated",
    "cond-mat.stat-mech": "statistical_mechanics",
    "cond-mat.soft": "soft_matter",
    "cond-mat.supr-con": "superconductivity",
    "cond-mat.quant-gas": "quantum_gases",
    "cond-mat.dis-nn": "disorder_and_neural_networks",
    "cond-mat.other": "other_condensed_matter",
}

_PRIMARY_FIELD_KEYWORDS = {
    "superconductivity": (
        "superconduct", "pairing", "cuprate", "josephson", "bogoliubov",
    ),
    "strongly_correlated": (
        "hubbard", "mott", "kondo", "strongly correlated", "spin liquid",
        "heavy fermion", "many-body", "fractional chern",
    ),
    "soft_matter": (
        "soft matter", "polymer", "colloid", "granular", "liquid crystal",
        "active matter", "rheology",
    ),
    "statistical_mechanics": (
        "statistical mechanics", "critical phenomena", "phase transition",
        "nonequilibrium", "stochastic", "ising model",
    ),
    "quantum_gases": (
        "ultracold", "bose gas", "fermi gas", "optical lattice",
        "bose-einstein", "quantum gas",
    ),
    "disorder_and_neural_networks": (
        "disorder", "localization", "amorphous", "spin glass", "percolation",
        "neural network",
    ),
    "materials_science": (
        "synthesis", "crystal growth", "thin film", "materials science",
        "alloy", "perovskite", "battery", "fabrication",
    ),
    "mesoscopic_and_electronic": (
        "graphene", "topological", "quantum hall", "band structure", "transport",
        "semiconductor", "spintronic", "quantum oscillation", "moire", "moiré",
    ),
}


def _primary_field_distribution(
    connection: sqlite3.Connection,
    current_total: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    papers = connection.execute(
        """
        SELECT o.canonical_paper_id,
               COALESCE(
                 (SELECT v.raw_json FROM paper_versions v
                  WHERE v.canonical_paper_id=o.canonical_paper_id
                    AND lower(v.source)='openalex'
                  ORDER BY COALESCE(v.last_seen_at,'') DESC, v.id DESC LIMIT 1),
                 p.raw_json
               ) AS raw_json,
               p.title, p.abstract
        FROM temp.analytics_canonical_outputs o
        JOIN papers p ON p.id=o.canonical_paper_id
        WHERE o.window_name='current'
        """
    ).fetchall()
    terms: dict[str, list[str]] = {}
    for row in connection.execute(
        """
        SELECT pt.paper_id, pt.normalized_term
        FROM paper_terms pt
        JOIN temp.analytics_canonical_outputs o ON o.canonical_paper_id=pt.paper_id
        WHERE o.window_name='current' AND pt.display_eligible=1
        """
    ):
        terms.setdefault(str(row["paper_id"]), []).append(str(row["normalized_term"]))

    classified: dict[str, dict[str, int]] = {}
    direct_count = 0
    inferred_count = 0
    unclassified_count = 0
    for paper in papers:
        paper_id = str(paper["canonical_paper_id"])
        field = ""
        method = ""
        try:
            raw = json.loads(str(paper["raw_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            raw = {}
        categories = raw.get("categories") or []
        if isinstance(categories, str):
            categories = categories.split()
        primary_category = raw.get("primary_category") or raw.get("primary_category_term")
        ordered_categories = ([primary_category] if primary_category else []) + list(categories)
        for category_value in ordered_categories:
            if isinstance(category_value, dict):
                category = str(category_value.get("term") or category_value.get("id") or "")
            else:
                category = str(category_value)
            if category in _ARXIV_FIELD_MAP:
                field = _ARXIV_FIELD_MAP[category]
                method = "direct_arxiv_primary_category"
                break
            if category.startswith("cond-mat."):
                field = "other_condensed_matter"
                method = "direct_arxiv_primary_category"
                break
        if not field:
            primary_topic = raw.get("primary_topic") or {}
            if isinstance(primary_topic, dict):
                subfield = primary_topic.get("subfield") or {}
                subfield_id = subfield.get("id") if isinstance(subfield, dict) else subfield
                if str(subfield_id or "").rstrip("/").rsplit("/", 1)[-1] == "3104":
                    topic_evidence = str(primary_topic.get("display_name") or "").casefold()
                    scores = {
                        field_name: sum(1 for keyword in keywords if keyword in topic_evidence)
                        for field_name, keywords in _PRIMARY_FIELD_KEYWORDS.items()
                    }
                    field, score = max(scores.items(), key=lambda item: item[1])
                    if score <= 0:
                        field = "other_condensed_matter"
                    method = "direct_openalex_primary_topic"
        if not field:
            evidence = " ".join(
                [
                    str(paper["title"] or ""),
                    str(paper["abstract"] or ""),
                    *terms.get(paper_id, []),
                ]
            ).casefold()
            scores = {
                field_name: sum(1 for keyword in keywords if keyword in evidence)
                for field_name, keywords in _PRIMARY_FIELD_KEYWORDS.items()
            }
            field, score = max(scores.items(), key=lambda item: item[1])
            if score > 0:
                method = "inferred_from_text_and_terms"
            else:
                field = "unclassified"
                method = "unclassified"
        target = classified.setdefault(field, {"direct": 0, "inferred": 0, "unclassified": 0})
        if method.startswith("direct_"):
            target["direct"] += 1
            direct_count += 1
        elif method == "inferred_from_text_and_terms":
            target["inferred"] += 1
            inferred_count += 1
        else:
            target["unclassified"] += 1
            unclassified_count += 1

    distribution = []
    for field, counts in classified.items():
        count = counts["direct"] + counts["inferred"] + counts["unclassified"]
        distribution.append(
            {
                "name": field,
                "count": count,
                "share_pct": _percentage(count, current_total),
                "direct_count": counts["direct"],
                "inferred_count": counts["inferred"],
                "unclassified_count": counts["unclassified"],
            }
        )
    distribution.sort(key=lambda item: (-item["count"], item["name"]))
    quality = {
        "classification_coverage_pct": _percentage(direct_count + inferred_count, current_total),
        "direct_classification_pct": _percentage(direct_count, current_total),
        "inferred_classification_pct": _percentage(inferred_count, current_total),
        "unclassified_count": unclassified_count,
        "exclusive_assignment": True,
    }
    return distribution, quality

def build_analytics_payload(connection: sqlite3.Connection, days: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today = now.date()
    _prepare_eligible_canonical_papers(connection)
    data_anchor, latest_observed, excluded_partial_today = _latest_stable_anchor(connection, today)
    anchor_day = datetime.strptime(data_anchor, "%Y-%m-%d").date()
    range_from = (anchor_day - timedelta(days=days - 1)).isoformat()
    baseline_to = (anchor_day - timedelta(days=days)).isoformat()
    baseline_from = (anchor_day - timedelta(days * 2 - 1)).isoformat()
    _prepare_windows(connection, baseline_from, range_from, data_anchor)

    totals = {
        str(row["window_name"]): int(row["paper_count"] or 0)
        for row in connection.execute(
            "SELECT window_name, COUNT(*) AS paper_count FROM temp.analytics_canonical_outputs GROUP BY window_name"
        )
    }
    current_total = totals.get("current", 0)
    baseline_total = totals.get("baseline", 0)
    summary_row = dict(
        connection.execute(
            """
            SELECT COUNT(*) AS window_papers, SUM(has_preprint) AS preprints,
                   SUM(has_publication) AS publications, SUM(has_abstract) AS abstract_available,
                   SUM(CASE WHEN has_abstract=0 THEN 1 ELSE 0 END) AS abstract_missing,
                   SUM(has_doi) AS doi_count, SUM(is_open_access) AS oa_count,
                   SUM(CASE WHEN EXISTS (
                     SELECT 1 FROM paper_files f WHERE f.canonical_paper_id=o.canonical_paper_id
                   ) THEN 1 ELSE 0 END) AS pdf_count
            FROM temp.analytics_canonical_outputs o WHERE window_name='current'
            """
        ).fetchone()
    )

    timeline_by_day = {
        str(row["day"]): dict(row)
        for row in connection.execute(
            """
            SELECT research_date AS day, COUNT(*) AS total,
                   SUM(has_preprint) AS preprints, SUM(has_publication) AS publications
            FROM temp.analytics_canonical_outputs
            WHERE window_name='current' GROUP BY research_date ORDER BY research_date
            """
        )
    }
    paper_timeline = []
    for offset in range(days):
        day = (anchor_day - timedelta(days=days - 1 - offset)).isoformat()
        row = timeline_by_day.get(day, {})
        paper_timeline.append({
            "date": day,
            "preprints": int(row.get("preprints") or 0),
            "publications": int(row.get("publications") or 0),
            "total": int(row.get("total") or 0),
        })

    version_event_timeline = [
        dict(row)
        for row in connection.execute(
            """
            SELECT paper_date AS date,
              COUNT(DISTINCT CASE WHEN version_type='preprint' THEN canonical_paper_id END) AS preprints,
              COUNT(DISTINCT CASE WHEN version_type IN ('publication','published') THEN canonical_paper_id END) AS publications,
              COUNT(DISTINCT canonical_paper_id) AS total
            FROM temp.analytics_scoped_versions WHERE window_name='current'
            GROUP BY paper_date ORDER BY paper_date
            """
        )
    ]
    for row in version_event_timeline:
        for field in ("preprints", "publications", "total"):
            row[field] = int(row[field] or 0)

    source_distribution = []
    for row in connection.execute(
        """
        SELECT source_name AS name, COUNT(DISTINCT canonical_paper_id) AS count
        FROM temp.analytics_output_sources
        WHERE window_name='current'
        GROUP BY source_name ORDER BY count DESC, source_name LIMIT 20
        """
    ):
        source_distribution.append({"name": _effective_source(row["name"]), "count": int(row["count"] or 0)})

    journal_distribution = [
        {"name": str(row["name"]), "count": int(row["count"] or 0)}
        for row in connection.execute(
            """
            SELECT COALESCE(NULLIF(p.journal,''), 'Unknown') AS name, COUNT(*) AS count
            FROM temp.analytics_canonical_outputs o JOIN papers p ON p.id=o.canonical_paper_id
            WHERE o.window_name='current' GROUP BY name ORDER BY count DESC, name LIMIT 15
            """
        )
    ]

    download_rows = connection.execute(
        """
        WITH ranked AS (
          SELECT d.canonical_paper_id, d.status,
                 ROW_NUMBER() OVER (PARTITION BY d.canonical_paper_id ORDER BY d.updated_at DESC, d.id DESC) AS rn
          FROM download_tasks d JOIN temp.analytics_canonical_outputs o ON o.canonical_paper_id=d.canonical_paper_id
          WHERE o.window_name='current'
        )
        SELECT COALESCE(r.status, 'not_requested') AS status, COUNT(*) AS count
        FROM temp.analytics_canonical_outputs o
        LEFT JOIN ranked r ON r.canonical_paper_id=o.canonical_paper_id AND r.rn=1
        WHERE o.window_name='current' GROUP BY COALESCE(r.status, 'not_requested')
        """
    ).fetchall()
    download_counts = {str(row["status"]): int(row["count"] or 0) for row in download_rows}
    status_order = ("completed", "pending", "resolving", "downloading", "retryable_failed", "permanent_failed", "manual_review", "not_requested")
    download_status = [{"status": status, "count": download_counts.get(status, 0)} for status in status_order]

    monitor_rows = connection.execute(
        """
        SELECT substr(mh.first_matched_at,1,10) AS day, COUNT(*) AS hits
        FROM monitor_hits mh
        JOIN temp.analytics_eligible_canonical_papers e
          ON e.canonical_paper_id=mh.canonical_paper_id
        WHERE date(substr(mh.first_matched_at,1,10)) BETWEEN date(?) AND date(?)
        GROUP BY substr(mh.first_matched_at,1,10) ORDER BY day
        """,
        (range_from, data_anchor),
    ).fetchall()
    monitor_by_day = {str(row["day"]): int(row["hits"] or 0) for row in monitor_rows}
    monitor_timeline = [{"date": item["date"], "hits": monitor_by_day.get(item["date"], 0)} for item in paper_timeline]
    monitor_hits = sum(item["hits"] for item in monitor_timeline)

    top_materials = [
        dict(row)
        for row in connection.execute(
            """
            SELECT m.id, m.canonical_name AS name, m.material_family AS family,
                   COUNT(DISTINCT pm.canonical_paper_id) AS paper_count,
                   COUNT(DISTINCT CASE WHEN o.has_preprint=1 THEN pm.canonical_paper_id END) AS preprints,
                   COUNT(DISTINCT CASE WHEN o.has_publication=1 THEN pm.canonical_paper_id END) AS publications,
                   COUNT(*) AS evidence_mentions, MAX(o.research_date) AS latest_paper_date
            FROM temp.analytics_canonical_outputs o
            JOIN paper_materials pm ON pm.canonical_paper_id=o.canonical_paper_id
            JOIN materials m ON m.id=pm.material_id
            WHERE o.window_name='current' AND pm.confidence>=0.6
            GROUP BY m.id, m.canonical_name, m.material_family
            ORDER BY paper_count DESC, evidence_mentions DESC, m.canonical_name LIMIT 15
            """
        )
    ]
    for item in top_materials:
        for field in ("paper_count", "preprints", "publications", "evidence_mentions"):
            item[field] = int(item[field] or 0)

    monthly_totals = {
        str(row["month"]): int(row["paper_count"] or 0)
        for row in connection.execute(
            "SELECT substr(research_date,1,7) AS month, COUNT(*) AS paper_count FROM temp.analytics_canonical_outputs GROUP BY month"
        )
    }
    topic_trends, current_topic_coverage, baseline_topic_coverage = _entity_trends(
        connection, "topic", current_total, baseline_total, monthly_totals
    )
    material_trends, current_material_coverage, baseline_material_coverage = _entity_trends(
        connection, "material", current_total, baseline_total, monthly_totals
    )
    method_trends, current_method_coverage, baseline_method_coverage = _entity_trends(
        connection, "method", current_total, baseline_total, monthly_totals
    )

    entity_type_coverage = [
        {"name": str(row["name"]), "count": int(row["count"] or 0), "share_pct": _percentage(row["count"] or 0, current_total)}
        for row in connection.execute(
            """
            SELECT pt.term_type AS name, COUNT(DISTINCT pt.paper_id) AS count
            FROM temp.analytics_canonical_outputs o JOIN paper_terms pt ON pt.paper_id=o.canonical_paper_id
            WHERE o.window_name='current' AND pt.display_eligible=1
            GROUP BY pt.term_type ORDER BY count DESC, pt.term_type
            """
        )
    ]
    field_distribution, primary_field_quality = _primary_field_distribution(connection, current_total)

    source_overlap = _source_overlap(connection)
    source_mix = _source_mix_metrics(connection)
    publication_pathways = _publication_pathways(connection)
    quality_timeline = _quality_timeline(connection, days)

    citation_distribution = [
        {"name": str(row["bucket"]), "count": int(row["count"] or 0)}
        for row in connection.execute(
            """
            SELECT CASE WHEN cited_by_count=0 THEN '0' WHEN cited_by_count<=5 THEN '1-5'
                        WHEN cited_by_count<=20 THEN '6-20' WHEN cited_by_count<=100 THEN '21-100'
                        ELSE '100+' END AS bucket, COUNT(*) AS count
            FROM temp.analytics_canonical_outputs WHERE window_name='current'
            GROUP BY bucket
            ORDER BY CASE bucket WHEN '0' THEN 0 WHEN '1-5' THEN 1 WHEN '6-20' THEN 2 WHEN '21-100' THEN 3 ELSE 4 END
            """
        )
    ]
    downloads_completed = download_counts.get("completed", 0)
    downloads_failed = download_counts.get("retryable_failed", 0) + download_counts.get("permanent_failed", 0)
    abstract_available = int(summary_row.get("abstract_available") or 0)
    abstract_missing = int(summary_row.get("abstract_missing") or 0)
    doi_count = int(summary_row.get("doi_count") or 0)
    oa_count = int(summary_row.get("oa_count") or 0)
    pdf_count = int(summary_row.get("pdf_count") or 0)
    summary = {
        "window_papers": current_total,
        "baseline_papers": baseline_total,
        "preprints": int(summary_row.get("preprints") or 0),
        "publications": int(summary_row.get("publications") or 0),
        "abstract_available": abstract_available,
        "abstract_missing": abstract_missing,
        "abstract_coverage_pct": _percentage(abstract_available, current_total),
        "doi_count": doi_count,
        "doi_coverage_pct": _percentage(doi_count, current_total),
        "oa_count": oa_count,
        "oa_coverage_pct": _percentage(oa_count, current_total),
        "pdf_count": pdf_count,
        "pdf_coverage_pct": _percentage(pdf_count, current_total),
        "monitor_hits": monitor_hits,
        "downloads_completed": downloads_completed,
        "downloads_failed": downloads_failed,
        "success_rate_pct": _percentage(downloads_completed, downloads_completed + downloads_failed),
    }
    summary["download_success_rate_pct"] = summary["success_rate_pct"]

    pending_conflicts = int(connection.execute(
        """
        SELECT COUNT(*)
        FROM manual_review_items r
        JOIN temp.analytics_eligible_canonical_papers e
          ON e.canonical_paper_id=r.candidate_paper_id
        WHERE r.review_type='identity_conflict' AND r.status='pending'
        """
    ).fetchone()[0] or 0)
    source_cursor_count = int(connection.execute("SELECT COUNT(*) FROM source_cursors").fetchone()[0] or 0)
    freshness_days = max(0, (today - anchor_day).days)
    volume_ratio = round(current_total / baseline_total, 2) if baseline_total else 0.0
    topic_coverage_comparable = bool(
        current_topic_coverage >= 60
        and baseline_topic_coverage >= 60
        and abs(current_topic_coverage - baseline_topic_coverage) <= 15
    )
    material_coverage_comparable = bool(
        current_material_coverage >= 60
        and baseline_material_coverage >= 60
        and abs(current_material_coverage - baseline_material_coverage) <= 15
    )
    method_coverage_comparable = bool(
        current_method_coverage >= 60
        and baseline_method_coverage >= 60
        and abs(current_method_coverage - baseline_method_coverage) <= 15
    )
    provenance_overlap_reliable = bool(
        source_overlap["multi_source_coverage_pct"] >= 25
        and len(source_overlap["sources"]) >= 3
    )
    common_trend_reliable = bool(
        current_total >= 100
        and baseline_total >= 100
        and 0.5 <= volume_ratio <= 2.0
        and summary["abstract_coverage_pct"] >= 70
        and source_mix["shift_pct"] <= 15
        and freshness_days <= 3
        and provenance_overlap_reliable
    )
    topic_trend_reliable = bool(common_trend_reliable and topic_coverage_comparable)
    material_trend_reliable = bool(common_trend_reliable and material_coverage_comparable)
    method_trend_reliable = bool(common_trend_reliable and method_coverage_comparable)
    # Overall release gate; each chart also exposes its entity-specific gate.
    hotspot_reliable = bool(
        topic_trend_reliable
        and material_trend_reliable
        and method_trend_reliable
    )
    data_quality = {
        "current_research_outputs": current_total,
        "baseline_research_outputs": baseline_total,
        "window_volume_ratio": volume_ratio,
        "abstract_coverage_pct": summary["abstract_coverage_pct"],
        "doi_coverage_pct": summary["doi_coverage_pct"],
        "current_topic_extraction_coverage_pct": current_topic_coverage,
        "baseline_topic_extraction_coverage_pct": baseline_topic_coverage,
        "topic_extraction_coverage_pct": current_topic_coverage,
        "current_material_extraction_coverage_pct": current_material_coverage,
        "baseline_material_extraction_coverage_pct": baseline_material_coverage,
        "material_extraction_coverage_pct": current_material_coverage,
        "current_method_extraction_coverage_pct": current_method_coverage,
        "baseline_method_extraction_coverage_pct": baseline_method_coverage,
        "method_extraction_coverage_pct": current_method_coverage,
        "topic_coverage_comparable": topic_coverage_comparable,
        "material_coverage_comparable": material_coverage_comparable,
        "method_coverage_comparable": method_coverage_comparable,
        "primary_field_classification_coverage_pct": primary_field_quality["classification_coverage_pct"],
        "primary_field_direct_classification_pct": primary_field_quality["direct_classification_pct"],
        "primary_field_inferred_classification_pct": primary_field_quality["inferred_classification_pct"],
        "multi_source_coverage_pct": source_overlap["multi_source_coverage_pct"],
        "source_mix_shift": source_mix["shift_pct"],
        "source_mix_shift_pct": source_mix["shift_pct"],
        "comparable_source_count": source_mix["comparable_source_count"],
        "source_count": len(source_overlap["sources"]),
        "source_cursor_count": source_cursor_count,
        "pending_identity_conflicts": pending_conflicts,
        "data_freshness_days": freshness_days,
        "partial_today_excluded": excluded_partial_today,
        "hotspot_reliable": hotspot_reliable,
        "common_trend_reliable": common_trend_reliable,
        "topic_trend_reliable": topic_trend_reliable,
        "material_trend_reliable": material_trend_reliable,
        "method_trend_reliable": method_trend_reliable,
        "provenance_overlap_reliable": provenance_overlap_reliable,
    }
    warnings: list[str] = []
    if summary["abstract_coverage_pct"] < 70:
        warnings.append("摘要覆盖率低于70%，主题抽取和语义热点可能偏向元数据完整的来源。")
    if not topic_coverage_comparable:
        warnings.append("前后窗口的主题抽取覆盖不足或差异超过15个百分点，主题动量已降级为证据不足。")
    if not material_coverage_comparable:
        warnings.append("前后窗口的材料证据覆盖不足或不可比，材料榜单仅显示样本内频次。")
    if not method_coverage_comparable:
        warnings.append("前后窗口的方法抽取覆盖不足或不可比，方法动量不用于热点结论。")
    if source_mix["shift_pct"] > 15:
        warnings.append("前后窗口来源构成变化超过15%，热点已按共同来源分层校正。")
    if source_overlap["multi_source_coverage_pct"] < 25:
        warnings.append("多源交叉命中率不足，当前无法用来源交集证明全网覆盖率。")
    if not (0.5 <= volume_ratio <= 2.0):
        warnings.append("前后等长窗口论文量差异过大，可能存在历史回填或来源启用时间偏差。")
    if primary_field_quality["direct_classification_pct"] < 50:
        warnings.append("多数研究方向依赖文本推断而非来源主分类，方向占比需结合分类置信度查看。")
    if baseline_total < 100:
        warnings.append("对照窗口样本较少，增长率已做支持度收缩但仍不宜作强结论。")
    if freshness_days > 3:
        warnings.append("数据锚点距今天超过3天，近期热点存在滞后。")
    if excluded_partial_today:
        warnings.append("趋势统计排除了尚未完整结束的今天；实时新增应在首页单独查看。")
    source_health = [
        dict(row)
        for row in connection.execute(
            """
            SELECT source_name, status, last_successful_at, last_attempt_at,
                   CASE WHEN trim(COALESCE(error_message,''))<>'' THEN 1 ELSE 0 END AS has_error
            FROM source_cursors ORDER BY source_name
            """
        )
    ]
    download_source_performance = [
        {"name": str(row["name"]), "attempted": int(row["attempted"] or 0), "completed": int(row["completed"] or 0), "success_rate_pct": _percentage(row["completed"] or 0, row["attempted"] or 0)}
        for row in connection.execute(
            """
            SELECT COALESCE(NULLIF(source,''), 'unknown') AS name,
                   COUNT(*) AS attempted, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed
            FROM download_tasks d
            JOIN temp.analytics_eligible_canonical_papers e
              ON e.canonical_paper_id=d.canonical_paper_id
            GROUP BY COALESCE(NULLIF(source,''), 'unknown')
            ORDER BY attempted DESC, name LIMIT 12
            """
        )
    ]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "data_anchor": data_anchor,
        "latest_observed_date": latest_observed,
        "range_from": range_from,
        "range_to": data_anchor,
        "baseline_from": baseline_from,
        "baseline_to": baseline_to,
        "days": days,
        "summary": summary,
        "paper_timeline": paper_timeline,
        "version_event_timeline": version_event_timeline,
        "source_distribution": source_distribution,
        "source_overlap": source_overlap,
        "source_window_distribution": source_mix["distribution"],
        "source_health": source_health,
        "journal_distribution": journal_distribution,
        "download_status": download_status,
        "download_source_performance": download_source_performance,
        "abstract_status": {"available": abstract_available, "missing": abstract_missing, "coverage_pct": summary["abstract_coverage_pct"]},
        "monitor_timeline": monitor_timeline,
        "top_materials": top_materials,
        "topic_trends": topic_trends,
        "material_trends": material_trends,
        "method_trends": method_trends,
        "field_distribution": field_distribution,
        "primary_field_quality": primary_field_quality,
        "entity_type_coverage": entity_type_coverage,
        "publication_pathways": publication_pathways,
        "quality_timeline": quality_timeline,
        "citation_distribution": citation_distribution,
        "data_quality": data_quality,
        "data_warnings": warnings,
        "trend_methodology": {
            "unit": "canonical_research_output",
            "date_assignment": "earliest_scholarly_version_date",
            "comparison": "equal_length_previous_window",
            "normalization": "source_standardized_entity_share_of_window_outputs",
            "source_adjustment": "fixed_weights_across_sources_present_in_both_windows",
            "extraction_gate": "both_windows_at_least_60pct_and_difference_at_most_15pp",
            "smoothing": "jeffreys_pseudocount_and_support_shrinkage",
            "incomplete_day_policy": "exclude_today_when_an_earlier_complete_day_exists",
        },
    }