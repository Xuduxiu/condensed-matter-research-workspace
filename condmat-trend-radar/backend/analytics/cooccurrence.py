from __future__ import annotations

import itertools
import sqlite3
from collections import Counter
from typing import Any


def cooccurrence_network(conn: sqlite3.Connection, min_count: int = 2) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT paper_id, normalized_term
        FROM paper_terms
        WHERE term_type = 'concept'
        ORDER BY paper_id, normalized_term
        """
    ).fetchall()
    by_paper: dict[str, set[str]] = {}
    for row in rows:
        by_paper.setdefault(row["paper_id"], set()).add(row["normalized_term"])

    node_counts: Counter[str] = Counter()
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in by_paper.values():
        node_counts.update(terms)
        for a, b in itertools.combinations(sorted(terms), 2):
            edge_counts[(a, b)] += 1

    valid_edges = [(pair, count) for pair, count in edge_counts.items() if count >= min_count]
    connected = {term for pair, _count in valid_edges for term in pair}
    nodes = [
        {"id": term, "name": term, "value": count}
        for term, count in node_counts.most_common()
        if term in connected or count >= min_count
    ]
    links = [
        {"source": a, "target": b, "value": count}
        for (a, b), count in valid_edges
    ]
    return {"nodes": nodes, "links": links}


# --- Data-mode aware co-occurrence override. ---
def cooccurrence_network(conn: sqlite3.Connection, min_count: int = 2, data_mode: str = "auto") -> dict[str, Any]:
    real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
    mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
    mode = "real" if (data_mode in {"auto", "real"} and real_count) else "mock" if data_mode != "mixed" and mock_count else "mixed"
    rows = conn.execute(
        """
        SELECT pt.paper_id, pt.normalized_term
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE pt.term_type = 'concept' AND pt.display_eligible = 1 AND (? = 'mixed' OR p.data_mode = ?)
        ORDER BY pt.paper_id, pt.normalized_term
        """,
        (mode, mode),
    ).fetchall()
    by_paper: dict[str, set[str]] = {}
    for row in rows:
        by_paper.setdefault(row["paper_id"], set()).add(row["normalized_term"])
    node_counts: Counter[str] = Counter()
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in by_paper.values():
        node_counts.update(terms)
        for a, b in itertools.combinations(sorted(terms), 2):
            edge_counts[(a, b)] += 1
    valid_edges = [(pair, count) for pair, count in edge_counts.items() if count >= min_count]
    connected = {term for pair, _count in valid_edges for term in pair}
    nodes = [{"id": term, "name": term, "value": count} for term, count in node_counts.most_common() if term in connected or count >= min_count]
    links = [{"source": a, "target": b, "value": count} for (a, b), count in valid_edges]
    return {"nodes": nodes, "links": links, "data_mode": mode}

# --- Corpus-preset aware co-occurrence override. ---
def cooccurrence_network(conn: sqlite3.Connection, min_count: int = 2, data_mode: str = "auto") -> dict[str, Any]:
    requested = (data_mode or "auto").lower()
    if requested == "strict_condmat":
        mode = "strict_condmat"
        where = "p.data_mode = 'real' AND COALESCE(p.condmat_view_eligible, 0) = 1"
        params: tuple[Any, ...] = ()
    else:
        real_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='real'").fetchone()["n"]
        mock_count = conn.execute("SELECT COUNT(*) AS n FROM papers WHERE data_mode='mock'").fetchone()["n"]
        mode = "real" if (requested in {"auto", "real"} and real_count) else "mock" if requested != "mixed" and mock_count else "mixed"
        where = "(? = 'mixed' OR p.data_mode = ?)"
        params = (mode, mode)
    rows = conn.execute(
        f"""
        SELECT pt.paper_id, pt.normalized_term
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE pt.term_type = 'concept' AND pt.display_eligible = 1 AND {where}
        ORDER BY pt.paper_id, pt.normalized_term
        """,
        params,
    ).fetchall()
    by_paper: dict[str, set[str]] = {}
    for row in rows:
        by_paper.setdefault(row["paper_id"], set()).add(row["normalized_term"])
    node_counts: Counter[str] = Counter()
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in by_paper.values():
        node_counts.update(terms)
        for a, b in itertools.combinations(sorted(terms), 2):
            edge_counts[(a, b)] += 1
    valid_edges = [(pair, count) for pair, count in edge_counts.items() if count >= min_count]
    connected = {term for pair, _count in valid_edges for term in pair}
    nodes = [{"id": term, "name": term, "value": count} for term, count in node_counts.most_common() if term in connected or count >= min_count]
    links = [{"source": a, "target": b, "value": count} for (a, b), count in valid_edges]
    return {"nodes": nodes, "links": links, "data_mode": mode}
# --- Ego-network helpers for large real corpora. ---
from backend.db.strict_condmat import corpus_preset_predicate, preset_for_stats_mode, stats_mode_for_preset
from backend.nlp.normalize import normalize_term


def _mode_predicate(data_mode: str, alias: str = "p") -> tuple[str, list[Any], str]:
    requested = (data_mode or "strict_core_published").lower()
    if requested == "auto" or requested == "strict_condmat":
        requested = stats_mode_for_preset("strict_core_published")
    preset = preset_for_stats_mode(requested)
    if preset:
        sql, params = corpus_preset_predicate(preset, alias)
        return sql, params, requested
    if requested == "mixed":
        return "1=1", [], requested
    return f"{alias}.data_mode = ?", [requested], requested


def cooccurrence_network(conn: sqlite3.Connection, min_count: int = 2, data_mode: str = "strict_core_published") -> dict[str, Any]:
    where, params, mode = _mode_predicate(data_mode, "p")
    rows = conn.execute(
        f"""
        SELECT pt.paper_id, pt.normalized_term
        FROM paper_terms pt JOIN papers p ON p.id = pt.paper_id
        WHERE pt.term_type = 'concept' AND pt.display_eligible = 1 AND {where}
        ORDER BY pt.paper_id, pt.normalized_term
        """,
        tuple(params),
    ).fetchall()
    by_paper: dict[str, set[str]] = {}
    for row in rows:
        by_paper.setdefault(row["paper_id"], set()).add(row["normalized_term"])
    node_counts: Counter[str] = Counter()
    edge_counts: Counter[tuple[str, str]] = Counter()
    for terms in by_paper.values():
        node_counts.update(terms)
        for a, b in itertools.combinations(sorted(terms), 2):
            edge_counts[(a, b)] += 1
    valid_edges = [(pair, count) for pair, count in edge_counts.items() if count >= min_count]
    connected = {term for pair, _count in valid_edges for term in pair}
    nodes = [{"id": term, "name": term, "value": count} for term, count in node_counts.most_common(200) if term in connected or count >= min_count]
    node_set = {node["id"] for node in nodes}
    links = [{"source": a, "target": b, "value": count} for (a, b), count in valid_edges if a in node_set and b in node_set]
    return {"nodes": nodes, "links": links, "data_mode": mode}


def ego_cooccurrence_network(conn: sqlite3.Connection, concept: str, top_n_nodes: int = 20, min_edge_weight: int = 1, data_mode: str = "strict_core_published") -> dict[str, Any]:
    root = normalize_term(concept)
    where, params, mode = _mode_predicate(data_mode, "p")
    rows = conn.execute(
        f"""
        SELECT other.normalized_term AS term, COUNT(DISTINCT p.id) AS weight
        FROM paper_terms root_term
        JOIN papers p ON p.id = root_term.paper_id
        JOIN paper_terms other ON other.paper_id = p.id
        WHERE root_term.normalized_term = ?
          AND other.normalized_term != ?
          AND other.term_type = 'concept'
          AND other.display_eligible = 1
          AND {where}
        GROUP BY other.normalized_term
        HAVING weight >= ?
        ORDER BY weight DESC, term ASC
        LIMIT ?
        """,
        (root, root, *params, min_edge_weight, max(1, top_n_nodes)),
    ).fetchall()
    nodes = [{"id": root, "name": root, "value": sum(int(row["weight"] or 0) for row in rows)}]
    links = []
    for row in rows:
        term = row["term"]
        weight = int(row["weight"] or 0)
        nodes.append({"id": term, "name": term, "value": weight})
        links.append({"source": root, "target": term, "value": weight})
    return {"root": root, "nodes": nodes, "links": links, "data_mode": mode}
