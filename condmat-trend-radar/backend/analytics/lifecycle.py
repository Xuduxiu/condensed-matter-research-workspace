from __future__ import annotations

import sqlite3
from statistics import mean, pstdev
from typing import Any

from backend.db.database import utc_now
from backend.nlp.dictionaries import (
    CONCEPT_CLASS_OVERRIDES,
    GENERAL_FIELD_TERMS,
    MATERIALS,
    METHODS,
    PHYSICS_CONCEPTS,
    PLATFORM_MATERIALS,
)
from backend.nlp.material_registry import MATERIAL_REGISTRY, is_platform_material
from backend.nlp.normalize import month_range

BASELINE_SCOPE = "all"


def rebuild_lifecycle(
    conn: sqlite3.Connection,
    baseline_from: str,
    baseline_to: str,
    trend_from: str,
    trend_to: str,
) -> int:
    conn.execute("DELETE FROM concept_lifecycle")
    terms = [
        row["term"]
        for row in conn.execute(
            "SELECT DISTINCT term FROM term_month_stats WHERE corpus_scope = ? AND display_eligible = 1 ORDER BY term",
            (BASELINE_SCOPE,),
        ).fetchall()
    ]
    count = 0
    for term in terms:
        item = compute_lifecycle(conn, term, baseline_from, baseline_to, trend_from, trend_to)
        if not item:
            continue
        conn.execute(
            """
            INSERT INTO concept_lifecycle
              (concept, first_seen, historical_first_seen, trend_first_seen, peak_month, peak_value,
               half_life_months, active_duration_months, burst_duration_months, baseline_total_count,
               historical_count_before_trend, trend_total_count, novelty_score, concept_class,
               is_historical, is_platform_term, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["concept"],
                item["historical_first_seen"],
                item["historical_first_seen"],
                item["trend_first_seen"],
                item["peak_month"],
                item["peak_value"],
                item["half_life_months"],
                item["active_duration_months"],
                item["burst_duration_months"],
                item["baseline_total_count"],
                item["historical_count_before_trend"],
                item["trend_total_count"],
                item["novelty_score"],
                item["concept_class"],
                1 if item["is_historical"] else 0,
                1 if item["is_platform_term"] else 0,
                item["status"],
                utc_now(),
            ),
        )
        count += 1
    return count


def compute_lifecycle(
    conn: sqlite3.Connection,
    term: str,
    baseline_from: str,
    baseline_to: str,
    trend_from: str,
    trend_to: str,
) -> dict[str, Any] | None:
    baseline_from_m = baseline_from[:7]
    baseline_to_m = baseline_to[:7]
    trend_from_m = trend_from[:7]
    trend_to_m = trend_to[:7]
    rows = conn.execute(
        """
        SELECT month, raw_freq, weighted_freq
        FROM term_month_stats
        WHERE term = ? AND corpus_scope = ? AND month >= ? AND month <= ?
        ORDER BY month
        """,
        (term, BASELINE_SCOPE, baseline_from_m, baseline_to_m),
    ).fetchall()
    if not rows:
        return None

    raw_by_month = {row["month"]: int(row["raw_freq"] or 0) for row in rows}
    weighted_by_month = {row["month"]: float(row["weighted_freq"] or 0) for row in rows}
    baseline_months = month_range(baseline_from_m, baseline_to_m)
    trend_months = month_range(trend_from_m, trend_to_m)
    raw_values = [raw_by_month.get(month, 0) for month in baseline_months]
    weighted_values = [weighted_by_month.get(month, 0.0) for month in baseline_months]
    nonzero_months = [month for month in baseline_months if raw_by_month.get(month, 0) > 0]
    if not nonzero_months:
        return None

    historical_first_seen = nonzero_months[0]
    trend_nonzero_months = [month for month in trend_months if raw_by_month.get(month, 0) > 0]
    trend_first_seen = trend_nonzero_months[0] if trend_nonzero_months else None
    baseline_total_count = int(sum(raw_values))
    historical_count_before_trend = int(sum(raw_by_month.get(month, 0) for month in baseline_months if month < trend_from_m))
    trend_total_count = int(sum(raw_by_month.get(month, 0) for month in trend_months))
    novelty_score = 1.0 / (1.0 + historical_count_before_trend)
    peak_value = max(weighted_values)
    peak_idx = weighted_values.index(peak_value)
    peak_month = baseline_months[peak_idx]
    half_life = half_life_months(baseline_months, weighted_values, peak_idx, peak_value)
    activity_threshold = max(1.0, peak_value * 0.2)
    active_indices = [idx for idx, value in enumerate(weighted_values) if value >= activity_threshold]
    active_duration = active_indices[-1] - active_indices[0] + 1 if active_indices else 0
    sigma = pstdev(weighted_values) if len(weighted_values) > 1 else 0.0
    burst_threshold = mean(weighted_values) + sigma
    burst_duration = longest_run(value > burst_threshold for value in weighted_values)
    term_types = term_type_set(conn, term)
    concept_class = classify_concept(term, term_types)
    is_platform = concept_class == "platform_material" or term in PLATFORM_MATERIALS or is_platform_material(term)
    status = classify_status(
        raw_by_month=raw_by_month,
        weighted_by_month=weighted_by_month,
        trend_months=trend_months,
        peak_value=peak_value,
        historical_count_before_trend=historical_count_before_trend,
        trend_total_count=trend_total_count,
        active_months_across_baseline=sum(1 for value in raw_values if value > 0),
        concept_class=concept_class,
        is_platform_term=is_platform,
    )
    return {
        "concept": term,
        "historical_first_seen": historical_first_seen,
        "trend_first_seen": trend_first_seen,
        "peak_month": peak_month,
        "peak_value": peak_value,
        "half_life_months": half_life,
        "active_duration_months": active_duration,
        "burst_duration_months": burst_duration,
        "baseline_total_count": baseline_total_count,
        "historical_count_before_trend": historical_count_before_trend,
        "trend_total_count": trend_total_count,
        "novelty_score": novelty_score,
        "concept_class": concept_class,
        "is_historical": historical_count_before_trend > 0,
        "is_platform_term": is_platform,
        "status": status,
    }


def term_type_set(conn: sqlite3.Connection, term: str) -> set[str]:
    return {
        row["term_type"]
        for row in conn.execute(
            "SELECT DISTINCT term_type FROM paper_terms WHERE normalized_term = ?",
            (term,),
        ).fetchall()
    }


def classify_concept(term: str, term_types: set[str]) -> str:
    if term in CONCEPT_CLASS_OVERRIDES:
        return CONCEPT_CLASS_OVERRIDES[term]
    if term in PLATFORM_MATERIALS or is_platform_material(term):
        return "platform_material"
    if term in GENERAL_FIELD_TERMS:
        return "general_field"
    if "method" in term_types or term in METHODS:
        return "method"
    if "material" in term_types or term in MATERIALS or term in MATERIAL_REGISTRY:
        return "material_system"
    if term in PHYSICS_CONCEPTS:
        return "physics_concept"
    return "physics_concept"


def half_life_months(months: list[str], values: list[float], peak_idx: int, peak_value: float) -> int | None:
    if peak_value <= 0:
        return None
    half_threshold = peak_value * 0.5
    for idx in range(peak_idx + 1, len(months)):
        if values[idx] <= half_threshold:
            return idx - peak_idx
    return None


def classify_status(
    raw_by_month: dict[str, int],
    weighted_by_month: dict[str, float],
    trend_months: list[str],
    peak_value: float,
    historical_count_before_trend: int,
    trend_total_count: int,
    active_months_across_baseline: int,
    concept_class: str,
    is_platform_term: bool,
) -> str:
    recent_3m = trend_months[-3:]
    previous_3m = trend_months[-6:-3]
    recent_6m = trend_months[-6:]
    recent_3m_weighted = sum(weighted_by_month.get(month, 0.0) for month in recent_3m)
    previous_3m_weighted = sum(weighted_by_month.get(month, 0.0) for month in previous_3m)
    recent_6m_count = sum(raw_by_month.get(month, 0) for month in recent_6m)
    current_freq = weighted_by_month.get(trend_months[-1], 0.0) if trend_months else 0.0
    recent_freq = sum(weighted_by_month.get(month, 0.0) for month in recent_3m)
    growth = (recent_3m_weighted + 1.0) / (previous_3m_weighted + 1.0)

    if (
        is_platform_term
        or concept_class in {"platform_material", "method", "general_field"}
        or active_months_across_baseline >= 18
        or historical_count_before_trend >= 50
    ):
        return "persistent"
    if (
        concept_class in {"physics_concept", "material_system"}
        and historical_count_before_trend <= 5
        and recent_3m_weighted >= 3
        and growth >= 2
        and trend_total_count >= 3
    ):
        return "emerging"
    if historical_count_before_trend > 0 and recent_6m_count == 0:
        return "stale"
    if recent_6m_count > 0 and peak_value > 0 and current_freq >= 0.5 * peak_value and historical_count_before_trend > 5:
        return "active"
    if peak_value > 0 and recent_freq < 0.5 * peak_value and recent_6m_count > 0:
        return "cooling"
    if trend_total_count > 0:
        return "active"
    return "stale"


def longest_run(flags) -> int:
    best = 0
    current = 0
    for flag in flags:
        if flag:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def lifecycle_table(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = [dict(row) for row in conn.execute("SELECT * FROM concept_lifecycle ORDER BY peak_value DESC")]
    half_lives = [row["half_life_months"] for row in rows if row["half_life_months"] is not None]
    bursts = [row["burst_duration_months"] for row in rows if row["burst_duration_months"] is not None]
    return {
        "items": rows,
        "summary": {
            "median_concept_half_life": median(half_lives),
            "median_burst_duration": median(bursts),
            "longest_active_concepts": sorted(rows, key=lambda row: row["active_duration_months"] or 0, reverse=True)[:10],
            "fastest_rising_concepts": [row for row in rows if row["status"] == "emerging"][:10],
            "fastest_cooling_concepts": [row for row in rows if row["status"] == "cooling"][:10],
        },
    }


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2



# --- Data-mode aware lifecycle overrides. ---
def lifecycle_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
    requested = (requested or "auto").lower()
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    if requested == "mixed":
        return "mixed"
    if requested == "real" and real_count:
        return "real"
    if requested == "mock" and mock_count:
        return "mock"
    if real_count:
        return "real"
    if mock_count:
        return "mock"
    return "real"


def rebuild_lifecycle(conn: sqlite3.Connection, baseline_from: str, baseline_to: str, trend_from: str, trend_to: str, data_mode: str = "auto") -> int:
    mode = lifecycle_data_mode(conn, data_mode)
    conn.execute("DELETE FROM concept_lifecycle")
    terms = [
        row["term"]
        for row in conn.execute(
            "SELECT DISTINCT term FROM term_month_stats WHERE corpus_scope = ? AND data_mode = ? AND display_eligible = 1 ORDER BY term",
            (BASELINE_SCOPE, mode),
        ).fetchall()
    ]
    count = 0
    for term in terms:
        item = compute_lifecycle(conn, term, baseline_from, baseline_to, trend_from, trend_to, mode)
        if not item:
            continue
        conn.execute(
            """
            INSERT INTO concept_lifecycle
              (concept, first_seen, historical_first_seen, trend_first_seen, peak_month, peak_value,
               half_life_months, active_duration_months, burst_duration_months, baseline_total_count,
               historical_count_before_trend, trend_total_count, novelty_score, concept_class,
               is_historical, is_platform_term, status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item["concept"], item["historical_first_seen"], item["historical_first_seen"], item["trend_first_seen"],
                item["peak_month"], item["peak_value"], item["half_life_months"], item["active_duration_months"],
                item["burst_duration_months"], item["baseline_total_count"], item["historical_count_before_trend"],
                item["trend_total_count"], item["novelty_score"], item["concept_class"], 1 if item["is_historical"] else 0,
                1 if item["is_platform_term"] else 0, item["status"], utc_now(),
            ),
        )
        count += 1
    return count


