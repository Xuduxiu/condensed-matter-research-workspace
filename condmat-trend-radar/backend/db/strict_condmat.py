from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from backend.nlp.dictionaries import (
    COND_MAT_KEYWORDS,
    CONTEXT_JOURNALS,
    CORE_JOURNALS,
    GENERALIST_JOURNALS,
    HARD_KEEP_TERMS,
    MATERIALS,
    METHODS,
    PHYSICS_CONCEPTS,
    SEED_CONCEPTS,
)
from backend.ingest.openalex_quality import reclassify_openalex_repository_quality
from backend.nlp.normalize import lookup_key, month_range, normalize_term

VALID_CORPUS_PRESETS = (
    "all_real",
    "strict_condmat",
    "strict_core_published",
    "strict_context_published",
    "strict_core_plus_context_published",
    "arxiv_preprint",
    "published_vs_preprint",
    "core_all",
    "mock",
)
DEFAULT_CORPUS_PRESET = "strict_core_published"
STRICT_STATS_MODE = "strict_core_published"
CORE_CONTEXT_JOURNALS = tuple(dict.fromkeys([*CORE_JOURNALS, *CONTEXT_JOURNALS]))

CORPUS_PRESET_LABELS = {
    "all_real": "\u5168\u90e8\u771f\u5b9e\u6570\u636e",
    "strict_condmat": "\u4e25\u683c\u51dd\u805a\u6001",
    "strict_core_published": "\u4e25\u683c\u51dd\u805a\u6001",
    "strict_context_published": "Context \u51dd\u805a\u6001",
    "strict_core_plus_context_published": "Core + Context \u51dd\u805a\u6001",
    "arxiv_preprint": "arXiv \u9884\u5370\u672c",
    "published_vs_preprint": "\u53d1\u8868 / \u9884\u5370\u672c",
    "core_all": "\u6838\u5fc3\u671f\u520a\u5168\u90e8",
    "mock": "Mock \u793a\u4f8b",
}

EXCLUDED_NON_CONDMAT_TERMS = [
    "protein",
    "cell",
    "cancer",
    "tumor",
    "genome",
    "gene",
    "clinical",
    "patient",
    "virus",
    "bacteria",
    "climate",
    "ecology",
    "battery",
    "catalyst",
    "catalysis",
    "photocatalysis",
    "perovskite solar cell",
    "water splitting",
    "CO2 reduction",
    "polymer chemistry",
    "drug",
    "enzyme",
]

STRONG_CONDMAT_OVERRIDE_TERMS = [
    "quantum Hall",
    "fractional quantum Hall",
    "quantum anomalous Hall",
    "superconductivity",
    "superconductor",
    "ARPES",
    "STM",
    "STS",
    "moire",
    "moir\u00e9",
    "topological semimetal",
    "Weyl semimetal",
    "Dirac semimetal",
    "Chern insulator",
    "fractional Chern insulator",
    "charge density wave",
    "Mott",
    "Hubbard",
    "spin liquid",
    "Kitaev",
    "strange metal",
    "altermagnetism",
    "quantum oscillation",
]

STRICT_KEEP_TERMS = sorted(
    {
        normalize_term(term)
        for term in (
            HARD_KEEP_TERMS
            + COND_MAT_KEYWORDS
            + PHYSICS_CONCEPTS
            + SEED_CONCEPTS
            + MATERIALS
            + METHODS
            + STRONG_CONDMAT_OVERRIDE_TERMS
        )
        if term
    }
)

