from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from backend.library.repository import stable_id, utc_now
from backend.nlp.material_extract import extract_materials, valid_dynamic_formula
from backend.nlp.material_registry import MATERIAL_REGISTRY
from backend.nlp.normalize import normalize_term


def material_family(name: str) -> str:
    entry = MATERIAL_REGISTRY.get(name)
    if entry:
        return entry.material_family
    elements = set(part for part in ("O", "S", "Se", "Te", "P", "As", "Sb", "N") if part in name)
    if "O" in elements:
        return "paper-extracted oxide"
    if elements & {"S", "Se", "Te"}:
        return "paper-extracted chalcogenide"
    if elements & {"P", "As", "Sb", "N"}:
        return "paper-extracted pnictide"
    return "paper-extracted material"


def _context(title: str, abstract: str, material: str, detector: str) -> dict[str, Any]:
    field = "title" if detector.endswith("_title") or material.casefold() in title.casefold() else "abstract"
    text = title if field == "title" else abstract
    folded = " ".join((text or "").split())
    lower = folded.casefold()
    at = lower.find(material.casefold())
    if at < 0:
        snippet = folded[:360]
    else:
        snippet = folded[max(0, at - 110): at + len(material) + 190]
    return {"field": field, "snippet": snippet, "detector": detector, "derived_from_paper_text": True}


def sync_materials_from_papers(
    connection: sqlite3.Connection,
    *,
    paper_ids: Iterable[str] | None = None,
    seen_since: str | None = None,
    limit: int | None = None,
) -> dict[str, int]:
    clauses = ["p.data_mode='real'", "p.condmat_view_eligible=1"]
    params: list[Any] = []
    ids = list(dict.fromkeys(str(item) for item in (paper_ids or []) if item))
    if ids:
        marks = ",".join("?" for _ in ids)
        clauses.append(f"p.id IN ({marks})")
        params.extend(ids)
    if seen_since:
        clauses.append("p.updated_at>=?")
        params.append(seen_since)
    sql = f"""
        SELECT p.id, p.title,
               COALESCE(NULLIF(p.abstract,''), (
                 SELECT v.abstract FROM paper_versions v
                 WHERE v.canonical_paper_id=p.id AND length(trim(COALESCE(v.abstract,'')))>0
                 ORDER BY length(v.abstract) DESC LIMIT 1
               ), '') AS abstract
        FROM papers p
        WHERE {' AND '.join(clauses)}
        ORDER BY p.updated_at DESC
    """
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(1, int(limit)))
    rows = connection.execute(sql, tuple(params)).fetchall()
    before = int(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0])
    # Paper-derived evidence is replaceable output, not an append-only cache. This
    # removes stale partial formulae after extractor improvements while preserving
    # imported/manual material links from the original radar.
    changes_before = connection.total_changes
    connection.executemany(
        "DELETE FROM paper_materials WHERE canonical_paper_id=? AND source IN ('paper_text_formula','paper_text_registry')",
        ((str(row["id"]),) for row in rows),
    )
    links_replaced = connection.total_changes - changes_before
    scanned = links_created = links_updated = evidence_count = 0
    now = utc_now()
    for row in rows:
        scanned += 1
        title = str(row["title"] or "")
        abstract = str(row["abstract"] or "")
        for item in extract_materials(title, abstract):
            canonical = str(item.get("normalized_term") or item.get("term") or "").strip()
            if not canonical:
                continue
            material_id = stable_id("material", canonical.casefold())
            entry = MATERIAL_REGISTRY.get(canonical)
            family = material_family(canonical)
            composition = canonical if valid_dynamic_formula(canonical) else (entry.canonical if entry else canonical)
            stored = connection.execute(
                "SELECT id, canonical_name, material_family FROM materials WHERE id=? OR lower(canonical_name)=lower(?) LIMIT 1",
                (material_id, canonical),
            ).fetchone()
            if stored:
                material_id = str(stored["id"])
                canonical = str(stored["canonical_name"])
                connection.execute(
                    """
                    UPDATE materials SET
                      material_family=CASE
                        WHEN material_family IN ('quantum material','paper-extracted material')
                        THEN ? ELSE material_family END,
                      composition=COALESCE(NULLIF(composition,''), ?), updated_at=?
                    WHERE id=?
                    """,
                    (family, composition, now, material_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO materials
                    (id, canonical_name, material_family, phase, thickness, composition,
                     is_platform_material, created_at, updated_at)
                    VALUES (?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (material_id, canonical, family, composition, int(bool(entry and entry.is_platform_material)), now, now),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO material_aliases(alias, normalized_alias, material_id, source, created_at)
                VALUES (?, ?, ?, 'paper_text_extractor', ?)
                """,
                (str(item.get("term") or canonical), normalize_term(str(item.get("term") or canonical)), material_id, now),
            )
            detector = str(item.get("detector") or "paper_text")
            source = "paper_text_formula" if detector.startswith("formula_ner") else "paper_text_registry"
            context = _context(title, abstract, canonical, detector)
            existed = connection.execute(
                "SELECT 1 FROM paper_materials WHERE canonical_paper_id=? AND material_id=? AND source=?",
                (row["id"], material_id, source),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO paper_materials
                (canonical_paper_id, material_id, confidence, source, context_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(canonical_paper_id, material_id, source) DO UPDATE SET
                  confidence=MAX(paper_materials.confidence, excluded.confidence),
                  context_json=excluded.context_json
                """,
                (row["id"], material_id, float(item.get("confidence") or 0.7), source, json.dumps(context, ensure_ascii=False), now),
            )
            if existed:
                links_updated += 1
            else:
                links_created += 1
            evidence_count += 1
    after = int(connection.execute("SELECT COUNT(*) FROM materials").fetchone()[0])
    return {
        "papers_scanned": scanned,
        "materials_before": before,
        "materials_after": after,
        "materials_discovered": max(0, after - before),
        "links_created": links_created,
        "links_updated": links_updated,
        "links_replaced": links_replaced,
        "evidence_count": evidence_count,
    }
