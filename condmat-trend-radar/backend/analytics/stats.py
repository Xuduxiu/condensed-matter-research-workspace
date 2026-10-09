
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from datetime import date
from typing import Any, Callable, Iterable

from backend.db.database import db_path, export_dir, replace_terms
from backend.nlp.dictionaries import CONTEXT_JOURNALS, CORE_JOURNALS, JOURNAL_WEIGHTS
from backend.nlp.extract_terms import extract_terms
from backend.nlp.normalize import month_range, normalize_term
from backend.nlp.term_filters import display_eligibility

SCOPE_CORE = "core"
SCOPE_CORE_CONTEXT = "core_context"
SCOPE_ALL = "all"
SCOPES = (SCOPE_CORE, SCOPE_CORE_CONTEXT, SCOPE_ALL)
METRICS = {"raw_freq", "weighted_freq", "momentum", "normalized_share", "weighted_normalized_share"}


def journal_weight(journal: str | None, source: str | None = None) -> float:
    if source == "arxiv" or journal == "arXiv":
        return 1.0
    return JOURNAL_WEIGHTS.get(journal or "", 1.5)


def paper_bucket(journal: str | None, source: str | None = None) -> str:
    if source == "arxiv" or journal == "arXiv":
        return "arxiv"
    if journal in CORE_JOURNALS:
        return "core"
    if journal in CONTEXT_JOURNALS:
        return "context"
    return "context"


def included_scopes(journal: str | None, source: str | None = None) -> tuple[str, ...]:
    bucket = paper_bucket(journal, source)
    if bucket == "core":
        return (SCOPE_CORE, SCOPE_CORE_CONTEXT, SCOPE_ALL)
    if bucket == "context":
        return (SCOPE_CORE_CONTEXT, SCOPE_ALL)
    return (SCOPE_ALL,)


def paper_scope_predicate(scope: str, alias: str = "p") -> tuple[str, list[Any]]:
    journal_col = f"{alias}.journal"
    if scope == SCOPE_CORE:
        return f"{journal_col} IN ({placeholders(CORE_JOURNALS)})", list(CORE_JOURNALS)
    if scope == SCOPE_CORE_CONTEXT:
        journals = CORE_JOURNALS + CONTEXT_JOURNALS
        return f"{journal_col} IN ({placeholders(journals)})", list(journals)
    return "1=1", []


def placeholders(items: list[Any] | tuple[Any, ...]) -> str:
    return ",".join("?" for _ in items)


def metric_sql(metric: str) -> str:
    return metric if metric in METRICS else "momentum"


def rebuild_paper_terms(conn: sqlite3.Connection) -> int:
    rows = conn.execute("SELECT id, title, abstract FROM papers").fetchall()
    for row in rows:
        replace_terms(conn, row["id"], extract_terms(row["title"], row["abstract"]))
    return len(rows)


def rebuild_monthly_corpus_stats(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM monthly_corpus_stats")
    rows = conn.execute("SELECT id, month, journal, source FROM papers WHERE month IS NOT NULL").fetchall()
    grouped: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"total_papers": 0, "total_weighted_papers": 0.0})
    for row in rows:
        for scope in included_scopes(row["journal"], row["source"]):
            key = (row["month"], scope)
            grouped[key]["total_papers"] += 1
            grouped[key]["total_weighted_papers"] += journal_weight(row["journal"], row["source"])
    for (month, scope), item in grouped.items():
        conn.execute(
            "INSERT INTO monthly_corpus_stats (month, corpus_scope, total_papers, total_weighted_papers) VALUES (?, ?, ?, ?)",
            (month, scope, int(item["total_papers"]), float(item["total_weighted_papers"])),
        )
    return len(grouped)