STRICT_KEEP_KEYS = {lookup_key(term) for term in STRICT_KEEP_TERMS}
STRONG_CONDMAT_OVERRIDE_KEYS = {lookup_key(term) for term in STRONG_CONDMAT_OVERRIDE_TERMS}
EXCLUDED_NON_CONDMAT_KEYS = {lookup_key(term) for term in EXCLUDED_NON_CONDMAT_TERMS}
GENERALIST_JOURNAL_KEYS = {lookup_key(journal) for journal in GENERALIST_JOURNALS}
GENERALIST_DOMAIN_TERMS = sorted(
    {
        normalize_term(term)
        for term in (
            HARD_KEEP_TERMS
            + COND_MAT_KEYWORDS
            + PHYSICS_CONCEPTS
            + SEED_CONCEPTS
            + MATERIALS
            + STRONG_CONDMAT_OVERRIDE_TERMS
        )
        if term
    }
)
GENERALIST_TEXT_POLICY_VERSION = "generalist_text_evidence_v1"


def normalize_corpus_preset(value: str | None) -> str:
    value = (value or DEFAULT_CORPUS_PRESET).strip().lower()
    return value if value in VALID_CORPUS_PRESETS else DEFAULT_CORPUS_PRESET


def corpus_preset_label(preset: str) -> str:
    return CORPUS_PRESET_LABELS.get(normalize_corpus_preset(preset), CORPUS_PRESET_LABELS[DEFAULT_CORPUS_PRESET])


def stats_mode_for_preset(preset: str) -> str:
    preset = normalize_corpus_preset(preset)
    if preset == "strict_condmat":
        return STRICT_STATS_MODE
    if preset == "all_real":
        return "real"
    if preset == "mock":
        return "mock"
    return preset


def preset_for_stats_mode(mode: str) -> str | None:
    normalized = (mode or "").strip().lower()
    if normalized == "strict_condmat":
        return "strict_core_published"
    for preset in VALID_CORPUS_PRESETS:
        if stats_mode_for_preset(preset) == normalized:
            return preset
    return None


def scope_for_preset(preset: str, requested_scope: str) -> str:
    preset = normalize_corpus_preset(preset)
    if preset in {"strict_condmat", "strict_core_published", "core_all"}:
        return "core"
    if preset in {"strict_context_published", "strict_core_plus_context_published"}:
        return "core_context"
    if preset in {"arxiv_preprint", "published_vs_preprint"}:
        return "all"
    return requested_scope


def placeholders(items: list[Any] | tuple[Any, ...]) -> str:
    return ",".join("?" for _ in items)


def _published_sql(alias: str) -> str:
    return f"COALESCE(NULLIF({alias}.source_scope, ''), 'published') = 'published'"


def _preprint_sql(alias: str) -> str:
    return f"(COALESCE(NULLIF({alias}.source_scope, ''), '') = 'preprint' OR {alias}.source = 'arxiv' OR {alias}.journal = 'arXiv')"


def corpus_preset_predicate(preset: str, alias: str = "p") -> tuple[str, list[Any]]:
    preset = normalize_corpus_preset(preset)
    if preset == "strict_condmat":
        preset = "strict_core_published"
    if preset == "strict_core_published":
        return (
            f"{alias}.data_mode = 'real' AND COALESCE({alias}.condmat_view_eligible, 0) = 1 "
            f"AND {alias}.journal IN ({placeholders(CORE_JOURNALS)}) AND {_published_sql(alias)}",
            list(CORE_JOURNALS),
        )
    if preset == "strict_context_published":
        return (
            f"{alias}.data_mode = 'real' AND COALESCE({alias}.condmat_view_eligible, 0) = 1 "
            f"AND {alias}.journal IN ({placeholders(CONTEXT_JOURNALS)}) AND {_published_sql(alias)}",
            list(CONTEXT_JOURNALS),
        )
    if preset == "strict_core_plus_context_published":
        return (
            f"{alias}.data_mode = 'real' AND COALESCE({alias}.condmat_view_eligible, 0) = 1 "
            f"AND {alias}.journal IN ({placeholders(CORE_CONTEXT_JOURNALS)}) AND {_published_sql(alias)}",
            list(CORE_CONTEXT_JOURNALS),
        )
    if preset == "arxiv_preprint":
        return f"{alias}.data_mode = 'real' AND COALESCE({alias}.condmat_view_eligible, 0) = 1 AND {_preprint_sql(alias)}", []
    if preset == "core_all":
        return f"{alias}.data_mode = 'real' AND {alias}.journal IN ({placeholders(CORE_JOURNALS)})", list(CORE_JOURNALS)
    if preset == "mock":
        return f"{alias}.data_mode = 'mock'", []
    return f"{alias}.data_mode = 'real'", []


