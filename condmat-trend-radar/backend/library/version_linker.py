from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Callable

from backend.library.deduplication import normalize_doi, normalize_title
from backend.library.repository import stable_id, utc_now


DOI_PATTERN = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.I)


def doi_from_journal_reference(value: str | None) -> str | None:
    if not value:
        return None
    match = DOI_PATTERN.search(value)
    return normalize_doi(match.group(0).rstrip(".,;)")) if match else None


def _raw_doi(raw_json: str | None) -> str | None:
    try:
        raw = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return None
    for key in ("doi", "published_doi", "journal_doi"):
        doi = normalize_doi(raw.get(key)) if isinstance(raw, dict) else None
        if doi:
            return doi
    return None


def link_preprints_and_publications(
    connection: sqlite3.Connection,
    *,
    dry_run: bool = True,
    batch_size: int = 100,
    progress_callback: Callable[[int, int, int, int], None] | None = None,
    commit_batches: bool = False,
) -> dict[str, Any]:
    """Link preprints in bounded, independently replayable source batches.

    Defaults preserve the caller-owned transaction. When ``commit_batches`` is
    enabled, every source paper in a committed batch has its link row,
    canonical reassignment, source-paper backlink, and merge event completed
    together. Re-running is idempotent because all durable records use stable
    IDs or ``INSERT OR IGNORE``.
    """
    size = max(1, int(batch_size))
    published_titles: dict[str, list[dict[str, Any]]] = {}
    for candidate in connection.execute(
        "SELECT id, canonical_paper_id, title, doi FROM paper_versions WHERE version_type='published'"
    ):
        key = normalize_title(candidate["title"])
        if key and len(published_titles.setdefault(key, [])) < 2:
            published_titles[key].append(dict(candidate))

    source_sql = """
        SELECT v.*, p.year
        FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id
        WHERE v.arxiv_id IS NOT NULL AND v.arxiv_id <> '' AND (v.doi IS NULL OR v.doi='')
        ORDER BY v.id
    """
    total = int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM paper_versions v JOIN papers p ON p.id=v.canonical_paper_id
            WHERE v.arxiv_id IS NOT NULL AND v.arxiv_id <> '' AND (v.doi IS NULL OR v.doi='')
            """
        ).fetchone()[0]
    )
    source_cursor = connection.execute(source_sql)
    planned: list[dict[str, Any]] = []
    review_plans: list[dict[str, Any]] = []
    processed = links_seen = reviews_seen = 0

    links_before = reviews_before = 0
    now = utc_now()
    if not dry_run:
        links_before = int(connection.execute(
            "SELECT COUNT(*) FROM paper_version_links WHERE link_type='preprint_to_publication'"
        ).fetchone()[0])
        reviews_before = int(connection.execute(
            "SELECT COUNT(*) FROM manual_review_items WHERE review_type='version_link'"
        ).fetchone()[0])

    def classify(source: sqlite3.Row) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        doi = doi_from_journal_reference(source["journal_reference"]) or _raw_doi(source["raw_json"])
        target = None
        rule = ""
        confidence = 0.0
        if doi:
            target = connection.execute(
                "SELECT * FROM paper_versions WHERE lower(doi)=lower(?) "
                "ORDER BY CASE version_type WHEN 'published' THEN 0 ELSE 1 END LIMIT 1",
                (doi,),
            ).fetchone()
            rule = "journal_reference_doi_exact"
            confidence = 1.0
        if target and target["id"] != source["id"]:
            return (
                {
                    "source_version_id": source["id"],
                    "target_version_id": target["id"],
                    "source_canonical_id": source["canonical_paper_id"],
                    "target_canonical_id": target["canonical_paper_id"],
                    "match_rule": rule,
                    "confidence": confidence,
                    "doi": doi,
                },
                None,
            )
        title_key = normalize_title(source["title"])
        if not title_key:
            return None, None
        candidates = [
            item for item in published_titles.get(title_key, [])
            if item["id"] != source["id"]
        ]
        if not candidates:
            return None, None
        return (
            None,
            {
                "source_version_id": source["id"],
                "candidate_version_ids": [row["id"] for row in candidates],
                "reason": "title-only publication candidate requires manual review",
            },
        )

    def apply_link(item: dict[str, Any]) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_version_links
            (source_version_id, target_version_id, link_type, match_rule,
             confidence, reversible, created_at)
            VALUES (?, ?, 'preprint_to_publication', ?, ?, 1, ?)
            """,
            (item["source_version_id"], item["target_version_id"], item["match_rule"], item["confidence"], now),
        )
        connection.execute(
            "UPDATE paper_versions SET canonical_paper_id=? WHERE id=?",
            (item["target_canonical_id"], item["source_version_id"]),
        )
        connection.execute(
            "UPDATE papers SET linked_published_paper_id=? WHERE id=?",
            (item["target_canonical_id"], item["source_canonical_id"]),
        )
        event_id = stable_id("merge", f"version-link:{item['source_version_id']}:{item['target_version_id']}")
        connection.execute(
            """
            INSERT OR IGNORE INTO paper_merge_events
            (id, match_rule, confidence, source_record_id, source_paper_id,
             target_paper_id, reversible, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (event_id, item["match_rule"], item["confidence"], item["source_version_id"], item["source_canonical_id"], item["target_canonical_id"], json.dumps(item), now),
        )

    def apply_review(item: dict[str, Any]) -> None:
        review_id = stable_id("review", f"version-link:{item['source_version_id']}")
        connection.execute(
            """
            INSERT OR IGNORE INTO manual_review_items
            (id, review_type, status, source_project, source_record_id,
             reason, payload_json, created_at)
            VALUES (?, 'version_link', 'pending', 'unified_library', ?, ?, ?, ?)
            """,
            (review_id, item["source_version_id"], item["reason"], json.dumps(item), now),
        )

    while True:
        rows = source_cursor.fetchmany(size)
        if not rows:
            break
        for source in rows:
            link_item, review_item = classify(source)
            if link_item is not None:
                links_seen += 1
                if dry_run:
                    planned.append(link_item)
                else:
                    apply_link(link_item)
            if review_item is not None:
                reviews_seen += 1
                if dry_run:
                    review_plans.append(review_item)
                else:
                    apply_review(review_item)
            processed += 1
        if progress_callback is not None:
            progress_callback(processed, total, links_seen, reviews_seen)
        if not dry_run and commit_batches:
            connection.commit()

    if total == 0 and progress_callback is not None:
        progress_callback(0, 0, 0, 0)
        if not dry_run and commit_batches:
            connection.commit()

    if dry_run:
        return {
            "status": "PASS",
            "dry_run": True,
            "links_planned": links_seen,
            "reviews_planned": reviews_seen,
            "links": planned,
            "processed": processed,
            "total": total,
            "batch_size": size,
        }

    links_after = int(connection.execute(
        "SELECT COUNT(*) FROM paper_version_links WHERE link_type='preprint_to_publication'"
    ).fetchone()[0])
    reviews_after = int(connection.execute(
        "SELECT COUNT(*) FROM manual_review_items WHERE review_type='version_link'"
    ).fetchone()[0])
    return {
        "status": "PASS",
        "dry_run": False,
        "links_created": links_after - links_before,
        "reviews_created": reviews_after - reviews_before,
        "links_seen": links_seen,
        "reviews_seen": reviews_seen,
        "processed": processed,
        "total": total,
        "batch_size": size,
    }