def rebuild_term_month_stats(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM term_month_stats")
    rebuild_monthly_corpus_stats(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT pt.normalized_term AS term, pt.term_type, pt.confidence,
          pt.display_eligible, pt.display_reason, p.id AS paper_id, p.month,
          p.journal, p.source, p.cited_by_count
        FROM paper_terms pt
        JOIN papers p ON p.id = pt.paper_id
        WHERE p.month IS NOT NULL AND pt.term_type IN ('concept', 'material', 'method')
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    term_meta: dict[str, dict[str, Any]] = defaultdict(lambda: {"types": set(), "eligible": 0, "reasons": defaultdict(int), "max_confidence": 0.0})
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        term = row["term"]
        term_meta[term]["types"].add(row["term_type"])
        term_meta[term]["eligible"] = max(int(term_meta[term]["eligible"]), int(row["display_eligible"] or 0))
        term_meta[term]["reasons"][row["display_reason"] or "unknown"] += 1
        term_meta[term]["max_confidence"] = max(float(term_meta[term]["max_confidence"]), float(row["confidence"] or 0.0))
        for scope in included_scopes(row["journal"], row["source"]):
            dedupe_key = (term, row["paper_id"], scope)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            key = (term, row["month"], scope)
            item = grouped.setdefault(key, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
            item["raw_freq"] += 1
            item["weighted_freq"] += journal_weight(row["journal"], row["source"])
            item["journals"][row["journal"] or "Unknown"] += 1
            item["citation_values"].append(float(row["cited_by_count"] or 0))
    corpus_totals = {(row["month"], row["corpus_scope"]): dict(row) for row in conn.execute("SELECT * FROM monthly_corpus_stats").fetchall()}
    global_start = min_month(conn)
    global_end = max_month(conn)
    if not global_start or not global_end:
        return 0
    all_months = month_range(global_start, global_end)
    by_term_scope: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for (term, month, scope), item in grouped.items():
        by_term_scope[(term, scope)][month] = item
    inserted = 0
    for (term, scope), series in by_term_scope.items():
        display_eligible, display_reason = display_flag_for_term(term, term_meta.get(term, {}))
        for month in all_months:
            item = series.get(month, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
            recent = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 3))
            previous = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 6, 3))
            growth_factor = min((recent + 1.0) / (previous + 1.0), 4.0)
            avg_citations = sum(item["citation_values"]) / len(item["citation_values"]) if item["citation_values"] else 0.0
            citation_signal = min(math.log1p(avg_citations), 3.0)
            weighted_freq = float(item["weighted_freq"])
            momentum = math.log1p(weighted_freq) * growth_factor * (1.0 + 0.35 * citation_signal)
            totals = corpus_totals.get((month, scope), {"total_papers": 0, "total_weighted_papers": 0.0})
            total_papers = int(totals.get("total_papers") or 0)
            total_weighted = float(totals.get("total_weighted_papers") or 0.0)
            conn.execute(
                """
                INSERT INTO term_month_stats
                  (term, month, corpus_scope, raw_freq, weighted_freq, normalized_share,
                   weighted_normalized_share, momentum, journal_breakdown, citation_signal,
                   display_eligible, display_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    term, month, scope, int(item["raw_freq"]), weighted_freq,
                    float(item["raw_freq"]) / total_papers if total_papers else 0.0,
                    weighted_freq / total_weighted if total_weighted else 0.0,
                    float(momentum), json.dumps(dict(item["journals"]), ensure_ascii=False),
                    citation_signal, display_eligible, display_reason,
                ),
            )
            inserted += 1
    return inserted


def display_flag_for_term(term: str, meta: dict[str, Any]) -> tuple[int, str]:
    if meta.get("eligible"):
        reason_counts = meta.get("reasons") or {}
        reason = max(reason_counts, key=reason_counts.get) if reason_counts else "extractor"
        return 1, str(reason)
    for term_type in meta.get("types") or {"concept"}:
        eligible, reason = display_eligibility(term, term_type, float(meta.get("max_confidence") or 1.0))
        if eligible:
            return eligible, reason
    return 0, "filtered"


def previous_months(months: list[str], current: str, width: int, offset: int = 0) -> list[str]:
    idx = months.index(current)
    end = max(0, idx - offset + 1)
    start = max(0, end - width)
    return months[start:end]


def latest_windows(conn: sqlite3.Connection) -> dict[str, str | int]:
    row = conn.execute(
        """
        SELECT baseline_from, baseline_to, trend_from, trend_to, trend_months, scope
        FROM update_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row and row["trend_to"]:
        return {
            "baseline_from": row["baseline_from"] or min_month(conn) or "2015-01",
            "baseline_to": row["baseline_to"] or max_month(conn) or date.today().isoformat()[:7],
            "trend_from": row["trend_from"] or shift_month(row["trend_to"][:7], -23),
            "trend_to": row["trend_to"] or max_month(conn) or date.today().isoformat()[:7],
            "trend_months": row["trend_months"] or 24,
            "scope": row["scope"] or SCOPE_CORE,
        }
    end_month = max_month(conn) or date.today().isoformat()[:7]
    return {"baseline_from": min_month(conn) or "2015-01", "baseline_to": end_month, "trend_from": shift_month(end_month, -23), "trend_to": end_month, "trend_months": 24, "scope": SCOPE_CORE}


def min_month(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MIN(month) AS month FROM papers WHERE month IS NOT NULL").fetchone()
    return row["month"] if row else None


def max_month(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(month) AS month FROM papers WHERE month IS NOT NULL").fetchone()
    return row["month"] if row else None


def shift_month(month: str, delta: int) -> str:
    year, mon = [int(part) for part in month[:7].split("-")]
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def overview(conn: sqlite3.Connection, scope: str = SCOPE_CORE, trend_from: str | None = None, trend_to: str | None = None) -> dict[str, Any]:
    windows = latest_windows(conn)
    scope = scope or str(windows["scope"] or SCOPE_CORE)
    trend_from = trend_from or str(windows["trend_from"])
    trend_to = trend_to or str(windows["trend_to"])
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    total = conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {predicate} AND p.month >= ? AND p.month <= ?", (*predicate_params, trend_from[:7], trend_to[:7])).fetchone()["n"]
    latest_month = conn.execute(f"SELECT MAX(p.month) AS month FROM papers p WHERE {predicate} AND p.month >= ? AND p.month <= ?", (*predicate_params, trend_from[:7], trend_to[:7])).fetchone()["month"]
    current_month = conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {predicate} AND p.month = ?", (*predicate_params, latest_month)).fetchone()["n"] if latest_month else 0
    return {
        "total_papers": total,
        "latest_month": latest_month,
        "this_month_papers": current_month,
        "window": {"baseline_from": str(windows["baseline_from"])[:7], "baseline_to": str(windows["baseline_to"])[:7], "trend_from": trend_from[:7], "trend_to": trend_to[:7], "scope": scope},
        "data_source": data_source_summary(conn),
        "corpus_size": corpus_size_summary(conn),
        "hot_concepts": top_terms(conn, "momentum", 20, scope, trend_from, trend_to),
        "rising_concepts": rising_terms(conn, 20, scope, trend_from, trend_to),
        "cooling_concepts": cooling_terms(conn, 20, scope, trend_from, trend_to),
        "top_materials": top_terms_by_type(conn, "material", 16, scope, trend_from, trend_to),
        "top_methods": top_terms_by_type(conn, "method", 16, scope, trend_from, trend_to),
        "journal_distribution": journal_distribution(conn, scope, trend_from, trend_to),
        "source_distribution": source_distribution(conn, scope, trend_from, trend_to),
    }


def data_source_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    source_rows = conn.execute("SELECT data_mode, source, COUNT(*) AS n FROM papers GROUP BY data_mode, source ORDER BY data_mode, n DESC").fetchall()
    status = data_status(conn)
    return {
        "is_mock": status["data_mode"] == "mock",
        "label": "Mock 示例数据" if status["data_mode"] == "mock" else ("Mixed metadata" if status["data_mode"] == "mixed" else "Real metadata"),
        "data_mode": status["data_mode"],
        "sources": [dict(item) for item in source_rows],
    }


def data_status(conn: sqlite3.Connection, scope: str = SCOPE_CORE) -> dict[str, Any]:
    windows = latest_windows(conn)
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode = 'real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode = 'mock'").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    if real_count and mock_count:
        mode = "mixed"
    elif real_count:
        mode = "real"
    elif mock_count:
        mode = "mock"
    else:
        mode = "real"
    last_real = conn.execute("SELECT MAX(updated_at) AS ts FROM papers WHERE data_mode = 'real'").fetchone()["ts"]
    last_mock = conn.execute("SELECT MAX(updated_at) AS ts FROM papers WHERE data_mode = 'mock'").fetchone()["ts"]
    return {
        "data_mode": mode,
        "db_path": str(db_path()),
        "paper_count": total,
        "real_paper_count": real_count,
        "mock_paper_count": mock_count,
        "last_real_ingest": last_real,
        "last_mock_ingest": last_mock,
        "scope": scope,
        "baseline_from": str(windows["baseline_from"]),
        "baseline_to": str(windows["baseline_to"]),
    }


def corpus_size_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    estimates = load_latest_estimate()
    return {
        "database_path": str(db_path()),
        "total_papers": conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"],
        "real_paper_count": conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"],
        "mock_paper_count": conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"],
        "concept_count": conn.execute("SELECT COUNT(DISTINCT normalized_term) AS n FROM paper_terms WHERE term_type='concept' AND display_eligible=1").fetchone()["n"],
        "material_count": conn.execute("SELECT COUNT(DISTINCT normalized_term) AS n FROM paper_terms WHERE term_type='material'").fetchone()["n"],
        "method_count": conn.execute("SELECT COUNT(DISTINCT normalized_term) AS n FROM paper_terms WHERE term_type='method'").fetchone()["n"],
        "estimated_full_corpus": estimates.get("estimated_total_works") if estimates else None,
    }

def load_latest_estimate() -> dict[str, Any]:
    path = export_dir() / "corpus_count_estimate.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def top_terms(conn: sqlite3.Connection, metric: str = "momentum", limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None) -> list[dict[str, Any]]:
    metric_col = metric_sql(metric)
    where = ["t.corpus_scope = ?", "t.display_eligible = 1", "COALESCE(l.concept_class, 'physics_concept') = 'physics_concept'"]
    params: list[Any] = [scope]
    if from_month:
        where.append("t.month >= ?")
        params.append(from_month[:7])
    if to_month:
        where.append("t.month <= ?")
        params.append(to_month[:7])
    return [dict(row) for row in conn.execute(
        f"""
        SELECT t.term, SUM(t.raw_freq) AS raw_freq, SUM(t.weighted_freq) AS weighted_freq,
               SUM(t.momentum) AS momentum, AVG(t.normalized_share) AS normalized_share,
               AVG(t.weighted_normalized_share) AS weighted_normalized_share
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE {" AND ".join(where)}
        GROUP BY t.term HAVING SUM(t.raw_freq) >= 2
        ORDER BY SUM(t.{metric_col}) DESC LIMIT ?
        """, (*params, limit))]


def top_terms_by_type(conn: sqlite3.Connection, term_type: str, limit: int, scope: str, from_month: str, to_month: str) -> list[dict[str, Any]]:
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    return [dict(row) for row in conn.execute(
        f"""
        SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS raw_freq
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE pt.term_type = ? AND pt.display_eligible = 1 AND {predicate} AND p.month >= ? AND p.month <= ?
        GROUP BY pt.normalized_term ORDER BY raw_freq DESC, term ASC LIMIT ?
        """, (term_type, *predicate_params, from_month[:7], to_month[:7], limit))]


def rising_terms(conn: sqlite3.Connection, limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None) -> list[dict[str, Any]]:
    months = scoped_months(conn, scope, from_month, to_month)
    return _growth_table(conn, set(months[-3:]), set(months[-6:-3]), limit, True, scope)


def cooling_terms(conn: sqlite3.Connection, limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None) -> list[dict[str, Any]]:
    months = scoped_months(conn, scope, from_month, to_month)
    return _growth_table(conn, set(months[-3:]), set(months[-6:-3]), limit, False, scope)


def scoped_months(conn: sqlite3.Connection, scope: str, from_month: str | None, to_month: str | None) -> list[str]:
    where = ["corpus_scope = ?"]
    params: list[Any] = [scope]
    if from_month:
        where.append("month >= ?")
        params.append(from_month[:7])
    if to_month:
        where.append("month <= ?")
        params.append(to_month[:7])
    return [row["month"] for row in conn.execute(f"SELECT DISTINCT month FROM term_month_stats WHERE {' AND '.join(where)} ORDER BY month", tuple(params))]


def _growth_table(conn: sqlite3.Connection, recent_months: set[str], previous_months_set: set[str], limit: int, descending: bool, scope: str) -> list[dict[str, Any]]:
    if not recent_months or not previous_months_set:
        return []
    rows = conn.execute(
        """
        SELECT t.term, t.month, SUM(t.weighted_freq) AS weighted_freq, SUM(t.raw_freq) AS raw_freq
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE t.corpus_scope = ? AND t.display_eligible = 1 AND COALESCE(l.concept_class, 'physics_concept') = 'physics_concept'
        GROUP BY t.term, t.month
        """, (scope,)).fetchall()
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"recent": 0.0, "previous": 0.0, "raw": 0.0})
    for row in rows:
        if row["month"] in recent_months:
            stats[row["term"]]["recent"] += float(row["weighted_freq"])
            stats[row["term"]]["raw"] += float(row["raw_freq"])
        elif row["month"] in previous_months_set:
            stats[row["term"]]["previous"] += float(row["weighted_freq"])
    results = []
    for term, values in stats.items():
        growth = (values["recent"] + 1.0) / (values["previous"] + 1.0)
        if descending and values["recent"] < 1:
            continue
        if not descending and values["previous"] < 1:
            continue
        results.append({"term": term, "recent_weighted_freq": values["recent"], "previous_weighted_freq": values["previous"], "growth": growth, "raw_freq": values["raw"]})
    results.sort(key=lambda item: item["growth"], reverse=descending)
    return results[:limit]


def journal_distribution(conn: sqlite3.Connection, scope: str, from_month: str, to_month: str) -> list[dict[str, Any]]:
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    return [dict(row) for row in conn.execute(f"SELECT p.journal, COUNT(*) AS count FROM papers p WHERE {predicate} AND p.month >= ? AND p.month <= ? GROUP BY p.journal ORDER BY count DESC", (*predicate_params, from_month[:7], to_month[:7]))]


def source_distribution(conn: sqlite3.Connection, scope: str, from_month: str, to_month: str) -> list[dict[str, Any]]:
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    return [dict(row) for row in conn.execute(f"SELECT p.source, COUNT(*) AS count FROM papers p WHERE {predicate} AND p.month >= ? AND p.month <= ? GROUP BY p.source ORDER BY count DESC", (*predicate_params, from_month[:7], to_month[:7]))]


def heatmap(
    conn: sqlite3.Connection,
    metric: str = "momentum",
    journal: str = "all",
    from_month: str | None = None,
    to_month: str | None = None,
    limit: int = 40,
    scope: str = SCOPE_CORE,
    concept_class: str = "all",
    exclude_methods: bool = False,
    exclude_platform_materials: bool = False,
    min_count: int = 2,
) -> dict[str, Any]:
    windows = latest_windows(conn)
    from_month = (from_month or str(windows["trend_from"]))[:7]
    to_month = (to_month or str(windows["trend_to"]))[:7]
    metric_col = metric_sql(metric)
    where = ["t.corpus_scope = ?", "t.display_eligible = 1", "t.month >= ?", "t.month <= ?"]
    params: list[Any] = [scope, from_month, to_month]
    if concept_class and concept_class != "all":
        where.append("COALESCE(l.concept_class, '') = ?")
        params.append(concept_class)
    if exclude_methods:
        where.append("COALESCE(l.concept_class, '') != 'method'")
    if exclude_platform_materials:
        where.append("COALESCE(l.concept_class, '') != 'platform_material'")
        where.append("COALESCE(l.is_platform_term, 0) = 0")
    top = [row["term"] for row in conn.execute(
        f"""
        SELECT t.term, SUM(t.{metric_col}) AS score, SUM(t.raw_freq) AS raw_total
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE {" AND ".join(where)}
        GROUP BY t.term HAVING raw_total >= ? ORDER BY score DESC LIMIT ?
        """, (*params, max(1, min_count), limit))]
    if not top:
        return {"months": [], "terms": [], "values": [], "metric": metric_col, "scope": scope}
    rows = conn.execute(
        f"""
        SELECT term, month, raw_freq, weighted_freq, normalized_share, weighted_normalized_share, momentum
        FROM term_month_stats WHERE corpus_scope = ? AND term IN ({placeholders(top)}) AND month >= ? AND month <= ?
        ORDER BY month, term
        """, (scope, *top, from_month, to_month)).fetchall()
    months = month_range(from_month, to_month)
    month_index = {month: idx for idx, month in enumerate(months)}
    term_index = {term: idx for idx, term in enumerate(top)}
    values = [[month_index[row["month"]], term_index[row["term"]], float(row[metric_col])] for row in rows if row["month"] in month_index and row["term"] in term_index and float(row[metric_col]) > 0]
    return {"months": months, "terms": top, "values": values, "metric": metric_col, "journal": journal, "scope": scope}

def concept_series(conn: sqlite3.Connection, concept: str, scope: str = SCOPE_ALL, from_month: str | None = None, to_month: str | None = None, smoothing: str = "raw") -> list[dict[str, Any]]:
    windows = latest_windows(conn)
    from_month = (from_month or str(windows["baseline_from"]))[:7]
    to_month = (to_month or str(windows["baseline_to"]))[:7]
    rows = conn.execute(
        """
        SELECT month, raw_freq, weighted_freq, normalized_share, weighted_normalized_share, momentum
        FROM term_month_stats WHERE term = ? AND corpus_scope = ? AND month >= ? AND month <= ? ORDER BY month
        """, (concept, scope, from_month, to_month)).fetchall()
    by_month = {row["month"]: dict(row) for row in rows}
    output = []
    for month in month_range(from_month, to_month):
        item = by_month.get(month, {"month": month, "raw_freq": 0, "weighted_freq": 0.0, "normalized_share": 0.0, "weighted_normalized_share": 0.0, "momentum": 0.0})
        output.append({"month": month, "raw_freq": item["raw_freq"], "weighted_freq": item["weighted_freq"], "normalized_share": item["normalized_share"], "weighted_normalized_share": item["weighted_normalized_share"], "momentum": item["momentum"]})
    return smooth_series(output, smoothing)


def smooth_series(series: list[dict[str, Any]], smoothing: str) -> list[dict[str, Any]]:
    if smoothing == "raw":
        return series
    keys = ["raw_freq", "weighted_freq", "normalized_share", "weighted_normalized_share", "momentum"]
    smoothed = [dict(item) for item in series]
    for key in keys:
        values = [float(item[key]) for item in series]
        new_values = smooth_values(values, smoothing)
        for idx, value in enumerate(new_values):
            smoothed[idx][key] = value
    return smoothed


def smooth_values(values: list[float], smoothing: str) -> list[float]:
    if smoothing == "rolling3":
        width = 3
    elif smoothing == "rolling6":
        width = 6
    elif smoothing == "ewma":
        alpha = 0.35
        output = []
        prev = 0.0
        for idx, value in enumerate(values):
            prev = value if idx == 0 else alpha * value + (1 - alpha) * prev
            output.append(prev)
        return output
    else:
        return values
    return [sum(values[max(0, idx - width + 1): idx + 1]) / len(values[max(0, idx - width + 1): idx + 1]) for idx in range(len(values))]


def compare_data(conn: sqlite3.Connection, concepts: list[str], metric: str, from_month: str, to_month: str, smoothing: str, scope: str) -> dict[str, Any]:
    metric_col = metric_sql(metric if metric != "z_score" else "weighted_freq")
    normalized_terms = [normalize_term(item) for item in concepts if item.strip()][:8]
    series_payload = []
    for concept in normalized_terms:
        rows = concept_series(conn, concept, scope=scope, from_month=from_month, to_month=to_month, smoothing="raw")
        values = [float(row[metric_col]) for row in rows]
        if metric == "z_score":
            avg = sum(values) / len(values) if values else 0.0
            variance = sum((value - avg) ** 2 for value in values) / len(values) if values else 0.0
            denom = math.sqrt(variance) or 1.0
            values = [(value - avg) / denom for value in values]
        values = smooth_values(values, smoothing)
        series_payload.append({"concept": concept, "points": [{"month": row["month"], "value": values[idx]} for idx, row in enumerate(rows)]})
    table = []
    if normalized_terms:
        table = [dict(row) for row in conn.execute(f"SELECT * FROM concept_lifecycle WHERE concept IN ({placeholders(normalized_terms)}) ORDER BY concept", normalized_terms)]
    return {"metric": metric, "scope": scope, "smoothing": smoothing, "series": series_payload, "table": table}


def concept_search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[dict[str, Any]]:
    needle = f"%{query}%"
    return [dict(row) for row in conn.execute("SELECT concept, concept_class, status FROM concept_lifecycle WHERE concept LIKE ? ORDER BY trend_total_count DESC, concept LIMIT ?", (needle, limit))]


def papers_query(conn: sqlite3.Connection, query: str = "", journal: str = "", concept: str = "", from_date: str = "", to_date: str = "", limit: int = 250, scope: str = SCOPE_CORE) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    joins = ""
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    where.append(predicate)
    params.extend(predicate_params)
    if query:
        where.append("(p.title LIKE ? OR p.abstract LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if journal:
        where.append("p.journal = ?")
        params.append(journal)
    if from_date:
        where.append("p.publication_date >= ?")
        params.append(from_date if len(from_date) > 7 else f"{from_date[:7]}-01")
    if to_date:
        where.append("p.publication_date <= ?")
        params.append(to_date if len(to_date) > 7 else f"{to_date[:7]}-31")
    if concept:
        joins = "JOIN paper_terms filter_terms ON filter_terms.paper_id = p.id"
        where.append("filter_terms.normalized_term = ?")
        params.append(concept)
    rows = conn.execute(
        f"""
        SELECT p.*, COALESCE(SUM(tms.momentum), 0) AS momentum_score
        FROM papers p {joins}
        LEFT JOIN paper_terms pt ON pt.paper_id = p.id
        LEFT JOIN term_month_stats tms ON tms.term = pt.normalized_term AND tms.month = p.month AND tms.corpus_scope = ?
        WHERE {" AND ".join(where)}
        GROUP BY p.id ORDER BY p.publication_date DESC LIMIT ?
        """, (scope, *params, limit)).fetchall()
    return hydrate_papers(conn, rows)


def compare_papers_query(conn: sqlite3.Connection, concepts: list[str], mode: str, from_month: str, to_month: str, scope: str, limit: int = 250) -> list[dict[str, Any]]:
    normalized = [normalize_term(item) for item in concepts if item.strip()][:8]
    if not normalized:
        return []
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    comparator = "COUNT(DISTINCT pt.normalized_term) = ?" if mode == "and" else "COUNT(DISTINCT pt.normalized_term) >= 1"
    having_params: list[Any] = [len(normalized)] if mode == "and" else []
    rows = conn.execute(
        f"""
        SELECT p.*, COALESCE(SUM(tms.momentum), 0) AS momentum_score
        FROM papers p
        JOIN paper_terms pt ON pt.paper_id = p.id AND pt.normalized_term IN ({placeholders(normalized)})
        LEFT JOIN term_month_stats tms ON tms.term = pt.normalized_term AND tms.month = p.month AND tms.corpus_scope = ?
        WHERE {predicate} AND p.month >= ? AND p.month <= ?
        GROUP BY p.id HAVING {comparator}
        ORDER BY p.publication_date DESC LIMIT ?
        """, (*normalized, scope, *predicate_params, from_month[:7], to_month[:7], *having_params, limit)).fetchall()
    return hydrate_papers(conn, rows)


def hydrate_papers(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        item = dict(row)
        terms = conn.execute("SELECT term_type, normalized_term FROM paper_terms WHERE paper_id = ? ORDER BY confidence DESC, normalized_term", (item["id"],)).fetchall()
        item["concepts"] = [term["normalized_term"] for term in terms if term["term_type"] == "concept"]
        item["materials"] = [term["normalized_term"] for term in terms if term["term_type"] == "material"]
        item["methods"] = [term["normalized_term"] for term in terms if term["term_type"] == "method"]
        output.append(item)
    return output


def download_tasks(conn: sqlite3.Connection, concept: str = "", from_month: str = "", to_month: str = "", scope: str = SCOPE_ALL, limit: int = 1000) -> list[dict[str, Any]]:
    concept_key = normalize_term(concept) if concept else ""
    papers = papers_query(conn, concept=concept_key, from_date=from_month, to_date=to_month, limit=limit, scope=scope)
    return build_download_tasks(papers, selected_concepts=[concept_key] if concept_key else [])


def build_download_tasks(papers: list[dict[str, Any]], selected_concepts: list[str] | None = None) -> list[dict[str, Any]]:
    selected = [normalize_term(item) for item in (selected_concepts or []) if item]
    momentum_values = sorted((float(paper.get("momentum_score") or paper.get("momentum") or 0.0) for paper in papers), reverse=True)
    top5 = momentum_values[max(0, int(len(momentum_values) * 0.05) - 1)] if momentum_values else 0.0
    top20 = momentum_values[max(0, int(len(momentum_values) * 0.20) - 1)] if momentum_values else 0.0
    output = []
    for paper in papers:
        raw = safe_json(paper.get("raw_json"))
        paper_id = str(paper.get("id", ""))
        arxiv_id = paper.get("arxiv_id") or raw.get("arxiv_id") or (paper_id.removeprefix("arxiv:") if paper_id.startswith("arxiv:") else "")
        pdf_url = paper.get("pdf_url") or raw.get("pdf_url") or ""
        openalex_id = paper.get("openalex_id") or (paper_id if paper_id.startswith("https://openalex.org/") else raw.get("id", ""))
        momentum = float(paper.get("momentum_score") or paper.get("momentum") or 0.0)
        terms = list(dict.fromkeys(paper.get("concepts", []) + paper.get("materials", []) + paper.get("methods", [])))
        matched = [term for term in selected if term and term in terms]
        priority = 0
        reasons: list[str] = []
        journal = paper.get("journal") or ""
        source = paper.get("source") or ""
        if journal in CORE_JOURNALS:
            priority += 30
            reasons.append("core journal")
        elif journal in CONTEXT_JOURNALS:
            priority += 15
            reasons.append("context journal")
        elif source == "arxiv" or journal == "arXiv":
            priority += 5
            reasons.append("arXiv only")
        if matched:
            priority += 20 + max(0, len(matched) - 1) * 10
            reasons.append("matched " + ", ".join(matched))
        if momentum_values:
            if momentum >= top5:
                priority += 30
                reasons.append("top momentum")
            elif momentum >= top20:
                priority += 15
                reasons.append("high momentum")
            else:
                priority += 5
                reasons.append("nonzero momentum")
        pub_month = str(paper.get("publication_date") or "")[:7]
        now_month = date.today().isoformat()[:7]
        if pub_month >= shift_month(now_month, -12):
            priority += 10
            reasons.append("recent 12 months")
        elif pub_month >= shift_month(now_month, -36):
            priority += 5
            reasons.append("recent 36 months")
        if paper.get("doi"):
            priority += 10
            reasons.append("has DOI")
        if arxiv_id:
            priority += 5
            reasons.append("has arxiv_id")
        if pdf_url:
            priority += 10
            reasons.append("has pdf_url")
        priority = max(0, min(100, priority))
        task_seed = "|".join([str(paper.get("doi") or ""), str(arxiv_id or ""), str(paper.get("title") or "")])
        task_id = "trend:" + hashlib.sha1(task_seed.encode("utf-8")).hexdigest()[:16]
        output.append({
            "task_id": task_id,
            "doi": paper.get("doi") or "",
            "arxiv_id": arxiv_id or "",
            "title": paper.get("title") or "",
            "journal": journal,
            "publication_date": paper.get("publication_date") or "",
            "url": paper.get("url") or "",
            "pdf_url": pdf_url,
            "openalex_id": openalex_id or "",
            "source": source,
            "concepts": "; ".join(paper.get("concepts", [])),
            "materials": "; ".join(paper.get("materials", [])),
            "methods": "; ".join(paper.get("methods", [])),
            "momentum": momentum,
            "download_priority": priority,
            "reason": "; ".join(reasons),
            "status": "pending",
        })
    output.sort(key=lambda item: (item["download_priority"], item["momentum"]), reverse=True)
    return output

def safe_json(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}




# --- Data-mode aware overrides for real/mock isolation. ---
def resolve_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
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


def paper_mode_predicate(data_mode: str, alias: str = "p") -> tuple[str, list[Any]]:
    if data_mode == "mixed":
        return "1=1", []
    return f"{alias}.data_mode = ?", [data_mode]


def rebuild_paper_terms(
    conn: sqlite3.Connection,
    paper_ids: Iterable[str] | None = None,
    *,
    batch_size: int = 250,
    progress_callback: Callable[[int, int], None] | None = None,
    commit_batches: bool = False,
) -> int:
    """Re-extract terms with bounded Python memory.

    The default preserves the historical caller-owned transaction: this
    function never commits unless ``commit_batches`` is explicitly enabled.
    Batch commits are safe at the per-paper level because ``replace_terms``
    completes each paper's delete/insert replacement before the checkpoint.
    Callers that require a whole-corpus atomic rebuild must keep the default.
    The optional callback runs after a complete batch and before its optional
    commit so progress metadata can be committed with the same checkpoint.
    """
    ids = list(dict.fromkeys(str(item) for item in (paper_ids or []) if item))
    if paper_ids is not None and not ids:
        return 0
    size = max(1, int(batch_size))
    where = ""
    params: list[Any] = []
    if ids:
        where = f" WHERE id IN ({','.join('?' for _ in ids)})"
        params.extend(ids)
    total_row = conn.execute(f"SELECT COUNT(*) AS n FROM papers{where}", params).fetchone()
    total = int(total_row["n"] if isinstance(total_row, sqlite3.Row) else total_row[0])
    cursor = conn.execute(
        f"SELECT id, title, abstract, condmat_confidence FROM papers{where} ORDER BY id",
        params,
    )
    processed = 0
    while True:
        rows = cursor.fetchmany(size)
        if not rows:
            break
        for row in rows:
            terms = extract_terms(row["title"], row["abstract"])
            if (row["condmat_confidence"] or "medium") in {"low", "excluded"}:
                for term in terms:
                    term["display_eligible"] = 0
                    term["display_reason"] = "low_condmat_confidence"
            replace_terms(conn, row["id"], terms)
        processed += len(rows)
        if progress_callback is not None:
            progress_callback(processed, total)
        if commit_batches:
            conn.commit()
    return processed

def rebuild_monthly_corpus_stats(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM monthly_corpus_stats")
    rows = conn.execute("SELECT id, month, journal, source, data_mode FROM papers WHERE month IS NOT NULL").fetchall()
    grouped: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: {"total_papers": 0, "total_weighted_papers": 0.0})
    for row in rows:
        modes = [row["data_mode"] or "real", "mixed"]
        for mode in modes:
            for scope in included_scopes(row["journal"], row["source"]):
                key = (row["month"], scope, mode)
                grouped[key]["total_papers"] += 1
                grouped[key]["total_weighted_papers"] += journal_weight(row["journal"], row["source"])
    for (month, scope, mode), item in grouped.items():
        conn.execute(
            "INSERT INTO monthly_corpus_stats (month, corpus_scope, data_mode, total_papers, total_weighted_papers) VALUES (?, ?, ?, ?, ?)",
            (month, scope, mode, int(item["total_papers"]), float(item["total_weighted_papers"])),
        )
    return len(grouped)


def rebuild_term_month_stats(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM term_month_stats")
    rebuild_monthly_corpus_stats(conn)
    rows = conn.execute(
        """
        SELECT DISTINCT pt.normalized_term AS term, pt.term_type, pt.confidence,
          pt.display_eligible, pt.display_reason, p.id AS paper_id, p.month,
          p.journal, p.source, p.data_mode, p.cited_by_count
        FROM paper_terms pt
        JOIN papers p ON p.id = pt.paper_id
        WHERE p.month IS NOT NULL AND pt.term_type IN ('concept', 'material', 'method')
        """
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    term_meta: dict[str, dict[str, Any]] = defaultdict(lambda: {"types": set(), "eligible": 0, "reasons": defaultdict(int), "max_confidence": 0.0})
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        term = row["term"]
        term_meta[term]["types"].add(row["term_type"])
        term_meta[term]["eligible"] = max(int(term_meta[term]["eligible"]), int(row["display_eligible"] or 0))
        term_meta[term]["reasons"][row["display_reason"] or "unknown"] += 1
        term_meta[term]["max_confidence"] = max(float(term_meta[term]["max_confidence"]), float(row["confidence"] or 0.0))
        for mode in (row["data_mode"] or "real", "mixed"):
            for scope in included_scopes(row["journal"], row["source"]):
                dedupe_key = (term, row["paper_id"], scope, mode)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                key = (term, row["month"], scope, mode)
                item = grouped.setdefault(key, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
                item["raw_freq"] += 1
                item["weighted_freq"] += journal_weight(row["journal"], row["source"])
                item["journals"][row["journal"] or "Unknown"] += 1
                item["citation_values"].append(float(row["cited_by_count"] or 0))
    corpus_totals = {(row["month"], row["corpus_scope"], row["data_mode"]): dict(row) for row in conn.execute("SELECT * FROM monthly_corpus_stats").fetchall()}
    global_start = min_month(conn)
    global_end = max_month(conn)
    if not global_start or not global_end:
        return 0
    all_months = month_range(global_start, global_end)
    by_term_scope_mode: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for (term, month, scope, mode), item in grouped.items():
        by_term_scope_mode[(term, scope, mode)][month] = item
    inserted = 0
    for (term, scope, mode), series in by_term_scope_mode.items():
        display_eligible, display_reason = display_flag_for_term(term, term_meta.get(term, {}))
        for month in all_months:
            item = series.get(month, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
            recent = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 3))
            previous = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 6, 3))
            growth_factor = min((recent + 1.0) / (previous + 1.0), 4.0)
            avg_citations = sum(item["citation_values"]) / len(item["citation_values"]) if item["citation_values"] else 0.0
            citation_signal = min(math.log1p(avg_citations), 3.0)
            weighted_freq = float(item["weighted_freq"])
            momentum = math.log1p(weighted_freq) * growth_factor * (1.0 + 0.35 * citation_signal)
            totals = corpus_totals.get((month, scope, mode), {"total_papers": 0, "total_weighted_papers": 0.0})
            total_papers = int(totals.get("total_papers") or 0)
            total_weighted = float(totals.get("total_weighted_papers") or 0.0)
            conn.execute(
                """
                INSERT INTO term_month_stats
                  (term, month, corpus_scope, data_mode, raw_freq, weighted_freq, normalized_share,
                   weighted_normalized_share, momentum, journal_breakdown, citation_signal,
                   display_eligible, display_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    term, month, scope, mode, int(item["raw_freq"]), weighted_freq,
                    float(item["raw_freq"]) / total_papers if total_papers else 0.0,
                    weighted_freq / total_weighted if total_weighted else 0.0,
                    float(momentum), json.dumps(dict(item["journals"]), ensure_ascii=False),
                    citation_signal, display_eligible, display_reason,
                ),
            )
            inserted += 1
    return inserted


def data_status(conn: sqlite3.Connection, scope: str = SCOPE_CORE) -> dict[str, Any]:
    windows = latest_windows(conn)
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode = 'real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode = 'mock'").fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    mode = "real" if real_count else "mock" if mock_count else "real"
    last_real = conn.execute("SELECT MAX(updated_at) AS ts FROM papers WHERE data_mode = 'real'").fetchone()["ts"]
    last_mock = conn.execute("SELECT MAX(updated_at) AS ts FROM papers WHERE data_mode = 'mock'").fetchone()["ts"]
    return {"data_mode": mode, "db_path": str(db_path()), "paper_count": total, "real_paper_count": real_count, "mock_paper_count": mock_count, "last_real_ingest": last_real, "last_mock_ingest": last_mock, "scope": scope, "baseline_from": str(windows["baseline_from"]), "baseline_to": str(windows["baseline_to"])}


def data_source_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    mode = resolve_data_mode(conn, data_mode)
    source_rows = conn.execute("SELECT data_mode, source, COUNT(*) AS n FROM papers GROUP BY data_mode, source ORDER BY data_mode, n DESC").fetchall()
    active_sources = [dict(row) for row in conn.execute("SELECT source, COUNT(*) AS n FROM papers WHERE (?='mixed' OR data_mode=?) GROUP BY source ORDER BY n DESC", (mode, mode)).fetchall()]
    source_names = {row["source"] for row in active_sources}
    if mode == "mock":
        label = "Mock 示例数据"
    elif source_names == {"openalex"}:
        label = "OpenAlex real metadata"
    elif source_names:
        label = "/".join(sorted(source_names)) + " real metadata"
    else:
        label = "Real metadata"
    return {"is_mock": mode == "mock", "label": label, "data_mode": mode, "sources": [dict(item) for item in source_rows], "active_sources": active_sources}


def corpus_size_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    estimates = load_latest_estimate()
    mode = resolve_data_mode(conn, data_mode)
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    return {
        "database_path": str(db_path()),
        "total_papers": conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"],
        "active_paper_count": conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "real_paper_count": conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"],
        "mock_paper_count": conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"],
        "concept_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='concept' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "material_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='material' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "method_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='method' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "estimated_full_corpus": estimates.get("estimated_total_works") if estimates else None,
    }


def overview(conn: sqlite3.Connection, scope: str = SCOPE_CORE, trend_from: str | None = None, trend_to: str | None = None, data_mode: str = "auto") -> dict[str, Any]:
    windows = latest_windows(conn)
    scope = scope or str(windows["scope"] or SCOPE_CORE)
    mode = resolve_data_mode(conn, data_mode)
    trend_from = trend_from or str(windows["trend_from"])
    trend_to = trend_to or str(windows["trend_to"])
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    params = [*predicate_params, *mode_params, trend_from[:7], trend_to[:7]]
    total = conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ?", tuple(params)).fetchone()["n"]
    latest_month = conn.execute(f"SELECT MAX(p.month) AS month FROM papers p WHERE {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ?", tuple(params)).fetchone()["month"]
    current_month = conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {predicate} AND {mode_sql} AND p.month = ?", (*predicate_params, *mode_params, latest_month)).fetchone()["n"] if latest_month else 0
    return {
        "total_papers": total,
        "latest_month": latest_month,
        "this_month_papers": current_month,
        "window": {"baseline_from": str(windows["baseline_from"])[:7], "baseline_to": str(windows["baseline_to"])[:7], "trend_from": trend_from[:7], "trend_to": trend_to[:7], "scope": scope},
        "data_source": data_source_summary(conn, mode),
        "corpus_size": corpus_size_summary(conn, mode),
        "hot_concepts": top_terms(conn, "momentum", 20, scope, trend_from, trend_to, mode),
        "rising_concepts": rising_terms(conn, 20, scope, trend_from, trend_to, mode),
        "cooling_concepts": cooling_terms(conn, 20, scope, trend_from, trend_to, mode),
        "top_materials": top_terms_by_type(conn, "material", 16, scope, trend_from, trend_to, mode),
        "top_methods": top_terms_by_type(conn, "method", 16, scope, trend_from, trend_to, mode),
        "journal_distribution": journal_distribution(conn, scope, trend_from, trend_to, mode),
        "source_distribution": source_distribution(conn, scope, trend_from, trend_to, mode),
    }


def top_terms(conn: sqlite3.Connection, metric: str = "momentum", limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None, data_mode: str = "auto") -> list[dict[str, Any]]:
    mode = resolve_data_mode(conn, data_mode)
    dense_months = scoped_months(conn, scope, from_month, to_month, mode)
    if not dense_months:
        return []
    metric_col = metric_sql(metric)
    where = ["t.corpus_scope = ?", "t.data_mode = ?", "t.display_eligible = 1", "COALESCE(l.concept_class, 'physics_concept') = 'physics_concept'"]
    params: list[Any] = [scope, mode]
    if from_month:
        where.append("t.month >= ?"); params.append(from_month[:7])
    if to_month:
        where.append("t.month <= ?"); params.append(to_month[:7])
    return [dict(row) for row in conn.execute(f"""
        SELECT t.term, SUM(t.raw_freq) AS raw_freq, SUM(t.weighted_freq) AS weighted_freq,
               SUM(t.momentum) AS momentum, SUM(t.normalized_share) / ? AS normalized_share,
               SUM(t.weighted_normalized_share) / ? AS weighted_normalized_share
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE {' AND '.join(where)}
        GROUP BY t.term HAVING SUM(t.raw_freq) >= 2
        ORDER BY SUM(t.{metric_col}) DESC LIMIT ?
        """, (len(dense_months), len(dense_months), *params, limit))]


def top_terms_by_type(conn: sqlite3.Connection, term_type: str, limit: int, scope: str, from_month: str, to_month: str, data_mode: str = "auto") -> list[dict[str, Any]]:
    mode = resolve_data_mode(conn, data_mode)
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    return [dict(row) for row in conn.execute(f"""
        SELECT pt.normalized_term AS term, COUNT(DISTINCT pt.paper_id) AS raw_freq
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE pt.term_type = ? AND pt.display_eligible = 1 AND {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ?
        GROUP BY pt.normalized_term ORDER BY raw_freq DESC, term ASC LIMIT ?
        """, (term_type, *predicate_params, *mode_params, from_month[:7], to_month[:7], limit))]


def scoped_months(conn: sqlite3.Connection, scope: str, from_month: str | None, to_month: str | None, data_mode: str = "auto") -> list[str]:
    mode = resolve_data_mode(conn, data_mode)
    # term_month_stats is intentionally sparse: a missing row is a true zero.
    # The former dense table used the preset-wide calendar for every scope.
    bounds = conn.execute(
        "SELECT MIN(month) AS min_month, MAX(month) AS max_month FROM monthly_corpus_stats WHERE data_mode = ?",
        (mode,),
    ).fetchone()
    if not bounds or not bounds["min_month"] or not bounds["max_month"]:
        return []
    start = max(str(bounds["min_month"]), from_month[:7]) if from_month else str(bounds["min_month"])
    end = min(str(bounds["max_month"]), to_month[:7]) if to_month else str(bounds["max_month"])
    return month_range(start, end) if start <= end else []


def rising_terms(conn: sqlite3.Connection, limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None, data_mode: str = "auto") -> list[dict[str, Any]]:
    months = scoped_months(conn, scope, from_month, to_month, data_mode)
    return _growth_table(conn, set(months[-3:]), set(months[-6:-3]), limit, True, scope, data_mode)


def cooling_terms(conn: sqlite3.Connection, limit: int = 20, scope: str = SCOPE_CORE, from_month: str | None = None, to_month: str | None = None, data_mode: str = "auto") -> list[dict[str, Any]]:
    months = scoped_months(conn, scope, from_month, to_month, data_mode)
    return _growth_table(conn, set(months[-3:]), set(months[-6:-3]), limit, False, scope, data_mode)


def _growth_table(conn: sqlite3.Connection, recent_months: set[str], previous_months_set: set[str], limit: int, descending: bool, scope: str, data_mode: str = "auto") -> list[dict[str, Any]]:
    if not recent_months or not previous_months_set:
        return []
    mode = resolve_data_mode(conn, data_mode)
    rows = conn.execute("""
        SELECT t.term, t.month, SUM(t.weighted_freq) AS weighted_freq, SUM(t.raw_freq) AS raw_freq
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE t.corpus_scope = ? AND t.data_mode = ? AND t.display_eligible = 1 AND COALESCE(l.concept_class, 'physics_concept') = 'physics_concept'
        GROUP BY t.term, t.month
        """, (scope, mode)).fetchall()
    stats: dict[str, dict[str, float]] = defaultdict(lambda: {"recent": 0.0, "previous": 0.0, "raw": 0.0})
    for row in rows:
        if row["month"] in recent_months:
            stats[row["term"]]["recent"] += float(row["weighted_freq"]); stats[row["term"]]["raw"] += float(row["raw_freq"])
        elif row["month"] in previous_months_set:
            stats[row["term"]]["previous"] += float(row["weighted_freq"])
    results = []
    for term, values in stats.items():
        growth = (values["recent"] + 1.0) / (values["previous"] + 1.0)
        if descending and values["recent"] < 1:
            continue
        if not descending and values["previous"] < 1:
            continue
        results.append({"term": term, "recent_weighted_freq": values["recent"], "previous_weighted_freq": values["previous"], "growth": growth, "raw_freq": values["raw"]})
    results.sort(key=lambda item: item["growth"], reverse=descending)
    return results[:limit]


def journal_distribution(conn: sqlite3.Connection, scope: str, from_month: str, to_month: str, data_mode: str = "auto") -> list[dict[str, Any]]:
    mode = resolve_data_mode(conn, data_mode)
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    return [dict(row) for row in conn.execute(f"SELECT p.journal, COUNT(*) AS count FROM papers p WHERE {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ? GROUP BY p.journal ORDER BY count DESC", (*predicate_params, *mode_params, from_month[:7], to_month[:7]))]


def source_distribution(conn: sqlite3.Connection, scope: str, from_month: str, to_month: str, data_mode: str = "auto") -> list[dict[str, Any]]:
    mode = resolve_data_mode(conn, data_mode)
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    return [dict(row) for row in conn.execute(f"SELECT p.source, COUNT(*) AS count FROM papers p WHERE {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ? GROUP BY p.source ORDER BY count DESC", (*predicate_params, *mode_params, from_month[:7], to_month[:7]))]


def heatmap(conn: sqlite3.Connection, metric: str = "momentum", journal: str = "all", from_month: str | None = None, to_month: str | None = None, limit: int = 40, scope: str = SCOPE_CORE, concept_class: str = "all", exclude_methods: bool = False, exclude_platform_materials: bool = False, min_count: int = 2, data_mode: str = "auto") -> dict[str, Any]:
    windows = latest_windows(conn)
    mode = resolve_data_mode(conn, data_mode)
    from_month = (from_month or str(windows["trend_from"]))[:7]
    to_month = (to_month or str(windows["trend_to"]))[:7]
    metric_col = metric_sql(metric)
    where = ["t.corpus_scope = ?", "t.data_mode = ?", "t.display_eligible = 1", "t.month >= ?", "t.month <= ?"]
    params: list[Any] = [scope, mode, from_month, to_month]
    if concept_class and concept_class != "all":
        where.append("COALESCE(l.concept_class, '') = ?"); params.append(concept_class)
    if exclude_methods:
        where.append("COALESCE(l.concept_class, '') != 'method'")
    if exclude_platform_materials:
        where.append("COALESCE(l.concept_class, '') != 'platform_material'"); where.append("COALESCE(l.is_platform_term, 0) = 0")
    top = [row["term"] for row in conn.execute(f"""
        SELECT t.term, SUM(t.{metric_col}) AS score, SUM(t.raw_freq) AS raw_total
        FROM term_month_stats t LEFT JOIN concept_lifecycle l ON l.concept = t.term
        WHERE {' AND '.join(where)} GROUP BY t.term HAVING raw_total >= ? ORDER BY score DESC LIMIT ?
        """, (*params, max(1, min_count), limit))]
    if not top:
        return {"months": [], "terms": [], "values": [], "metric": metric_col, "scope": scope, "data_mode": mode}
    rows = conn.execute(f"""
        SELECT term, month, raw_freq, weighted_freq, normalized_share, weighted_normalized_share, momentum
        FROM term_month_stats WHERE corpus_scope = ? AND data_mode = ? AND term IN ({placeholders(top)}) AND month >= ? AND month <= ?
        ORDER BY month, term
        """, (scope, mode, *top, from_month, to_month)).fetchall()
    months = month_range(from_month, to_month)
    month_index = {month: idx for idx, month in enumerate(months)}
    term_index = {term: idx for idx, term in enumerate(top)}
    values = [[month_index[row["month"]], term_index[row["term"]], float(row[metric_col])] for row in rows if row["month"] in month_index and row["term"] in term_index and float(row[metric_col]) > 0]
    return {"months": months, "terms": top, "values": values, "metric": metric_col, "journal": journal, "scope": scope, "data_mode": mode}


def concept_series(conn: sqlite3.Connection, concept: str, scope: str = SCOPE_ALL, from_month: str | None = None, to_month: str | None = None, smoothing: str = "raw", data_mode: str = "auto") -> list[dict[str, Any]]:
    windows = latest_windows(conn)
    mode = resolve_data_mode(conn, data_mode)
    from_month = (from_month or str(windows["baseline_from"]))[:7]
    to_month = (to_month or str(windows["baseline_to"]))[:7]
    rows = conn.execute("""
        SELECT month, raw_freq, weighted_freq, normalized_share, weighted_normalized_share, momentum
        FROM term_month_stats WHERE term = ? AND corpus_scope = ? AND data_mode = ? AND month >= ? AND month <= ? ORDER BY month
        """, (concept, scope, mode, from_month, to_month)).fetchall()
    by_month = {row["month"]: dict(row) for row in rows}
    output = []
    for month in month_range(from_month, to_month):
        item = by_month.get(month, {"month": month, "raw_freq": 0, "weighted_freq": 0.0, "normalized_share": 0.0, "weighted_normalized_share": 0.0, "momentum": 0.0})
        output.append({"month": month, "raw_freq": item["raw_freq"], "weighted_freq": item["weighted_freq"], "normalized_share": item["normalized_share"], "weighted_normalized_share": item["weighted_normalized_share"], "momentum": item["momentum"]})
    return smooth_series(output, smoothing)


def compare_data(conn: sqlite3.Connection, concepts: list[str], metric: str, from_month: str, to_month: str, smoothing: str, scope: str, data_mode: str = "auto") -> dict[str, Any]:
    mode = resolve_data_mode(conn, data_mode)
    metric_col = metric_sql(metric if metric != "z_score" else "weighted_freq")
    normalized_terms = [normalize_term(item) for item in concepts if item.strip()][:8]
    series_payload = []
    for concept in normalized_terms:
        rows = concept_series(conn, concept, scope=scope, from_month=from_month, to_month=to_month, smoothing="raw", data_mode=mode)
        values = [float(row[metric_col]) for row in rows]
        if metric == "z_score":
            avg = sum(values) / len(values) if values else 0.0
            variance = sum((value - avg) ** 2 for value in values) / len(values) if values else 0.0
            denom = math.sqrt(variance) or 1.0
            values = [(value - avg) / denom for value in values]
        values = smooth_values(values, smoothing)
        series_payload.append({"concept": concept, "points": [{"month": row["month"], "value": values[idx]} for idx, row in enumerate(rows)]})
    table = []
    if normalized_terms:
        table = [dict(row) for row in conn.execute(f"SELECT * FROM concept_lifecycle WHERE concept IN ({placeholders(normalized_terms)}) ORDER BY concept", normalized_terms)]
    return {"metric": metric, "scope": scope, "smoothing": smoothing, "data_mode": mode, "series": series_payload, "table": table}


def papers_query(conn: sqlite3.Connection, query: str = "", journal: str = "", concept: str = "", from_date: str = "", to_date: str = "", limit: int = 250, scope: str = SCOPE_CORE, data_mode: str = "auto") -> list[dict[str, Any]]:
    mode = resolve_data_mode(conn, data_mode)
    where = ["1=1"]
    params: list[Any] = []
    joins = ""
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    where.extend([predicate, mode_sql]); params.extend(predicate_params); params.extend(mode_params); where.append("COALESCE(p.condmat_confidence, 'medium') != 'low'"); where.append("EXISTS (SELECT 1 FROM paper_terms visible WHERE visible.paper_id = p.id AND visible.display_eligible = 1)")
    if query:
        where.append("(p.title LIKE ? OR p.abstract LIKE ?)"); params.extend([f"%{query}%", f"%{query}%"])
    if journal:
        where.append("p.journal = ?"); params.append(journal)
    if from_date:
        where.append("p.publication_date >= ?"); params.append(from_date if len(from_date) > 7 else f"{from_date[:7]}-01")
    if to_date:
        where.append("p.publication_date <= ?"); params.append(to_date if len(to_date) > 7 else f"{to_date[:7]}-31")
    if concept:
        joins = "JOIN paper_terms filter_terms ON filter_terms.paper_id = p.id"; where.append("filter_terms.normalized_term = ?"); params.append(concept)
    rows = conn.execute(f"""
        SELECT p.*, COALESCE(SUM(tms.momentum), 0) AS momentum_score
        FROM papers p {joins}
        LEFT JOIN paper_terms pt ON pt.paper_id = p.id
        LEFT JOIN term_month_stats tms ON tms.term = pt.normalized_term AND tms.month = p.month AND tms.corpus_scope = ? AND tms.data_mode = ?
        WHERE {' AND '.join(where)}
        GROUP BY p.id ORDER BY p.publication_date DESC LIMIT ?
        """, (scope, mode, *params, limit)).fetchall()
    return hydrate_papers(conn, rows)


def compare_papers_query(conn: sqlite3.Connection, concepts: list[str], mode: str, from_month: str, to_month: str, scope: str, limit: int = 250, data_mode: str = "auto") -> list[dict[str, Any]]:
    stats_mode = resolve_data_mode(conn, data_mode)
    normalized = [normalize_term(item) for item in concepts if item.strip()][:8]
    if not normalized:
        return []
    predicate, predicate_params = paper_scope_predicate(scope, "p")
    mode_sql, mode_params = paper_mode_predicate(stats_mode, "p")
    comparator = "COUNT(DISTINCT pt.normalized_term) = ?" if mode == "and" else "COUNT(DISTINCT pt.normalized_term) >= 1"
    having_params: list[Any] = [len(normalized)] if mode == "and" else []
    rows = conn.execute(f"""
        SELECT p.*, COALESCE(SUM(tms.momentum), 0) AS momentum_score
        FROM papers p
        JOIN paper_terms pt ON pt.paper_id = p.id AND pt.normalized_term IN ({placeholders(normalized)})
        LEFT JOIN term_month_stats tms ON tms.term = pt.normalized_term AND tms.month = p.month AND tms.corpus_scope = ? AND tms.data_mode = ?
        WHERE {predicate} AND {mode_sql} AND p.month >= ? AND p.month <= ?
        GROUP BY p.id HAVING {comparator}
        ORDER BY p.publication_date DESC LIMIT ?
        """, (*normalized, scope, stats_mode, *predicate_params, *mode_params, from_month[:7], to_month[:7], *having_params, limit)).fetchall()
    return hydrate_papers(conn, rows)


def download_tasks(conn: sqlite3.Connection, concept: str = "", from_month: str = "", to_month: str = "", scope: str = SCOPE_ALL, limit: int = 1000, data_mode: str = "auto") -> list[dict[str, Any]]:
    concept_key = normalize_term(concept) if concept else ""
    papers = papers_query(conn, concept=concept_key, from_date=from_month, to_date=to_month, limit=limit, scope=scope, data_mode=data_mode)
    return build_download_tasks(papers, selected_concepts=[concept_key] if concept_key else [])

# --- Corpus-preset aware overrides for strict condensed matter view. ---
from backend.db.strict_condmat import (
    DEFAULT_CORPUS_PRESET,
    STRICT_STATS_MODE,
    corpus_preset_label,
    corpus_preset_predicate,
    ensure_strict_condmat_schema,
    latest_quality_report_path,
    normalize_corpus_preset,
    refresh_strict_condmat_flags,
    stats_mode_for_preset,
    strict_counts,
)


def resolve_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
    requested = (requested or "auto").lower()
    if requested == STRICT_STATS_MODE:
        return STRICT_STATS_MODE
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


def paper_mode_predicate(data_mode: str, alias: str = "p") -> tuple[str, list[Any]]:
    if data_mode == STRICT_STATS_MODE:
        return corpus_preset_predicate("strict_condmat", alias)
    if data_mode == "mixed":
        return "1=1", []
    return f"{alias}.data_mode = ?", [data_mode]


def data_source_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    mode = resolve_data_mode(conn, data_mode)
    source_rows = conn.execute("SELECT data_mode, source, COUNT(*) AS n FROM papers GROUP BY data_mode, source ORDER BY data_mode, n DESC").fetchall()
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    active_sources = [dict(row) for row in conn.execute(f"SELECT p.source AS source, COUNT(*) AS n FROM papers p WHERE {mode_sql} GROUP BY p.source ORDER BY n DESC", tuple(mode_params)).fetchall()]
    if mode == STRICT_STATS_MODE:
        label = corpus_preset_label("strict_condmat")
        preset = "strict_condmat"
    elif mode == "mock":
        label = corpus_preset_label("mock")
        preset = "mock"
    else:
        source_names = {row["source"] for row in active_sources}
        label = "/".join(sorted(source_names)) + " real metadata" if source_names else "Real metadata"
        preset = "all_real"
    return {"is_mock": mode == "mock", "label": label, "data_mode": mode, "corpus_preset": preset, "sources": [dict(item) for item in source_rows], "active_sources": active_sources}


def corpus_size_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    estimates = load_latest_estimate()
    mode = resolve_data_mode(conn, data_mode)
    if mode == STRICT_STATS_MODE:
        ensure_strict_condmat_schema(conn)
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    counts = strict_counts(conn)
    return {
        "database_path": str(db_path()),
        "total_papers": conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"],
        "active_paper_count": conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "real_paper_count": counts["real_paper_count"],
        "strict_condmat_paper_count": counts["strict_condmat_paper_count"],
        "excluded_non_condmat_paper_count": counts["excluded_non_condmat_paper_count"],
        "mock_paper_count": counts["mock_paper_count"],
        "current_corpus_preset": "strict_condmat" if mode == STRICT_STATS_MODE else "mock" if mode == "mock" else "all_real",
        "current_corpus_label": corpus_preset_label("strict_condmat") if mode == STRICT_STATS_MODE else corpus_preset_label("mock") if mode == "mock" else corpus_preset_label("all_real"),
        "latest_quality_report_path": latest_quality_report_path(export_dir()),
        "concept_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='concept' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "material_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='material' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "method_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='method' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "estimated_full_corpus": estimates.get("estimated_total_works") if estimates else None,
    }


def rebuild_preset_monthly_corpus_stats(conn: sqlite3.Connection, corpus_preset: str = DEFAULT_CORPUS_PRESET, refresh_flags: bool = True) -> int:
    preset = normalize_corpus_preset(corpus_preset)
    if preset == "strict_condmat" and refresh_flags:
        refresh_strict_condmat_flags(conn)
    mode = stats_mode_for_preset(preset)
    predicate, predicate_params = corpus_preset_predicate(preset, "p")
    conn.execute("DELETE FROM monthly_corpus_stats WHERE data_mode = ?", (mode,))
    rows = conn.execute(f"SELECT p.id, p.month, p.journal, p.source FROM papers p WHERE p.month IS NOT NULL AND {predicate}", tuple(predicate_params)).fetchall()
    grouped: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: {"total_papers": 0, "total_weighted_papers": 0.0})
    for row in rows:
        for scope in included_scopes(row["journal"], row["source"]):
            key = (row["month"], scope, mode)
            grouped[key]["total_papers"] += 1
            grouped[key]["total_weighted_papers"] += journal_weight(row["journal"], row["source"])
    for (month, scope, stats_mode), item in grouped.items():
        conn.execute(
            "INSERT OR REPLACE INTO monthly_corpus_stats (month, corpus_scope, data_mode, total_papers, total_weighted_papers) VALUES (?, ?, ?, ?, ?)",
            (month, scope, stats_mode, int(item["total_papers"]), float(item["total_weighted_papers"])),
        )
    return len(grouped)


def rebuild_preset_term_month_stats(conn: sqlite3.Connection, corpus_preset: str = DEFAULT_CORPUS_PRESET) -> int:
    preset = normalize_corpus_preset(corpus_preset)
    if preset == "strict_condmat":
        refresh_strict_condmat_flags(conn)
    mode = stats_mode_for_preset(preset)
    predicate, predicate_params = corpus_preset_predicate(preset, "p")
    conn.execute("DELETE FROM term_month_stats WHERE data_mode = ?", (mode,))
    rebuild_preset_monthly_corpus_stats(conn, preset, refresh_flags=False)
    rows = conn.execute(
        f"""
        SELECT DISTINCT pt.normalized_term AS term, pt.term_type, pt.confidence,
          pt.display_eligible, pt.display_reason, p.id AS paper_id, p.month,
          p.journal, p.source, p.cited_by_count
        FROM paper_terms pt
        JOIN papers p ON p.id = pt.paper_id
        WHERE p.month IS NOT NULL AND pt.term_type IN ('concept', 'material', 'method')
          AND {predicate}
        """,
        tuple(predicate_params),
    ).fetchall()
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    term_meta: dict[str, dict[str, Any]] = defaultdict(lambda: {"types": set(), "eligible": 0, "reasons": defaultdict(int), "max_confidence": 0.0})
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        term = row["term"]
        term_meta[term]["types"].add(row["term_type"])
        term_meta[term]["eligible"] = max(int(term_meta[term]["eligible"]), int(row["display_eligible"] or 0))
        term_meta[term]["reasons"][row["display_reason"] or "unknown"] += 1
        term_meta[term]["max_confidence"] = max(float(term_meta[term]["max_confidence"]), float(row["confidence"] or 0.0))
        for scope in included_scopes(row["journal"], row["source"]):
            dedupe_key = (term, row["paper_id"], scope, mode)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            key = (term, row["month"], scope, mode)
            item = grouped.setdefault(key, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
            item["raw_freq"] += 1
            item["weighted_freq"] += journal_weight(row["journal"], row["source"])
            item["journals"][row["journal"] or "Unknown"] += 1
            item["citation_values"].append(float(row["cited_by_count"] or 0))
    bounds = conn.execute(f"SELECT MIN(p.month) AS min_month, MAX(p.month) AS max_month FROM papers p WHERE p.month IS NOT NULL AND {predicate}", tuple(predicate_params)).fetchone()
    if not bounds or not bounds["min_month"] or not bounds["max_month"]:
        return 0
    all_months = month_range(bounds["min_month"], bounds["max_month"])
    corpus_totals = {(row["month"], row["corpus_scope"], row["data_mode"]): dict(row) for row in conn.execute("SELECT * FROM monthly_corpus_stats WHERE data_mode = ?", (mode,)).fetchall()}
    by_term_scope_mode: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for (term, month, scope, stats_mode), item in grouped.items():
        by_term_scope_mode[(term, scope, stats_mode)][month] = item
    inserted = 0
    for (term, scope, stats_mode), series in by_term_scope_mode.items():
        display_eligible, display_reason = display_flag_for_term(term, term_meta.get(term, {}))
        for month in all_months:
            item = series.get(month, {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citation_values": []})
            recent = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 3))
            previous = sum(series.get(m, {}).get("weighted_freq", 0.0) for m in previous_months(all_months, month, 6, 3))
            growth_factor = min((recent + 1.0) / (previous + 1.0), 4.0)
            avg_citations = sum(item["citation_values"]) / len(item["citation_values"]) if item["citation_values"] else 0.0
            citation_signal = min(math.log1p(avg_citations), 3.0)
            weighted_freq = float(item["weighted_freq"])
            momentum = math.log1p(weighted_freq) * growth_factor * (1.0 + 0.35 * citation_signal)
            totals = corpus_totals.get((month, scope, stats_mode), {"total_papers": 0, "total_weighted_papers": 0.0})
            total_papers = int(totals.get("total_papers") or 0)
            total_weighted = float(totals.get("total_weighted_papers") or 0.0)
            conn.execute(
                """
                INSERT OR REPLACE INTO term_month_stats
                  (term, month, corpus_scope, data_mode, raw_freq, weighted_freq, normalized_share,
                   weighted_normalized_share, momentum, journal_breakdown, citation_signal,
                   display_eligible, display_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    term, month, scope, stats_mode, int(item["raw_freq"]), weighted_freq,
                    float(item["raw_freq"]) / total_papers if total_papers else 0.0,
                    weighted_freq / total_weighted if total_weighted else 0.0,
                    float(momentum), json.dumps(dict(item["journals"]), ensure_ascii=False),
                    citation_signal, display_eligible, display_reason,
                ),
            )
            inserted += 1
    return inserted
# --- Published/preprint corpus-preset overrides. ---
from backend.db.strict_condmat import VALID_CORPUS_PRESETS, preset_for_stats_mode


def _known_stats_modes() -> set[str]:
    return {stats_mode_for_preset(preset) for preset in VALID_CORPUS_PRESETS} | {"mixed", "strict_condmat"}


def _preset_for_mode(mode: str) -> str | None:
    if mode == "strict_condmat":
        return "strict_core_published"
    return preset_for_stats_mode(mode)


def label_for_data_mode(mode: str) -> tuple[str, str]:
    preset = _preset_for_mode(mode)
    if preset:
        return preset, corpus_preset_label(preset)
    if mode == "mixed":
        return "mixed", "Mixed metadata"
    return mode, mode


def resolve_data_mode(conn: sqlite3.Connection, requested: str = "auto") -> str:
    requested = (requested or "auto").lower()
    if requested == "auto":
        return STRICT_STATS_MODE
    if requested == "strict_condmat":
        return STRICT_STATS_MODE
    if requested in _known_stats_modes():
        return requested
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    if requested == "real" and real_count:
        return "real"
    if requested == "mock" and mock_count:
        return "mock"
    if requested == "mixed":
        return "mixed"
    if real_count:
        return STRICT_STATS_MODE
    if mock_count:
        return "mock"
    return STRICT_STATS_MODE


def paper_mode_predicate(data_mode: str, alias: str = "p") -> tuple[str, list[Any]]:
    mode = (data_mode or STRICT_STATS_MODE).lower()
    preset = _preset_for_mode(mode)
    if preset:
        return corpus_preset_predicate(preset, alias)
    if mode == "mixed":
        return "1=1", []
    return f"{alias}.data_mode = ?", [mode]


def data_source_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    mode = resolve_data_mode(conn, data_mode)
    source_rows = conn.execute("SELECT data_mode, source, COUNT(*) AS n FROM papers GROUP BY data_mode, source ORDER BY data_mode, n DESC").fetchall()
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    active_sources = [dict(row) for row in conn.execute(f"SELECT p.source AS source, COUNT(*) AS n FROM papers p WHERE {mode_sql} GROUP BY p.source ORDER BY n DESC", tuple(mode_params)).fetchall()]
    preset, label = label_for_data_mode(mode)
    return {"is_mock": mode == "mock", "label": label, "data_mode": mode, "corpus_preset": preset, "sources": [dict(item) for item in source_rows], "active_sources": active_sources}


def corpus_size_summary(conn: sqlite3.Connection, data_mode: str = "auto") -> dict[str, Any]:
    estimates = load_latest_estimate()
    mode = resolve_data_mode(conn, data_mode)
    if mode in _known_stats_modes():
        ensure_strict_condmat_schema(conn)
    mode_sql, mode_params = paper_mode_predicate(mode, "p")
    counts = strict_counts(conn)
    preset, label = label_for_data_mode(mode)
    return {
        "database_path": str(db_path()),
        "total_papers": conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"],
        "active_paper_count": conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "real_paper_count": counts["real_paper_count"],
        "strict_condmat_paper_count": counts["strict_condmat_paper_count"],
        "strict_core_published_paper_count": counts.get("strict_core_published_paper_count", 0),
        "strict_context_published_paper_count": counts.get("strict_context_published_paper_count", 0),
        "arxiv_preprint_paper_count": counts.get("arxiv_preprint_paper_count", 0),
        "excluded_non_condmat_paper_count": counts["excluded_non_condmat_paper_count"],
        "mock_paper_count": counts["mock_paper_count"],
        "current_corpus_preset": preset,
        "current_corpus_label": label,
        "latest_quality_report_path": latest_quality_report_path(export_dir()),
        "concept_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='concept' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "material_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='material' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "method_count": conn.execute(f"SELECT COUNT(DISTINCT pt.normalized_term) AS n FROM paper_terms pt JOIN papers p ON p.id=pt.paper_id WHERE pt.term_type='method' AND pt.display_eligible=1 AND {mode_sql}", tuple(mode_params)).fetchone()["n"],
        "estimated_full_corpus": estimates.get("estimated_total_works") if estimates else None,
    }


def _preset_needs_strict_refresh(preset: str) -> bool:
    return normalize_corpus_preset(preset) in {"strict_condmat", "strict_core_published", "strict_context_published", "strict_core_plus_context_published", "arxiv_preprint", "published_vs_preprint"}


def rebuild_preset_monthly_corpus_stats(conn: sqlite3.Connection, corpus_preset: str = DEFAULT_CORPUS_PRESET, refresh_flags: bool = True) -> int:
    preset = normalize_corpus_preset(corpus_preset)
    if _preset_needs_strict_refresh(preset) and refresh_flags:
        refresh_strict_condmat_flags(conn)
    mode = stats_mode_for_preset(preset)
    predicate, predicate_params = corpus_preset_predicate(preset, "p")
    conn.execute("DELETE FROM monthly_corpus_stats WHERE data_mode = ?", (mode,))
    rows = conn.execute(f"SELECT p.id, p.month, p.journal, p.source FROM papers p WHERE p.month IS NOT NULL AND {predicate}", tuple(predicate_params))
    grouped: dict[tuple[str, str, str], dict[str, float]] = defaultdict(lambda: {"total_papers": 0, "total_weighted_papers": 0.0})
    for row in rows:
        for scope in included_scopes(row["journal"], row["source"]):
            key = (row["month"], scope, mode)
            grouped[key]["total_papers"] += 1
            grouped[key]["total_weighted_papers"] += journal_weight(row["journal"], row["source"])
    conn.executemany(
        "INSERT INTO monthly_corpus_stats (month, corpus_scope, data_mode, total_papers, total_weighted_papers) VALUES (?, ?, ?, ?, ?)",
        (
            (month, scope, stats_mode, int(item["total_papers"]), float(item["total_weighted_papers"]))
            for (month, scope, stats_mode), item in grouped.items()
        ),
    )
    return len(grouped)


def rebuild_preset_term_month_stats(conn: sqlite3.Connection, corpus_preset: str = DEFAULT_CORPUS_PRESET, refresh_flags: bool = True) -> int:
    """Store only non-zero observations; readers reconstruct calendar zeros."""
    preset = normalize_corpus_preset(corpus_preset)
    if _preset_needs_strict_refresh(preset) and refresh_flags:
        refresh_strict_condmat_flags(conn)
    mode = stats_mode_for_preset(preset)
    predicate, predicate_params = corpus_preset_predicate(preset, "p")
    savepoint = "rebuild_sparse_term_month_stats"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        conn.execute("DELETE FROM term_month_stats WHERE data_mode = ?", (mode,))
        rebuild_preset_monthly_corpus_stats(conn, preset, refresh_flags=False)
        bounds = conn.execute(
            f"SELECT MIN(p.month) AS min_month, MAX(p.month) AS max_month FROM papers p WHERE p.month IS NOT NULL AND {predicate}",
            tuple(predicate_params),
        ).fetchone()
        if not bounds or not bounds["min_month"] or not bounds["max_month"]:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            return 0
        months = month_range(bounds["min_month"], bounds["max_month"])
        month_index = {month: index for index, month in enumerate(months)}
        totals = {
            (row["month"], row["corpus_scope"]): dict(row)
            for row in conn.execute("SELECT * FROM monthly_corpus_stats WHERE data_mode = ?", (mode,))
        }
        rows = conn.execute(
            f"""
            SELECT pt.normalized_term AS term, pt.term_type, pt.confidence,
              pt.display_eligible, pt.display_reason, p.id AS paper_id, p.month,
              p.journal, p.source, p.cited_by_count
            FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
            WHERE p.month IS NOT NULL AND pt.term_type IN ('concept', 'material', 'method')
              AND {predicate}
            ORDER BY pt.normalized_term
            """, tuple(predicate_params))
        insert_sql = """
            INSERT INTO term_month_stats
              (term, month, corpus_scope, data_mode, raw_freq, weighted_freq, normalized_share,
               weighted_normalized_share, momentum, journal_breakdown, citation_signal,
               display_eligible, display_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        pending: list[tuple[Any, ...]] = []
        inserted = 0

        def window(series: dict[str, dict[str, Any]], month: str, width: int, offset: int = 0) -> float:
            index = month_index[month]
            end = max(0, index - offset + 1)
            start = max(0, end - width)
            return sum(float(series.get(key, {}).get("weighted_freq", 0.0)) for key in months[start:end])

        def flush(term: str | None, grouped: dict[tuple[str, str], dict[str, Any]], meta: dict[str, Any]) -> None:
            nonlocal inserted
            if term is None:
                return
            eligible, reason = display_flag_for_term(term, meta)
            scoped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
            for (month, scope), item in grouped.items():
                scoped[scope][month] = item
            for scope, series in scoped.items():
                for month, item in series.items():
                    growth = min((window(series, month, 3) + 1.0) / (window(series, month, 6, 3) + 1.0), 4.0)
                    avg_citations = sum(item["citations"]) / len(item["citations"]) if item["citations"] else 0.0
                    citation_signal = min(math.log1p(avg_citations), 3.0)
                    weighted = float(item["weighted_freq"])
                    momentum = math.log1p(weighted) * growth * (1.0 + 0.35 * citation_signal)
                    corpus = totals.get((month, scope), {})
                    total_raw = int(corpus.get("total_papers") or 0)
                    total_weighted = float(corpus.get("total_weighted_papers") or 0.0)
                    pending.append((
                        term, month, scope, mode, int(item["raw_freq"]), weighted,
                        float(item["raw_freq"]) / total_raw if total_raw else 0.0,
                        weighted / total_weighted if total_weighted else 0.0, momentum,
                        json.dumps(dict(item["journals"]), ensure_ascii=False),
                        citation_signal, eligible, reason,
                    ))
                    if len(pending) >= 1000:
                        conn.executemany(insert_sql, pending)
                        inserted += len(pending)
                        pending.clear()

        current: str | None = None
        grouped: dict[tuple[str, str], dict[str, Any]] = {}
        meta: dict[str, Any] = {"types": set(), "eligible": 0, "reasons": defaultdict(int), "max_confidence": 0.0}
        seen: set[tuple[str, str]] = set()
        for row in rows:
            term = row["term"]
            if current is not None and term != current:
                flush(current, grouped, meta)
                grouped = {}
                meta = {"types": set(), "eligible": 0, "reasons": defaultdict(int), "max_confidence": 0.0}
                seen = set()
            current = term
            meta["types"].add(row["term_type"])
            meta["eligible"] = max(int(meta["eligible"]), int(row["display_eligible"] or 0))
            meta["reasons"][row["display_reason"] or "unknown"] += 1
            meta["max_confidence"] = max(float(meta["max_confidence"]), float(row["confidence"] or 0.0))
            for scope in included_scopes(row["journal"], row["source"]):
                key = (row["paper_id"], scope)
                if key in seen:
                    continue
                seen.add(key)
                item = grouped.setdefault((row["month"], scope), {"raw_freq": 0, "weighted_freq": 0.0, "journals": defaultdict(int), "citations": []})
                item["raw_freq"] += 1
                item["weighted_freq"] += journal_weight(row["journal"], row["source"])
                item["journals"][row["journal"] or "Unknown"] += 1
                item["citations"].append(float(row["cited_by_count"] or 0))
        flush(current, grouped, meta)
        if pending:
            conn.executemany(insert_sql, pending)
            inserted += len(pending)
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return inserted
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