def ensure_strict_condmat_schema(conn: sqlite3.Connection) -> None:
    column_cursor = conn.execute("PRAGMA table_info(papers)")
    columns: set[str] = set()
    while True:
        column_rows = column_cursor.fetchmany(64)
        if not column_rows:
            break
        columns.update(str(row["name"]) for row in column_rows)
    column_cursor.close()
    if "condmat_view_eligible" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN condmat_view_eligible INTEGER NOT NULL DEFAULT 0")
    if "condmat_view_reason" not in columns:
        conn.execute("ALTER TABLE papers ADD COLUMN condmat_view_reason TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_condmat_view ON papers(condmat_view_eligible)")
    conn.execute("DROP VIEW IF EXISTS strict_condmat_papers")
    conn.execute(
        """
        CREATE VIEW strict_condmat_papers AS
        SELECT *
        FROM papers
        WHERE data_mode = 'real' AND COALESCE(condmat_view_eligible, 0) = 1
        """
    )


def phrase_hits(text_key: str, phrases: list[str] | tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for phrase in phrases:
        key = lookup_key(phrase)
        if not key:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(key).replace(r"\ ", r"\s+") + r"(?![a-z0-9])"
        if re.search(pattern, text_key):
            hits.append(phrase)
    return hits


def classify_strict_condmat(row: sqlite3.Row, term_rows: list[sqlite3.Row]) -> tuple[int, str]:
    if row["data_mode"] != "real":
        return 0, "not_real"
    journal = row["journal"] or ""
    source = row["source"] or ""
    source_scope = row["source_scope"] or ""
    tracked_source = journal in CORE_CONTEXT_JOURNALS or source_scope == "preprint" or source == "arxiv" or journal == "arXiv"
    if not tracked_source:
        return 0, "not_tracked_journal"
    if (row["condmat_confidence"] or "medium") not in {"high", "medium"}:
        return 0, f"condmat_confidence_{row['condmat_confidence'] or 'missing'}"

    text = lookup_key(f"{row['title'] or ''}. {row['abstract'] or ''}")
    paper_terms = [term_row["normalized_term"] for term_row in term_rows]
    term_keys = {lookup_key(term) for term in paper_terms}
    keep_hits = phrase_hits(text, STRICT_KEEP_TERMS)
    keep_hits.extend(term for term in paper_terms if lookup_key(term) in STRICT_KEEP_KEYS)
    strong_hits = phrase_hits(text, STRONG_CONDMAT_OVERRIDE_TERMS)
    strong_hits.extend(term for term in paper_terms if lookup_key(term) in STRONG_CONDMAT_OVERRIDE_KEYS)
    exclude_hits = phrase_hits(text, EXCLUDED_NON_CONDMAT_TERMS)
    exclude_hits.extend(term for term in paper_terms if lookup_key(term) in EXCLUDED_NON_CONDMAT_KEYS)

    if exclude_hits and not strong_hits:
        return 0, "excluded_non_condmat:" + ",".join(sorted(set(exclude_hits))[:5])

    # A multidisciplinary journal is venue evidence, not field evidence.
    # Generic methods such as "transport" must not promote an unrelated paper.
    # Do not use paper_terms here: historical OpenAlex topics can be stored in
    # that table without appearing in title/abstract. A generalist venue needs
    # literal paper-level text evidence, not a provider taxonomy assignment.
    if lookup_key(journal) in GENERALIST_JOURNAL_KEYS:
        domain_hits = phrase_hits(text, GENERALIST_DOMAIN_TERMS)
        if domain_hits:
            return 1, "strict_condmat:generalist_text=" + ",".join(sorted(set(domain_hits))[:5])
        return 0, "generalist_requires_condmat_text"

    if keep_hits or strong_hits or term_keys:
        reason = "strict_condmat"
        if strong_hits:
            reason += ":strong=" + ",".join(sorted(set(strong_hits))[:5])
        elif keep_hits:
            reason += ":hit=" + ",".join(sorted(set(keep_hits))[:5])
        else:
            reason += ":display_term"
        if exclude_hits:
            reason += ":override_excluded=" + ",".join(sorted(set(exclude_hits))[:5])
        return 1, reason
    return 0, "no_condmat_hit"


def _refresh_strict_condmat_selection(
    conn: sqlite3.Connection,
    *,
    journals: tuple[str, ...] | list[str] | None = None,
    canonical_ids: list[str] | tuple[str, ...] | None = None,
    batch_size: int = 1000,
    dry_run: bool = False,
    commit_batches: bool = False,
) -> dict[str, Any]:
    """Reclassify a bounded selection without materializing the corpus.

    Keyset pagination keeps at most one paper batch and its term rows in
    Python memory. An explicitly empty canonical subset never expands into a
    full-corpus scan.
    """
    # Stay below SQLite builds whose host-parameter limit is 999.
    size = min(max(1, int(batch_size)), 500)
    selected_journals = tuple(
        dict.fromkeys(str(item) for item in (journals or ()) if item)
    )
    selected_ids = tuple(
        dict.fromkeys(str(item) for item in (canonical_ids or ()) if item)
    )
    if canonical_ids is not None and not selected_ids:
        return {
            "processed": 0,
            "eligible": 0,
            "excluded": 0,
            "changed": 0,
            "downgraded": 0,
            "promoted": 0,
            "updates_needed": 0,
            "rows_updated": 0,
            "reasons": {},
            "skipped": True,
            "reason": "empty_canonical_subset",
        }

    clauses = ["data_mode='real'"]
    params: list[Any] = []
    if selected_journals:
        clauses.append(f"journal IN ({placeholders(selected_journals)})")
        params.extend(selected_journals)
    if canonical_ids is not None:
        clauses.append(f"id IN ({placeholders(selected_ids)})")
        params.extend(selected_ids)

    reasons: Counter[str] = Counter()
    processed = eligible = changed = downgraded = promoted = 0
    updates_needed = rows_updated = 0
    last_id = ""
    while True:
        paper_cursor = conn.execute(
            f"""
            SELECT id, title, abstract, journal, source, source_scope, data_mode,
                   condmat_confidence,
                   COALESCE(condmat_view_eligible, 0) AS prior_eligible,
                   COALESCE(condmat_view_reason, '') AS prior_reason
            FROM papers
            WHERE {' AND '.join(clauses)} AND id > ?
            ORDER BY id
            LIMIT ?
            """,
            (*params, last_id, size),
        )
        paper_rows = paper_cursor.fetchmany(size)
        paper_cursor.close()
        if not paper_rows:
            break
        last_id = str(paper_rows[-1]["id"])
        paper_ids = [str(row["id"]) for row in paper_rows]
        term_map: dict[str, list[sqlite3.Row]] = defaultdict(list)
        term_cursor = conn.execute(
            f"""
            SELECT paper_id, normalized_term, term_type, display_eligible, source
            FROM paper_terms
            WHERE paper_id IN ({placeholders(paper_ids)})
              AND term_type IN ('concept', 'material', 'method')
              AND display_eligible = 1
            ORDER BY paper_id
            """,
            paper_ids,
        )
        while True:
            term_rows = term_cursor.fetchmany(size)
            if not term_rows:
                break
            for term_row in term_rows:
                term_map[str(term_row["paper_id"])].append(term_row)
        term_cursor.close()

        updates: list[tuple[int, str, str]] = []
        for row in paper_rows:
            flag, reason = classify_strict_condmat(
                row, term_map.get(str(row["id"]), [])
            )
            prior = int(row["prior_eligible"] or 0)
            prior_reason = str(row["prior_reason"] or "")
            eligibility_changed = prior != int(flag)
            reason_changed = prior_reason != reason
            needs_update = eligibility_changed or reason_changed
            processed += 1
            eligible += int(flag)
            changed += int(eligibility_changed)
            downgraded += int(prior == 1 and int(flag) == 0)
            promoted += int(prior == 0 and int(flag) == 1)
            updates_needed += int(needs_update)
            reasons[reason.split(":", 1)[0]] += 1
            if not dry_run and needs_update:
                updates.append((int(flag), reason, str(row["id"])))
        if updates:
            conn.executemany(
                "UPDATE papers SET condmat_view_eligible=?, "
                "condmat_view_reason=? WHERE id=?",
                updates,
            )
            rows_updated += len(updates)
        if commit_batches and updates:
            conn.commit()

    return {
        "processed": processed,
        "eligible": eligible,
        "excluded": processed - eligible,
        "changed": changed,
        "downgraded": downgraded,
        "promoted": promoted,
        "updates_needed": updates_needed,
        "rows_updated": rows_updated,
        "reasons": dict(reasons),
        "skipped": False,
    }


def refresh_generalist_journal_flags(
    conn: sqlite3.Connection,
    batch_size: int = 500,
    *,
    dry_run: bool = False,
    commit_batches: bool = False,
) -> dict[str, Any]:
    """Safely reclassify only multidisciplinary journals under text policy."""
    if dry_run:
        column_cursor = conn.execute("PRAGMA table_info(papers)")
        columns: set[str] = set()
        while True:
            column_rows = column_cursor.fetchmany(64)
            if not column_rows:
                break
            columns.update(str(row["name"]) for row in column_rows)
        column_cursor.close()
        required = {"condmat_view_eligible", "condmat_view_reason"}
        if not required.issubset(columns):
            raise RuntimeError("strict condensed-matter schema is not installed")
    else:
        ensure_strict_condmat_schema(conn)
    result = _refresh_strict_condmat_selection(
        conn,
        journals=tuple(sorted(GENERALIST_JOURNALS)),
        batch_size=batch_size,
        dry_run=dry_run,
        commit_batches=commit_batches,
    )
    return {
        **result,
        "policy_version": GENERALIST_TEXT_POLICY_VERSION,
        "selection_mode": "generalist_journals",
        "journals": sorted(GENERALIST_JOURNALS),
        "dry_run": bool(dry_run),
    }


def apply_strict_condmat_policy_migrations(
    conn: sqlite3.Connection,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Apply one-time, idempotent historical quality-policy migrations."""
    ensure_strict_condmat_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS corpus_quality_policy_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            result_json TEXT NOT NULL DEFAULT '{}'
        )
        """
    )
    existing = conn.execute(
        "SELECT result_json FROM corpus_quality_policy_migrations WHERE version=?",
        (GENERALIST_TEXT_POLICY_VERSION,),
    ).fetchone()
    if existing:
        return {
            "policy_version": GENERALIST_TEXT_POLICY_VERSION,
            "already_applied": True,
            "processed": 0,
            "downgraded": 0,
        }
    result = refresh_generalist_journal_flags(
        conn,
        batch_size=batch_size,
        commit_batches=False,
    )
    conn.execute(
        "INSERT INTO corpus_quality_policy_migrations(version, result_json) "
        "VALUES (?, ?)",
        (
            GENERALIST_TEXT_POLICY_VERSION,
            json.dumps(result, ensure_ascii=False, sort_keys=True),
        ),
    )
    return {**result, "already_applied": False}


def refresh_strict_condmat_flags(
    conn: sqlite3.Connection,
    batch_size: int = 1000,
) -> dict[str, Any]:
    ensure_strict_condmat_schema(conn)
    selection = _refresh_strict_condmat_selection(
        conn,
        batch_size=batch_size,
        commit_batches=True,
    )
    source_quality = reclassify_openalex_repository_quality(conn)
    conn.commit()
    eligible = int(
        conn.execute(
            "SELECT COUNT(*) FROM papers "
            "WHERE data_mode='real' AND COALESCE(condmat_view_eligible,0)=1"
        ).fetchone()[0]
    )
    reasons: Counter[str] = Counter(selection["reasons"])
    if source_quality["repository_candidates"]:
        reasons["openalex_source_quality_gate"] = int(
            source_quality["repository_candidates"]
        )
    return {
        "processed": int(selection["processed"]),
        "eligible": eligible,
        "excluded": int(selection["processed"]) - eligible,
        "changed": int(selection["changed"]),
        "downgraded": int(selection["downgraded"]),
        "promoted": int(selection["promoted"]),
        "updates_needed": int(selection["updates_needed"]),
        "rows_updated": int(selection["rows_updated"]),
        "reasons": dict(reasons),
        "openalex_source_quality": source_quality,
    }

def latest_quality_report_path(exports: Path) -> str | None:
    candidates = sorted(exports.glob("corpus_quality_audit_*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    return str(candidates[0]) if candidates else None


def count_for_preset(conn: sqlite3.Connection, preset: str) -> int:
    predicate, params = corpus_preset_predicate(preset, "p")
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM papers p WHERE {predicate}", tuple(params)).fetchone()["n"])


def strict_counts(conn: sqlite3.Connection) -> dict[str, int]:
    ensure_strict_condmat_schema(conn)
    real = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    strict = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real' AND COALESCE(condmat_view_eligible,0)=1").fetchone()["n"]
    mock = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    strict_core = count_for_preset(conn, "strict_core_published")
    strict_context = count_for_preset(conn, "strict_context_published")
    arxiv_preprint = count_for_preset(conn, "arxiv_preprint")
    return {
        "real_paper_count": int(real),
        "strict_condmat_paper_count": int(strict),
        "strict_core_published_paper_count": int(strict_core),
        "strict_context_published_paper_count": int(strict_context),
        "arxiv_preprint_paper_count": int(arxiv_preprint),
        "excluded_non_condmat_paper_count": int(real - strict),
        "mock_paper_count": int(mock),
    }


def summarize_term_timeseries(conn: sqlite3.Connection, term: str, data_mode: str = STRICT_STATS_MODE, scope: str = "core") -> dict[str, Any]:
    normalized = normalize_term(term)
    rows = conn.execute(
        """
        SELECT month, raw_freq, weighted_freq, momentum
        FROM term_month_stats
        WHERE term = ? AND data_mode = ? AND corpus_scope = ?
        ORDER BY month
        """,
        (normalized, data_mode, scope),
    ).fetchall()
    nonzero = [row for row in rows if int(row["raw_freq"] or 0) > 0]
    if not rows:
        return {"term": normalized, "months": 0, "nonzero_months": 0, "total_raw_freq": 0, "first_seen": None, "latest_seen": None, "peak_month": None, "peak_raw_freq": 0}
    peak = max(rows, key=lambda row: float(row["weighted_freq"] or 0.0))
    bounds = conn.execute(
        "SELECT MIN(month) AS min_month, MAX(month) AS max_month FROM monthly_corpus_stats WHERE data_mode = ?",
        (data_mode,),
    ).fetchone()
    calendar_months = (
        len(month_range(bounds["min_month"], bounds["max_month"]))
        if bounds and bounds["min_month"] and bounds["max_month"]
        else len(rows)
    )
    return {
        "term": normalized,
        "months": calendar_months,
        "nonzero_months": len(nonzero),
        "total_raw_freq": int(sum(int(row["raw_freq"] or 0) for row in rows)),
        "total_weighted_freq": float(sum(float(row["weighted_freq"] or 0.0) for row in rows)),
        "first_seen": nonzero[0]["month"] if nonzero else None,
        "latest_seen": nonzero[-1]["month"] if nonzero else None,
        "peak_month": peak["month"],
        "peak_raw_freq": int(peak["raw_freq"] or 0),
        "peak_weighted_freq": float(peak["weighted_freq"] or 0.0),
    }