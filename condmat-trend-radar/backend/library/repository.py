from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from backend.library.deduplication import (
    choose_best_match,
    normalize_author,
    normalize_doi,
    normalize_title,
    parse_arxiv_id,
)


ID_NAMESPACE = uuid.UUID("79efe27d-cf86-46a0-aa16-8a257cd1f6bb")

def _fts_title_phrase(normalized_title: str) -> str:
    """Build an exact-token-order FTS query for an already-normalized title."""
    safe = str(normalized_title or "").replace('"', " ").strip()
    return f'title : "{safe}"' if safe else ""


def _case_exact_candidates(value: str) -> tuple[str, ...]:
    """Return a small set of exact spellings without wrapping indexed columns."""
    original = str(value or "").strip()
    if not original:
        return ()
    return tuple(dict.fromkeys((original, original.lower(), original.upper())))


def _openalex_exact_candidates(value: str) -> tuple[str, ...]:
    """Keep the supplied OpenAlex ID and add its common canonical spellings."""
    original = str(value or "").strip().rstrip("/")
    if not original:
        return ()
    lowered = original.lower()
    entity = original.rsplit("/", 1)[-1]
    canonical_entity = entity[:1].upper() + entity[1:]
    if lowered.startswith(("http://openalex.org/", "https://openalex.org/")):
        canonical = f"https://openalex.org/{canonical_entity}"
    else:
        canonical = canonical_entity
    return tuple(dict.fromkeys((original, lowered, canonical)))


def _in_clause(values: tuple[str, ...]) -> str:
    return ",".join("?" for _ in values)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stable_id(kind: str, value: str) -> str:
    return f"{kind}:{uuid.uuid5(ID_NAMESPACE, f'{kind}:{value}') }"


def json_text(value: Any, default: Any) -> str:
    if value is None:
        value = default
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except json.JSONDecodeError:
            pass
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def record_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in record.items()}


def source_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json_text(payload, {}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def derive_version_type(record: Mapping[str, Any]) -> str:
    explicit = str(record.get("version_type") or "").strip().lower()
    if explicit in {"preprint", "published", "repository", "imported", "metadata"}:
        return explicit
    doi = normalize_doi(record.get("doi"))
    arxiv_id, _ = parse_arxiv_id(record.get("arxiv_id"))
    source = str(record.get("source") or "").lower()
    journal = str(record.get("journal") or "").lower()
    if arxiv_id and not doi and (source == "arxiv" or "arxiv" in journal):
        return "preprint"
    if doi:
        return "published"
    if source in {"legacy_intake", "imported", "repository"}:
        return "imported"
    return "metadata"


@dataclass(frozen=True)
class UpsertResult:
    canonical_paper_id: str
    paper_version_id: str
    action: str
    match_rule: str
    confidence: float
    manual_review_created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_paper_id": self.canonical_paper_id,
            "paper_version_id": self.paper_version_id,
            "action": self.action,
            "match_rule": self.match_rule,
            "confidence": self.confidence,
            "manual_review_created": self.manual_review_created,
        }


class LibraryRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def find_exact_canonical(
        self,
        record: Mapping[str, Any],
    ) -> tuple[str | None, str, float]:
        doi = normalize_doi(record.get("doi"))
        if doi:
            row = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_external_ids "
                "WHERE namespace='doi' AND external_id=? LIMIT 1",
                (doi,),
            ).fetchone()
            if row:
                return str(row["canonical_paper_id"]), "doi_external_id", 1.0
            row = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_versions WHERE doi = ? LIMIT 1",
                (doi,),
            ).fetchone()
            if not row:
                row = self.conn.execute(
                    "SELECT id AS canonical_paper_id FROM papers "
                    "WHERE doi = ? AND doi IS NOT NULL AND doi <> '' LIMIT 1",
                    (doi,),
                ).fetchone()
            if row:
                return str(row["canonical_paper_id"]), "doi_exact", 1.0
        arxiv_id, _ = parse_arxiv_id(record.get("arxiv_id"))
        if arxiv_id:
            arxiv_candidates = _case_exact_candidates(arxiv_id)
            row = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_external_ids "
                "WHERE namespace='arxiv' AND external_id=? LIMIT 1",
                (arxiv_id.lower(),),
            ).fetchone()
            if row:
                return str(row["canonical_paper_id"]), "arxiv_external_id", 0.99
            row = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_versions "
                f"WHERE arxiv_id IN ({_in_clause(arxiv_candidates)}) LIMIT 1",
                arxiv_candidates,
            ).fetchone()
            if not row:
                row = self.conn.execute(
                    "SELECT id AS canonical_paper_id FROM papers "
                    f"WHERE arxiv_id IN ({_in_clause(arxiv_candidates)}) LIMIT 1",
                    arxiv_candidates,
                ).fetchone()
            if row:
                return str(row["canonical_paper_id"]), "arxiv_exact", 0.99
        openalex_id = str(record.get("openalex_id") or "").strip()
        if openalex_id:
            normalized_openalex_id = openalex_id.rstrip("/").lower()
            openalex_candidates = _openalex_exact_candidates(openalex_id)
            row = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_external_ids "
                "WHERE namespace='openalex' AND external_id=? LIMIT 1",
                (normalized_openalex_id,),
            ).fetchone()
            if not row:
                row = self.conn.execute(
                    "SELECT id AS canonical_paper_id FROM papers "
                    f"WHERE openalex_id IN ({_in_clause(openalex_candidates)}) LIMIT 1",
                    openalex_candidates,
                ).fetchone()
            if row:
                return str(row["canonical_paper_id"]), "openalex_exact", 0.995
        return None, "new_library_record", 0.0

    def find_canonical(
        self,
        record: Mapping[str, Any],
    ) -> tuple[str | None, str, float, str | None]:
        """Resolve a stable identity and surface weak title matches for review."""
        canonical_id, rule, confidence = self.find_exact_canonical(record)
        if canonical_id:
            return canonical_id, rule, confidence, None

        normalized = normalize_title(record.get("title"))
        if len(normalized) < 16:
            return None, "new_library_record", 0.0, None
        # The old fallback filtered only by year and then grouped every
        # version/author in a three-year slice.  That work grows with the whole
        # corpus.  Generate a small candidate set from two existing indexes:
        # the title B-tree catches byte-exact titles and FTS catches
        # punctuation/case variants with the same token sequence.  The strict
        # normalized-title, first-author and year checks remain authoritative.
        display_title = str(record.get("title") or "").strip()
        rows_by_version: dict[str, sqlite3.Row] = {}
        if display_title:
            exact_rows = self.conn.execute(
                """
                SELECT v.*, p.year AS canonical_year,
                       p.doi AS canonical_doi,
                       p.arxiv_id AS canonical_arxiv_id,
                       a.display_name AS matched_first_author
                FROM paper_versions v INDEXED BY idx_paper_versions_title
                JOIN papers p ON p.id=v.canonical_paper_id
                LEFT JOIN paper_authors pa
                  ON pa.paper_version_id=v.id AND pa.author_position=0
                LEFT JOIN authors a ON a.id=pa.author_id
                WHERE v.title=?
                """,
                (display_title,),
            ).fetchall()
            rows_by_version.update((str(row["id"]), row) for row in exact_rows)

        fts_expression = _fts_title_phrase(normalized)
        if fts_expression:
            try:
                fts_rows = self.conn.execute(
                    """
                    SELECT v.*, p.year AS canonical_year,
                           p.doi AS canonical_doi,
                           p.arxiv_id AS canonical_arxiv_id,
                           a.display_name AS matched_first_author
                    FROM library_fts
                    JOIN paper_versions v ON v.id=library_fts.paper_version_id
                    JOIN papers p ON p.id=v.canonical_paper_id
                    LEFT JOIN paper_authors pa
                      ON pa.paper_version_id=v.id AND pa.author_position=0
                    LEFT JOIN authors a ON a.id=pa.author_id
                    WHERE library_fts MATCH ?
                    """,
                    (fts_expression,),
                ).fetchall()
                rows_by_version.update((str(row["id"]), row) for row in fts_rows)
            except sqlite3.OperationalError as exc:
                # A partially initialized legacy DB still gets the indexed
                # byte-exact lookup.  Do not hide other SQLite failures.
                if "no such table: library_fts" not in str(exc).lower():
                    raise

        candidates: list[dict[str, Any]] = []
        for row in rows_by_version.values():
            item = dict(row)
            if normalize_title(item.get("title")) != normalized:
                continue
            item["year"] = item.get("canonical_year")
            if not item.get("doi") and item.get("canonical_doi"):
                item["doi"] = item["canonical_doi"]
            if not item.get("arxiv_id") and item.get("canonical_arxiv_id"):
                item["arxiv_id"] = item["canonical_arxiv_id"]
            first_author = str(item.pop("matched_first_author", "") or "")
            if first_author:
                item["authors"] = [first_author]
            candidates.append(item)
        candidate, decision = choose_best_match(record, candidates)
        if not candidate:
            return None, "new_library_record", 0.0, None
        candidate_id = str(candidate["canonical_paper_id"])
        if decision.auto_merge:
            return candidate_id, decision.rule, decision.confidence, None
        return None, decision.rule, decision.confidence, candidate_id
    def upsert_version(
        self,
        record: Mapping[str, Any],
        *,
        source: str,
        source_record_id: str,
        legacy_id: str | None = None,
        default_condmat_eligible: bool = False,
        create_corpus_review: bool = True,
    ) -> UpsertResult:
        existing_version = self.conn.execute(
            "SELECT id, canonical_paper_id FROM paper_versions "
            "WHERE source = ? AND source_record_id = ?",
            (source, source_record_id),
        ).fetchone()
        now = utc_now()
        payload = record_payload(record)
        if existing_version:
            version_id = str(existing_version["id"])
            canonical_id = str(existing_version["canonical_paper_id"])
            self._update_version(version_id, payload, now)
            self._record_observation(version_id, source, source_record_id, payload, now)
            return UpsertResult(
                canonical_id,
                version_id,
                "updated_existing_version",
                "source_record_exact",
                1.0,
                False,
            )

        canonical_id, match_rule, confidence, review_candidate_id = self.find_canonical(payload)
        created_canonical = canonical_id is None
        if created_canonical:
            canonical_id = stable_id("paper", f"{source}:{source_record_id}")
            self._create_canonical(
                canonical_id,
                payload,
                source,
                now,
                default_condmat_eligible,
            )
        version_id = stable_id("version", f"{source}:{source_record_id}")
        self._insert_version(version_id, canonical_id, payload, source, source_record_id, now)
        self._record_external_ids(canonical_id, version_id, payload, source, now)
        self._record_observation(version_id, source, source_record_id, payload, now)
        if legacy_id:
            self.conn.execute(
                """
                INSERT INTO legacy_id_aliases
                (source_project, legacy_id, canonical_paper_id, paper_version_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_project, legacy_id) DO UPDATE SET
                    canonical_paper_id = excluded.canonical_paper_id,
                    paper_version_id = excluded.paper_version_id,
                    reason = excluded.reason
                """,
                (
                    source,
                    legacy_id,
                    canonical_id,
                    version_id,
                    match_rule,
                    now,
                ),
            )
        if not created_canonical:
            event_id = stable_id("merge", f"{source}:{source_record_id}:{canonical_id}")
            self.conn.execute(
                """
                INSERT OR IGNORE INTO paper_merge_events
                (id, match_rule, confidence, source_record_id, source_paper_id,
                 target_paper_id, reversible, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    event_id,
                    match_rule,
                    confidence,
                    source_record_id,
                    legacy_id,
                    canonical_id,
                    json_text(payload, {}),
                    now,
                ),
            )
        review_created = False
        if created_canonical and review_candidate_id:
            review_id = stable_id("review", f"identity-title:{source}:{source_record_id}:{review_candidate_id}")
            review_payload = {
                "incoming": payload,
                "proposed_target_paper_id": review_candidate_id,
                "match_rule": match_rule,
            }
            self.conn.execute(
                """
                INSERT OR IGNORE INTO manual_review_items
                (id, review_type, status, source_project, source_record_id,
                 candidate_paper_id, reason, confidence, payload_json, created_at)
                VALUES (?, 'identity_match', 'pending', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    review_id,
                    source,
                    source_record_id,
                    canonical_id,
                    "title candidate was not strong enough for an automatic merge",
                    confidence,
                    json_text(review_payload, {}),
                    now,
                ),
            )
            review_created = True
        elif created_canonical and create_corpus_review and not default_condmat_eligible:
            review_id = stable_id("review", f"corpus:{source}:{source_record_id}")
            self.conn.execute(
                """
                INSERT OR IGNORE INTO manual_review_items
                (id, review_type, status, source_project, source_record_id,
                 candidate_paper_id, reason, confidence, payload_json, created_at)
                VALUES (?, 'corpus_eligibility', 'pending', ?, ?, ?, ?, NULL, ?, ?)
                """,
                (
                    review_id,
                    source,
                    source_record_id,
                    canonical_id,
                    "legacy library record has no exact Radar DOI/arXiv match",
                    json_text(payload, {}),
                    now,
                ),
            )
            review_created = True
        self._upsert_authors(version_id, payload, now)
        return UpsertResult(
            canonical_id,
            version_id,
            "created_canonical" if created_canonical else "attached_version",
            match_rule,
            confidence,
            review_created,
        )

    def _create_canonical(
        self,
        canonical_id: str,
        record: Mapping[str, Any],
        source: str,
        now: str,
        condmat_eligible: bool,
    ) -> None:
        title = str(record.get("title") or "Untitled")
        date = str(record.get("publication_date") or record.get("submitted_date") or "")
        year = int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else record.get("year")
        month = date[:7] if len(date) >= 7 else None
        doi = normalize_doi(record.get("doi"))
        arxiv_id, _ = parse_arxiv_id(record.get("arxiv_id"))
        self.conn.execute(
            """
            INSERT INTO papers (
                id, doi, title, abstract, journal, publication_date, year, month,
                source, source_scope, data_mode, condmat_confidence, url, pdf_url,
                is_open_access, oa_status, openalex_id, arxiv_id, cited_by_count,
                raw_json, download_status, zotero_export_status,
                condmat_view_eligible, condmat_view_reason, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, 'library_only', 'real', 'unreviewed',
                ?, ?, ?, ?, ?, ?, ?, ?, 'not_requested', 'not_requested', ?, ?, ?, ?
            )
            """,
            (
                canonical_id,
                doi,
                title,
                record.get("abstract") or "",
                record.get("journal") or "Unknown",
                date,
                year,
                month,
                source,
                record.get("url") or "",
                record.get("pdf_url") or "",
                1 if record.get("is_open_access") or record.get("pdf_url") else 0,
                record.get("oa_status") or "",
                record.get("openalex_id") or "",
                arxiv_id or "",
                int(record.get("citation_count") or record.get("cited_by_count") or 0),
                json_text(record.get("raw_json") or record.get("raw") or record, {}),
                1 if condmat_eligible else 0,
                "legacy_library_exact_scope" if condmat_eligible else "pending_manual_review",
                now,
                now,
            ),
        )

    def _insert_version(
        self,
        version_id: str,
        canonical_id: str,
        record: Mapping[str, Any],
        source: str,
        source_record_id: str,
        now: str,
    ) -> None:
        arxiv_id, arxiv_version = parse_arxiv_id(record.get("arxiv_id"))
        self.conn.execute(
            """
            INSERT INTO paper_versions (
                id, canonical_paper_id, version_type, title, abstract, doi,
                arxiv_id, arxiv_version, journal, publication_date, submitted_date,
                updated_date, source, source_record_id, journal_reference,
                url, pdf_url, raw_json, first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_id,
                canonical_id,
                derive_version_type(record),
                record.get("title") or "Untitled",
                record.get("abstract") or "",
                normalize_doi(record.get("doi")),
                arxiv_id,
                arxiv_version,
                record.get("journal") or "",
                record.get("publication_date") or "",
                record.get("submitted_date") or "",
                record.get("updated_date") or "",
                source,
                source_record_id,
                record.get("journal_reference") or "",
                record.get("url") or "",
                record.get("pdf_url") or "",
                json_text(record.get("raw_json") or record.get("raw") or record, {}),
                record.get("created_at") or now,
                record.get("last_seen_at") or record.get("updated_at") or now,
            ),
        )

        # New versions must be visible to cross-source identity matching in the
        # same scan.  Keep this append-only: deleting by the UNINDEXED FTS
        # metadata column would itself scan the entire virtual table.
        try:
            self.conn.execute(
                """
                INSERT INTO library_fts
                (canonical_paper_id, paper_version_id, title, abstract, body)
                VALUES (?, ?, ?, ?, '')
                """,
                (
                    canonical_id,
                    version_id,
                    record.get("title") or "Untitled",
                    record.get("abstract") or "",
                ),
            )
        except sqlite3.OperationalError as exc:
            if "no such table: library_fts" not in str(exc).lower():
                raise

    def _update_version(
        self,
        version_id: str,
        record: Mapping[str, Any],
        now: str,
    ) -> None:
        arxiv_id, arxiv_version = parse_arxiv_id(record.get("arxiv_id"))
        self.conn.execute(
            """
            UPDATE paper_versions SET
                title = COALESCE(NULLIF(?, ''), title),
                abstract = COALESCE(NULLIF(?, ''), abstract),
                doi = COALESCE(?, doi),
                arxiv_id = COALESCE(?, arxiv_id),
                arxiv_version = COALESCE(?, arxiv_version),
                journal = COALESCE(NULLIF(?, ''), journal),
                publication_date = COALESCE(NULLIF(?, ''), publication_date),
                updated_date = COALESCE(NULLIF(?, ''), updated_date),
                pdf_url = COALESCE(NULLIF(?, ''), pdf_url),
                raw_json = ?,
                last_seen_at = ?
            WHERE id = ?
            """,
            (
                record.get("title") or "",
                record.get("abstract") or "",
                normalize_doi(record.get("doi")),
                arxiv_id,
                arxiv_version,
                record.get("journal") or "",
                record.get("publication_date") or "",
                record.get("updated_date") or "",
                record.get("pdf_url") or "",
                json_text(record.get("raw_json") or record.get("raw") or record, {}),
                now,
                version_id,
            ),
        )

    def _record_external_ids(
        self,
        canonical_id: str,
        version_id: str,
        record: Mapping[str, Any],
        source: str,
        now: str,
    ) -> None:
        values: list[tuple[str, str]] = []
        doi = normalize_doi(record.get("doi"))
        arxiv_id, _ = parse_arxiv_id(record.get("arxiv_id"))
        openalex_id = str(record.get("openalex_id") or "").strip()
        if doi:
            values.append(("doi", doi))
        if arxiv_id:
            values.append(("arxiv", arxiv_id.lower()))
        if openalex_id:
            values.append(("openalex", openalex_id.lower()))
        for namespace, external_id in values:
            existing = self.conn.execute(
                "SELECT canonical_paper_id FROM paper_external_ids "
                "WHERE namespace = ? AND external_id = ?",
                (namespace, external_id),
            ).fetchone()
            if existing and existing["canonical_paper_id"] != canonical_id:
                review_id = stable_id(
                    "review", f"identity:{namespace}:{external_id}:{canonical_id}"
                )
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO manual_review_items
                    (id, review_type, status, source_project, source_record_id,
                     candidate_paper_id, reason, payload_json, created_at)
                    VALUES (?, 'identity_conflict', 'pending', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        review_id,
                        source,
                        external_id,
                        canonical_id,
                        f"{namespace} is already assigned to another canonical paper",
                        json_text(record, {}),
                        now,
                    ),
                )
                continue
            self.conn.execute(
                """
                INSERT OR IGNORE INTO paper_external_ids
                (namespace, external_id, canonical_paper_id, paper_version_id, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (namespace, external_id, canonical_id, version_id, source, now),
            )

    def _record_observation(
        self,
        version_id: str,
        source: str,
        source_record_id: str,
        payload: Mapping[str, Any],
        now: str,
    ) -> None:
        payload_json = json_text(payload, {})
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO paper_version_observations
            (paper_version_id, source_project, source_record_id, observed_at,
             payload_sha256, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (version_id, source, source_record_id, now, payload_hash, payload_json),
        )

    def _upsert_authors(
        self,
        version_id: str,
        record: Mapping[str, Any],
        now: str,
    ) -> None:
        authors = record.get("authors") or record.get("author_names") or []
        if isinstance(authors, str):
            authors = [item.strip() for item in authors.split(";") if item.strip()]
        if not isinstance(authors, Iterable) or isinstance(authors, (str, bytes, Mapping)):
            return
        for position, value in enumerate(authors):
            if isinstance(value, Mapping):
                raw_name = str(
                    value.get("display_name")
                    or value.get("name")
                    or value.get("author", {}).get("display_name")
                    or ""
                )
                orcid = str(
                    value.get("orcid")
                    or value.get("author", {}).get("orcid")
                    or ""
                )
                affiliations = value.get("affiliations") or value.get("institutions") or []
                corresponding = bool(value.get("is_corresponding"))
            else:
                raw_name = str(value)
                orcid = ""
                affiliations = []
                corresponding = False
            normalized = normalize_author(raw_name)
            if not normalized:
                continue
            author_key = orcid.lower() or normalized
            author_id = stable_id("author", author_key)
            self.conn.execute(
                """
                INSERT INTO authors
                (id, display_name, normalized_name, orcid, affiliations_json,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    display_name = excluded.display_name,
                    orcid = COALESCE(NULLIF(excluded.orcid, ''), authors.orcid),
                    affiliations_json = excluded.affiliations_json,
                    updated_at = excluded.updated_at
                """,
                (
                    author_id,
                    raw_name,
                    normalized,
                    orcid or None,
                    json_text(affiliations, []),
                    now,
                    now,
                ),
            )
            self.conn.execute(
                """
                INSERT OR REPLACE INTO paper_authors
                (paper_version_id, author_id, author_position, is_corresponding, raw_name)
                VALUES (?, ?, ?, ?, ?)
                """,
                (version_id, author_id, position, 1 if corresponding else 0, raw_name),
            )


def seed_radar_versions(conn: sqlite3.Connection, *, updated_since: str | None = None) -> dict[str, Any]:
    before = int(conn.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0])
    now = utc_now()
    paper_filter = "COALESCE(source, '') <> 'legacy_intake'"
    filter_params: tuple[str, ...] = ()
    if updated_since:
        paper_filter += " AND updated_at >= ?"
        filter_params = (updated_since,)
    version_ids = [
        f"radar-version:{row[0]}"
        for row in conn.execute(f"SELECT id FROM papers WHERE {paper_filter}", filter_params)
    ] if updated_since else []
    conn.execute(
        f"""
        INSERT INTO paper_versions (
            id, canonical_paper_id, version_type, title, abstract, doi,
            arxiv_id, arxiv_version, journal, publication_date, submitted_date,
            updated_date, source, source_record_id, journal_reference,
            url, pdf_url, raw_json, first_seen_at, last_seen_at
        )
        SELECT
            'radar-version:' || id,
            id,
            CASE
                WHEN COALESCE(arxiv_id, '') <> '' AND COALESCE(doi, '') = '' THEN 'preprint'
                WHEN COALESCE(doi, '') <> '' THEN 'published'
                ELSE 'metadata'
            END,
            title,
            COALESCE(abstract, ''),
            NULLIF(doi, ''),
            NULLIF(arxiv_id, ''),
            NULL,
            COALESCE(journal, ''),
            COALESCE(publication_date, ''),
            '',
            COALESCE(updated_at, ''),
            'radar_legacy',
            id,
            '',
            COALESCE(url, ''),
            COALESCE(pdf_url, ''),
            COALESCE(raw_json, '{{}}'),
            COALESCE(created_at, ?),
            COALESCE(updated_at, created_at, ?)
        FROM papers
        WHERE {paper_filter}
        ON CONFLICT(id) DO UPDATE SET
            title=excluded.title,
            abstract=CASE WHEN length(trim(excluded.abstract))>0 THEN excluded.abstract ELSE paper_versions.abstract END,
            doi=COALESCE(excluded.doi, paper_versions.doi),
            arxiv_id=COALESCE(excluded.arxiv_id, paper_versions.arxiv_id),
            journal=CASE WHEN excluded.journal<>'' THEN excluded.journal ELSE paper_versions.journal END,
            publication_date=CASE WHEN excluded.publication_date<>'' THEN excluded.publication_date ELSE paper_versions.publication_date END,
            updated_date=excluded.updated_date,
            url=CASE WHEN excluded.url<>'' THEN excluded.url ELSE paper_versions.url END,
            pdf_url=CASE WHEN excluded.pdf_url<>'' THEN excluded.pdf_url ELSE paper_versions.pdf_url END,
            raw_json=excluded.raw_json,
            last_seen_at=excluded.last_seen_at
        """,
        (now, now, *filter_params),
    )
    for namespace, column in (("doi", "doi"), ("arxiv", "arxiv_id"), ("openalex", "openalex_id")):
        conn.execute(
            f"""
            INSERT OR IGNORE INTO paper_external_ids
            (namespace, external_id, canonical_paper_id, paper_version_id, source, created_at)
            SELECT ?, lower({column}), id, 'radar-version:' || id, 'radar_legacy', ?
            FROM papers
            WHERE {column} IS NOT NULL AND {column} <> '' AND {paper_filter}
            """,
            (namespace, now, *filter_params),
        )
    conn.execute(
        f"""
        INSERT OR IGNORE INTO legacy_id_aliases
        (source_project, legacy_id, canonical_paper_id, paper_version_id, reason, created_at)
        SELECT 'condmat-trend-radar', id, id, 'radar-version:' || id,
               'existing_canonical_id', ? FROM papers
        WHERE {paper_filter}
        """,
        (now, *filter_params),
    )
    after = int(conn.execute("SELECT COUNT(*) FROM paper_versions").fetchone()[0])
    return {"before": before, "after": after, "inserted": after - before, "incremental": int(bool(updated_since)), "version_ids": version_ids}

