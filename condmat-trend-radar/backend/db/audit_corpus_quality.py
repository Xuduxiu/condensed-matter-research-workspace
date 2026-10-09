from __future__ import annotations

import csv
import json
from datetime import datetime
from typing import Any

from backend.config import export_dir
from backend.db.database import connect, init_db
from backend.db.strict_condmat import (
    EXCLUDED_NON_CONDMAT_TERMS,
    corpus_preset_label,
    ensure_strict_condmat_schema,
    refresh_strict_condmat_flags,
    strict_counts,
)
from backend.nlp.normalize import normalize_term

STATUS_TERMS = ["hBN", "graphene", "ARPES", "STM", "DFT"]
SUMMARY_TERMS = ["ZrTe5", "moiré superlattice", "fractional Chern insulator", "altermagnetism"]


def dict_rows(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def grouped(conn, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return dict_rows(conn.execute(sql, params).fetchall())


def top_terms(conn, term_type: str | None = None, display_eligible: int | None = None, strict: bool = False, limit: int = 100) -> list[dict[str, Any]]:
    where = ["p.data_mode = 'real'"]
    params: list[Any] = []
    if strict:
        where.append("COALESCE(p.condmat_view_eligible, 0) = 1")
    if term_type:
        where.append("pt.term_type = ?")
        params.append(term_type)
    if display_eligible is not None:
        where.append("pt.display_eligible = ?")
        params.append(display_eligible)
    return grouped(
        conn,
        f"""
        SELECT pt.normalized_term AS term, pt.term_type, pt.display_reason, COUNT(DISTINCT p.id) AS count
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE {' AND '.join(where)}
        GROUP BY pt.normalized_term, pt.term_type, pt.display_reason
        ORDER BY count DESC, term ASC
        LIMIT ?
        """,
        (*params, limit),
    )


def examples(conn, eligible: int, limit: int = 12) -> list[dict[str, Any]]:
    reason_clause = "AND p.condmat_view_reason LIKE 'excluded_non_condmat:%'" if not eligible else ""
    return grouped(
        conn,
        f"""
        SELECT p.id, p.doi, p.title, p.journal, p.year, p.source, p.condmat_confidence,
               p.condmat_view_eligible, p.condmat_view_reason
        FROM papers p
        WHERE p.data_mode = 'real' AND COALESCE(p.condmat_view_eligible, 0) = ? {reason_clause}
        ORDER BY p.year DESC, p.journal ASC, p.title ASC
        LIMIT ?
        """,
        (eligible, limit),
    )


def term_status(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    row = conn.execute(
        """
        SELECT COUNT(DISTINCT p.id) AS all_real_count,
               SUM(CASE WHEN COALESCE(p.condmat_view_eligible,0)=1 THEN 1 ELSE 0 END) AS strict_count,
               MIN(p.month) AS first_seen,
               MAX(p.month) AS latest_seen
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE p.data_mode='real' AND pt.normalized_term = ?
        """,
        (normalized,),
    ).fetchone()
    lifecycle = conn.execute("SELECT concept, status, concept_class, is_platform_term, historical_first_seen, trend_total_count FROM concept_lifecycle WHERE concept = ?", (normalized,)).fetchone()
    return {
        "term": normalized,
        "all_real_count": int(row["all_real_count"] or 0),
        "strict_count": int(row["strict_count"] or 0),
        "first_seen": row["first_seen"],
        "latest_seen": row["latest_seen"],
        "lifecycle": dict(lifecycle) if lifecycle else None,
    }


def term_timeseries_summary(conn, term: str) -> dict[str, Any]:
    normalized = normalize_term(term)
    rows = conn.execute(
        """
        SELECT p.month, COUNT(DISTINCT p.id) AS raw_freq
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE p.data_mode='real' AND COALESCE(p.condmat_view_eligible,0)=1
          AND pt.normalized_term = ? AND p.month IS NOT NULL
        GROUP BY p.month ORDER BY p.month
        """,
        (normalized,),
    ).fetchall()
    if not rows:
        return {"term": normalized, "nonzero_months": 0, "total_raw_freq": 0, "first_seen": None, "latest_seen": None, "peak_month": None, "peak_raw_freq": 0}
    peak = max(rows, key=lambda row: int(row["raw_freq"] or 0))
    return {
        "term": normalized,
        "nonzero_months": len(rows),
        "total_raw_freq": int(sum(int(row["raw_freq"] or 0) for row in rows)),
        "first_seen": rows[0]["month"],
        "latest_seen": rows[-1]["month"],
        "peak_month": peak["month"],
        "peak_raw_freq": int(peak["raw_freq"] or 0),
        "recent_points": [dict(row) for row in rows[-12:]],
    }


def build_payload(conn) -> dict[str, Any]:
    ensure_strict_condmat_schema(conn)
    refresh_result = refresh_strict_condmat_flags(conn)
    counts = strict_counts(conn)
    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "corpus_preset": "strict_condmat",
        "corpus_label": corpus_preset_label("strict_condmat"),
        "strict_refresh": refresh_result,
        "counts": counts,
        "by_journal": grouped(conn, "SELECT journal, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY journal ORDER BY count DESC"),
        "by_year": grouped(conn, "SELECT year, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY year ORDER BY year"),
        "by_source": grouped(conn, "SELECT source, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY source ORDER BY count DESC"),
        "by_source_scope": grouped(conn, "SELECT source_scope, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY source_scope ORDER BY count DESC"),
        "by_condmat_confidence": grouped(conn, "SELECT COALESCE(condmat_confidence, 'missing') AS condmat_confidence, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY COALESCE(condmat_confidence, 'missing') ORDER BY count DESC"),
        "by_strict_reason": grouped(conn, "SELECT substr(condmat_view_reason, 1, instr(condmat_view_reason || ':', ':') - 1) AS reason, COUNT(*) AS count FROM papers WHERE data_mode='real' GROUP BY reason ORDER BY count DESC"),
        "doi_coverage": dict(conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN doi IS NOT NULL AND doi != '' THEN 1 ELSE 0 END) AS with_doi FROM papers WHERE data_mode='real'").fetchone()),
        "abstract_coverage": dict(conn.execute("SELECT COUNT(*) AS total, SUM(CASE WHEN abstract IS NOT NULL AND length(trim(abstract)) > 0 THEN 1 ELSE 0 END) AS with_abstract FROM papers WHERE data_mode='real'").fetchone()),
        "title_only_papers_count": conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real' AND title IS NOT NULL AND (abstract IS NULL OR length(trim(abstract)) = 0)").fetchone()["n"],
        "likely_non_condmat_examples": examples(conn, 0),
        "likely_condmat_examples": examples(conn, 1),
        "top_100_raw_extracted_terms": top_terms(conn, limit=100),
        "top_100_display_eligible_concepts": top_terms(conn, term_type="concept", display_eligible=1, strict=True, limit=100),
        "top_100_materials": top_terms(conn, term_type="material", display_eligible=1, strict=True, limit=100),
        "top_100_methods": top_terms(conn, term_type="method", display_eligible=1, strict=True, limit=100),
        "top_excluded_generic_terms": top_terms(conn, display_eligible=0, strict=False, limit=100),
        "explicit_exclusion_terms": EXCLUDED_NON_CONDMAT_TERMS,
        "status_terms": {term: term_status(conn, term) for term in STATUS_TERMS},
        "time_series_summary": {term: term_timeseries_summary(conn, term) for term in SUMMARY_TERMS},
    }
    payload["confidence_buckets"] = {row["condmat_confidence"]: row["count"] for row in payload["by_condmat_confidence"]}
    return payload


def markdown_table(rows: list[dict[str, Any]], columns: list[str], limit: int | None = None) -> str:
    rows = rows[:limit] if limit else rows
    if not rows:
        return "_No rows._\n"
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(col, "") or "").replace("|", "\\|") for col in columns) + " |")
    return "\n".join(lines) + "\n"