def compute_lifecycle(conn: sqlite3.Connection, term: str, baseline_from: str, baseline_to: str, trend_from: str, trend_to: str, data_mode: str = "auto") -> dict[str, Any] | None:
    mode = lifecycle_data_mode(conn, data_mode)
    baseline_from_m = baseline_from[:7]
    baseline_to_m = baseline_to[:7]
    trend_from_m = trend_from[:7]
    trend_to_m = trend_to[:7]
    rows = conn.execute(
        """
        SELECT month, raw_freq, weighted_freq
        FROM term_month_stats
        WHERE term = ? AND corpus_scope = ? AND data_mode = ? AND month >= ? AND month <= ?
        ORDER BY month
        """,
        (term, BASELINE_SCOPE, mode, baseline_from_m, baseline_to_m),
    ).fetchall()
    if not rows:
        return None
    raw_by_month = {row["month"]: int(row["raw_freq"] or 0) for row in rows}
    weighted_by_month = {row["month"]: float(row["weighted_freq"] or 0) for row in rows}
    baseline_months = month_range(baseline_from_m, baseline_to_m)
    trend_months = month_range(trend_from_m, trend_to_m)
    raw_values = [raw_by_month.get(month, 0) for month in baseline_months]
    weighted_values = [weighted_by_month.get(month, 0.0) for month in baseline_months]
    nonzero_months = [month for month in baseline_months if raw_by_month.get(month, 0) > 0]
    if not nonzero_months:
        return None
    historical_first_seen = nonzero_months[0]
    trend_nonzero_months = [month for month in trend_months if raw_by_month.get(month, 0) > 0]
    trend_first_seen = trend_nonzero_months[0] if trend_nonzero_months else None
    baseline_total_count = int(sum(raw_values))
    historical_count_before_trend = int(sum(raw_by_month.get(month, 0) for month in baseline_months if month < trend_from_m))
    trend_total_count = int(sum(raw_by_month.get(month, 0) for month in trend_months))
    novelty_score = 1.0 / (1.0 + historical_count_before_trend)
    peak_value = max(weighted_values)
    peak_idx = weighted_values.index(peak_value)
    peak_month = baseline_months[peak_idx]
    half_life = half_life_months(baseline_months, weighted_values, peak_idx, peak_value)
    activity_threshold = max(1.0, peak_value * 0.2)
    active_indices = [idx for idx, value in enumerate(weighted_values) if value >= activity_threshold]
    active_duration = active_indices[-1] - active_indices[0] + 1 if active_indices else 0
    sigma = pstdev(weighted_values) if len(weighted_values) > 1 else 0.0
    burst_threshold = mean(weighted_values) + sigma
    burst_duration = longest_run(value > burst_threshold for value in weighted_values)
    term_types = term_type_set(conn, term, mode)
    concept_class = classify_concept(term, term_types)
    is_platform = concept_class == "platform_material" or term in PLATFORM_MATERIALS or is_platform_material(term)
    status = classify_status(raw_by_month, weighted_by_month, trend_months, peak_value, historical_count_before_trend, trend_total_count, sum(1 for value in raw_values if value > 0), concept_class, is_platform)
    return {"concept": term, "historical_first_seen": historical_first_seen, "trend_first_seen": trend_first_seen, "peak_month": peak_month, "peak_value": peak_value, "half_life_months": half_life, "active_duration_months": active_duration, "burst_duration_months": burst_duration, "baseline_total_count": baseline_total_count, "historical_count_before_trend": historical_count_before_trend, "trend_total_count": trend_total_count, "novelty_score": novelty_score, "concept_class": concept_class, "is_historical": historical_count_before_trend > 0, "is_platform_term": is_platform, "status": status}