def seed_radar_authors(conn: sqlite3.Connection) -> dict[str, int]:
    repository = LibraryRepository(conn)
    before = int(conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0])
    processed = 0
    failed = 0
    rows = conn.execute(
        """
        SELECT id, authorships_json FROM papers
        WHERE authorships_json IS NOT NULL
          AND authorships_json NOT IN ('', '[]', 'null')
        """
    )
    for row in rows:
        try:
            authors = json.loads(row["authorships_json"])
            repository._upsert_authors(
                f"radar-version:{row['id']}", {"authors": authors}, utc_now()
            )
            processed += 1
        except (json.JSONDecodeError, TypeError, sqlite3.Error):
            failed += 1
    after = int(conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0])
    return {
        "source_papers_processed": processed,
        "failed": failed,
        "authors_before": before,
        "authors_after": after,
        "authors_inserted": after - before,
    }


def seed_topics_and_materials(conn: sqlite3.Connection, *, paper_ids: Iterable[str] | None = None) -> dict[str, int]:
    from backend.nlp.material_registry import MATERIAL_REGISTRY

    ids = list(dict.fromkeys(str(item) for item in (paper_ids or []) if item))
    if paper_ids is not None and not ids:
        return {
            "materials": int(conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]),
            "material_aliases": int(conn.execute("SELECT COUNT(*) FROM material_aliases").fetchone()[0]),
            "paper_material_links_seen": 0,
            "topics": int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]),
            "paper_topic_links_seen": 0,
        }
    now = utc_now()
    for canonical, entry in MATERIAL_REGISTRY.items():
        material_id = stable_id("material", canonical.lower())
        conn.execute(
            """
            INSERT INTO materials
            (id, canonical_name, material_family, phase, thickness, composition,
             is_platform_material, created_at, updated_at)
            VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            ON CONFLICT(canonical_name) DO UPDATE SET
                material_family = excluded.material_family,
                is_platform_material = excluded.is_platform_material,
                updated_at = excluded.updated_at
            """,
            (
                material_id,
                canonical,
                entry.material_family,
                canonical,
                1 if entry.is_platform_material else 0,
                now,
                now,
            ),
        )
        for alias in (canonical, *entry.aliases):
            conn.execute(
                """
                INSERT OR IGNORE INTO material_aliases
                (alias, normalized_alias, material_id, source, created_at)
                VALUES (?, ?, ?, 'material_registry', ?)
                """,
                (alias, normalize_title(alias), material_id, now),
            )
    term_sql = "SELECT paper_id, term, term_type, normalized_term, confidence, source FROM paper_terms"
    term_params: list[Any] = []
    if ids:
        term_sql += f" WHERE paper_id IN ({','.join('?' for _ in ids)})"
        term_params.extend(ids)
    term_rows = conn.execute(term_sql, term_params).fetchall()
    material_links = 0
    topic_links = 0
    for row in term_rows:
        if row["term_type"] == "material":
            material = conn.execute(
                "SELECT id FROM materials WHERE lower(canonical_name) = lower(?)",
                (row["normalized_term"],),
            ).fetchone()
            if not material:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_materials
                (canonical_paper_id, material_id, confidence, source, context_json, created_at)
                VALUES (?, ?, ?, ?, '{}', ?)
                """,
                (
                    row["paper_id"],
                    material["id"],
                    float(row["confidence"] or 0),
                    row["source"] or "legacy_paper_terms",
                    now,
                ),
            )
            material_links += 1
        else:
            name = str(row["normalized_term"] or row["term"])
            topic_id = stable_id("topic", f"{row['term_type']}:{name.lower()}")
            conn.execute(
                """
                INSERT OR IGNORE INTO topics(id, canonical_name, topic_type, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (topic_id, name, row["term_type"], now),
            )
            existing_topic = conn.execute(
                "SELECT id FROM topics WHERE canonical_name = ?",
                (name,),
            ).fetchone()
            if not existing_topic:
                continue
            topic_id = str(existing_topic["id"])
            conn.execute(
                """
                INSERT OR IGNORE INTO paper_topics
                (canonical_paper_id, topic_id, confidence, source, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    row["paper_id"],
                    topic_id,
                    float(row["confidence"] or 0),
                    row["source"] or "legacy_paper_terms",
                    now,
                ),
            )
            topic_links += 1
    return {
        "materials": int(conn.execute("SELECT COUNT(*) FROM materials").fetchone()[0]),
        "material_aliases": int(
            conn.execute("SELECT COUNT(*) FROM material_aliases").fetchone()[0]
        ),
        "paper_material_links_seen": material_links,
        "topics": int(conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]),
        "paper_topic_links_seen": topic_links,
    }