def write_markdown(payload: dict[str, Any], path) -> None:
    counts = payload["counts"]
    lines = [
        "# Corpus Quality Audit",
        "",
        f"Generated at: `{payload['generated_at']}`",
        f"Current display corpus: **{payload['corpus_label']}**",
        "",
        "## Summary",
        f"- total real papers: {counts['real_paper_count']}",
        f"- total mock papers: {counts['mock_paper_count']}",
        f"- strict condensed matter papers: {counts['strict_condmat_paper_count']}",
        f"- excluded non-condmat papers: {counts['excluded_non_condmat_paper_count']}",
        f"- DOI coverage: {payload['doi_coverage']['with_doi']} / {payload['doi_coverage']['total']}",
        f"- abstract coverage: {payload['abstract_coverage']['with_abstract']} / {payload['abstract_coverage']['total']}",
        f"- title-only papers count: {payload['title_only_papers_count']}",
        "",
        "## By Condmat Confidence",
        markdown_table(payload["by_condmat_confidence"], ["condmat_confidence", "count"]),
        "## By Journal",
        markdown_table(payload["by_journal"], ["journal", "count"], 40),
        "## By Year",
        markdown_table(payload["by_year"], ["year", "count"]),
        "## By Source",
        markdown_table(payload["by_source"], ["source", "count"]),
        "## By Source Scope",
        markdown_table(payload["by_source_scope"], ["source_scope", "count"]),
        "## Strict Exclusion Reasons",
        markdown_table(payload["by_strict_reason"], ["reason", "count"], 40),
        "## Likely Non-Condmat Examples",
        markdown_table(payload["likely_non_condmat_examples"], ["journal", "year", "condmat_confidence", "condmat_view_reason", "title"], 12),
        "## Likely Condmat Examples",
        markdown_table(payload["likely_condmat_examples"], ["journal", "year", "condmat_confidence", "condmat_view_reason", "title"], 12),
        "## Top 100 Raw Extracted Terms",
        markdown_table(payload["top_100_raw_extracted_terms"], ["term", "term_type", "display_reason", "count"], 100),
        "## Top 100 Display Eligible Concepts",
        markdown_table(payload["top_100_display_eligible_concepts"], ["term", "term_type", "display_reason", "count"], 100),
        "## Top 100 Materials",
        markdown_table(payload["top_100_materials"], ["term", "term_type", "display_reason", "count"], 100),
        "## Top 100 Methods",
        markdown_table(payload["top_100_methods"], ["term", "term_type", "display_reason", "count"], 100),
        "## Top Excluded Generic Terms",
        markdown_table(payload["top_excluded_generic_terms"], ["term", "term_type", "display_reason", "count"], 100),
        "## Status Terms",
        "```json",
        json.dumps(payload["status_terms"], ensure_ascii=False, indent=2),
        "```",
        "## Time Series Summary",
        "```json",
        json.dumps(payload["time_series_summary"], ensure_ascii=False, indent=2),
        "```",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_csv(payload: dict[str, Any], path) -> None:
    rows: list[dict[str, Any]] = []
    for key, value in payload["counts"].items():
        rows.append({"section": "counts", "key": key, "value": value, "extra": ""})
    for section in ["by_journal", "by_year", "by_source", "by_source_scope", "by_condmat_confidence", "by_strict_reason"]:
        for row in payload[section]:
            rows.append({"section": section, "key": row.get("journal") or row.get("year") or row.get("source") or row.get("source_scope") or row.get("condmat_confidence") or row.get("reason"), "value": row.get("count"), "extra": ""})
    for section in ["top_100_raw_extracted_terms", "top_100_display_eligible_concepts", "top_100_materials", "top_100_methods", "top_excluded_generic_terms"]:
        for row in payload[section]:
            rows.append({"section": section, "key": row.get("term"), "value": row.get("count"), "extra": f"{row.get('term_type','')}|{row.get('display_reason','')}"})
    for term, value in payload["status_terms"].items():
        rows.append({"section": "status_terms", "key": term, "value": value.get("strict_count"), "extra": json.dumps(value, ensure_ascii=False)})
    for term, value in payload["time_series_summary"].items():
        rows.append({"section": "time_series_summary", "key": term, "value": value.get("total_raw_freq"), "extra": json.dumps(value, ensure_ascii=False)})
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["section", "key", "value", "extra"])
        writer.writeheader()
        writer.writerows(rows)


def main() -> dict[str, str]:
    with connect() as conn:
        init_db(conn)
        payload = build_payload(conn)
    out_dir = export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = out_dir / f"corpus_quality_audit_{stamp}"
    md_path = base.with_suffix(".md")
    json_path = base.with_suffix(".json")
    csv_path = base.with_suffix(".csv")
    write_markdown(payload, md_path)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(payload, csv_path)
    result = {"markdown": str(md_path), "json": str(json_path), "csv": str(csv_path)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    main()