def term_type_set(conn: sqlite3.Connection, term: str, data_mode: str = "auto") -> set[str]:
    mode = lifecycle_data_mode(conn, data_mode)
    return {
        row["term_type"]
        for row in conn.execute(
            """
            SELECT DISTINCT pt.term_type
            FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
            WHERE pt.normalized_term = ? AND (? = 'mixed' OR p.data_mode = ?)
            """,
            (term, mode, mode),
        ).fetchall()
    }

# --- Corpus-preset aware strict condensed matter overrides. ---
def lifecycle_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
    requested = (requested or "auto").lower()
    if requested == "strict_condmat":
        return "strict_condmat"
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    if requested == "mixed":
        return "mixed"
    if requested == "real" and real_count:
        return "real"
    if requested == "mock" and mock_count:
        return "mock"
    if real_count:
        return "real"
    if mock_count:
        return "mock"
    return "real"


def term_type_set(conn: sqlite3.Connection, term: str, data_mode: str = "auto") -> set[str]:
    mode = lifecycle_data_mode(conn, data_mode)
    if mode == "strict_condmat":
        where = "p.data_mode = 'real' AND COALESCE(p.condmat_view_eligible, 0) = 1"
        params: tuple[Any, ...] = (term,)
    else:
        where = "(? = 'mixed' OR p.data_mode = ?)"
        params = (term, mode, mode)
    return {
        row["term_type"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT pt.term_type
            FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
            WHERE pt.normalized_term = ? AND {where}
            """,
            params,
        ).fetchall()
    }
# --- Extended corpus-preset lifecycle helpers. ---
from backend.db.strict_condmat import VALID_CORPUS_PRESETS, corpus_preset_predicate, preset_for_stats_mode, stats_mode_for_preset


def lifecycle_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
    requested = (requested or "auto").lower()
    if requested == "auto" or requested == "strict_condmat":
        return stats_mode_for_preset("strict_core_published")
    known = {stats_mode_for_preset(preset) for preset in VALID_CORPUS_PRESETS} | {"mixed", "real", "mock"}
    if requested in known:
        return requested
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    if real_count:
        return stats_mode_for_preset("strict_core_published")
    if mock_count:
        return "mock"
    return stats_mode_for_preset("strict_core_published")


def term_type_set(conn: sqlite3.Connection, term: str, data_mode: str = "auto") -> set[str]:
    mode = lifecycle_data_mode(conn, data_mode)
    preset = preset_for_stats_mode(mode)
    if preset:
        where, params = corpus_preset_predicate(preset, "p")
        query_params: tuple[Any, ...] = (term, *params)
    elif mode == "mixed":
        where = "1=1"
        query_params = (term,)
    else:
        where = "p.data_mode = ?"
        query_params = (term, mode)
    return {
        row["term_type"]
        for row in conn.execute(
            f"""
            SELECT DISTINCT pt.term_type
            FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
            WHERE pt.normalized_term = ? AND {where}
            """,
            query_params,
        ).fetchall()
    }
