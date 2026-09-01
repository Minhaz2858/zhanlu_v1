"""Join Edge Detector — index-time inference of table relationships.

Given introspected tables (each with column metadata and sampled distinct
values), infers join edges of two kinds beyond declared FKs:

- **VALUE_OVERLAP**: containment between the sampled distinct values of two
  type-compatible columns across different tables. Language-agnostic — works
  on opaque abbreviations like ``FMATERIALID`` / ``material_id`` (common in ERP). Confidence
  = containment score.
- **NAME_MATCH**: identical column name + compatible type across two tables,
  *excluding* structural-noise columns. Fixed confidence ``0.5``.

Declared FKs are persisted separately by ``catalog_indexer._persist_relations``;
this module skips any undirected table pair already covered by a declared FK.

Pure functions, no I/O — fully unit-testable. Zero business keywords.
"""

from __future__ import annotations

from typing import Any

# Columns whose *names* are structural noise — they exist on nearly every
# table and would otherwise connect everything to everything via NAME_MATCH.
# This is structural filtering, NOT business-keyword matching.
STRUCTURAL_NOISE_COLUMNS: frozenset[str] = frozenset({
    "id", "created_at", "updated_at", "status", "name", "type", "remark",
    "created_by", "is_deleted", "org_id", "tenant_id",
    "state", "version", "updated_by", "deleted",
})

# Fixed confidence for name-based matches (weaker than FK=1.0 and any
# value-overlap containment that exceeds it).
NAME_MATCH_CONFIDENCE = 0.5

# Default minimum number of shared sampled values required before we even
# consider a value-overlap edge between two columns.
DEFAULT_MIN_SHARED = 3


def type_bucket(data_type: str | None) -> str | None:
    """Classify a column type into a joinable family, or None.

    Only integer and short-string families are join candidates. Float,
    decimal, date, blob, boolean and long text are deliberately excluded —
    they are unsafe join keys.
    """
    dt = (data_type or "").lower()
    if "int" in dt or "serial" in dt:
        return "int"
    if any(k in dt for k in ("char", "varchar", "string", "uuid")):
        return "varchar"
    return None


def _containment(a: set, b: set) -> float:
    inter = len(a & b)
    denom = min(len(a), len(b))
    return inter / denom if denom else 0.0


def _canonical_pair(t1: str, t2: str) -> tuple[str, str]:
    """Deterministic undirected pair ordering (alphabetical)."""
    return (t1, t2) if t1 <= t2 else (t2, t1)


def _declared_fk_pairs(tables: list[dict]) -> set[tuple[str, str]]:
    """Undirected (canonical) table pairs already linked by a declared FK."""
    pairs: set[tuple[str, str]] = set()
    for t in tables:
        src = t.get("table_name", "")
        for fk in t.get("foreign_keys", []) or []:
            ref = fk.get("ref_table", "")
            if src and ref and src != ref:
                pairs.add(_canonical_pair(src, ref))
    return pairs


def _columns_with_samples(tables: list[dict]) -> list[tuple[str, dict]]:
    """Flatten to (table_name, column) for columns with sampled values."""
    out: list[tuple[str, dict]] = []
    for t in tables:
        tname = t.get("table_name", "")
        for c in t.get("columns", []) or []:
            samples = c.get("value_samples") or []
            if c.get("column_name") and samples:
                out.append((tname, c))
    return out


def _detect_value_overlap(
    tables: list[dict], min_shared: int, fk_pairs: set[tuple[str, str]]
) -> list[dict]:
    """Infer VALUE_OVERLAP edges via a type-bucketed inverted index."""
    # (bucket, value) -> list of (table_name, column_name). Bucketing the
    # value by type family prevents int "1" from colliding with varchar "1".
    index: dict[tuple[str, str], list[tuple[str, str]]] = {}
    # (table_name, column_name) -> samples set
    sample_sets: dict[tuple[str, str], set] = {}
    for tname, c in _columns_with_samples(tables):
        bucket = type_bucket(c.get("data_type"))
        if bucket is None:
            continue
        cname = c["column_name"]
        samples = {str(v) for v in (c.get("value_samples") or [])}
        sample_sets[(tname, cname)] = samples
        for v in samples:
            index.setdefault((bucket, v), []).append((tname, cname))

    # Aggregate shared-value counts per cross-table column pair.
    pair_overlap: dict[tuple[tuple[str, str], tuple[str, str]], int] = {}
    for col_pairs in index.values():
        for i in range(len(col_pairs)):
            for j in range(i + 1, len(col_pairs)):
                a, b = col_pairs[i], col_pairs[j]
                if a[0] == b[0]:
                    continue
                if _canonical_pair(a[0], b[0]) in fk_pairs:
                    continue
                pair_overlap[(a, b)] = pair_overlap.get((a, b), 0) + 1

    # Best column pair per table pair (highest containment score).
    best: dict[tuple[str, str], dict] = {}
    for (a, b), overlap in pair_overlap.items():
        if overlap < min_shared:
            continue
        ta, ca = a
        tb, cb = b
        score = _containment(sample_sets[(ta, ca)], sample_sets[(tb, cb)])
        # Canonicalize direction so re-indexing is idempotent.
        if ta <= tb:
            src_table, src_col, tgt_table, tgt_col = ta, ca, tb, cb
        else:
            src_table, src_col, tgt_table, tgt_col = tb, cb, ta, ca
        pair = _canonical_pair(ta, tb)
        entry = best.get(pair)
        if entry is None or score > entry["confidence"]:
            best[pair] = {
                "source_table": src_table,
                "target_table": tgt_table,
                "source_columns": [src_col],
                "target_columns": [tgt_col],
                "kind": "VALUE_OVERLAP",
                "confidence": round(score, 4),
                "evidence": {"overlap_count": overlap, "column_pairs": [
                    {"source_column": src_col, "target_column": tgt_col,
                     "overlap_count": overlap, "score": round(score, 4)}
                ]},
            }

    return list(best.values())


def _detect_name_match(
    tables: list[dict], fk_pairs: set[tuple[str, str]], skip_pairs: set[tuple[str, str]]
) -> list[dict]:
    """Infer NAME_MATCH edges for identical non-noise column names + types."""
    by_name: dict[str, list[tuple[str, dict]]] = {}
    for t in tables:
        tname = t.get("table_name", "")
        for c in t.get("columns", []) or []:
            name = (c.get("column_name") or "").strip().lower()
            if not name or name in STRUCTURAL_NOISE_COLUMNS:
                continue
            if type_bucket(c.get("data_type")) is None:
                continue
            by_name.setdefault(name, []).append((tname, c))

    edges: list[dict] = []
    for name, entries in by_name.items():
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                t1, c1 = entries[i]
                t2, c2 = entries[j]
                if t1 == t2:
                    continue
                if type_bucket(c1.get("data_type")) != type_bucket(c2.get("data_type")):
                    continue
                pair = _canonical_pair(t1, t2)
                if pair in fk_pairs or pair in skip_pairs:
                    continue
                src_col = c1["column_name"] if pair[0] == t1 else c2["column_name"]
                tgt_col = c2["column_name"] if pair[1] == t2 else c1["column_name"]
                edges.append({
                    "source_table": pair[0],
                    "target_table": pair[1],
                    "source_columns": [src_col],
                    "target_columns": [tgt_col],
                    "kind": "NAME_MATCH",
                    "confidence": NAME_MATCH_CONFIDENCE,
                    "evidence": {"shared_column_name": name},
                })
    return edges


def detect_join_edges(
    tables: list[dict],
    min_shared: int = DEFAULT_MIN_SHARED,
) -> list[dict]:
    """Infer join edges across ``tables``.

    Args:
        tables: list of introspected table dicts. Each has ``table_name``,
            ``columns`` (each with ``column_name``, ``data_type``, and
            optional ``value_samples``), and ``foreign_keys``.
        min_shared: minimum shared sampled values to consider a value-overlap.

    Returns:
        List of edge dicts, one per undirected table pair (highest-ranked
        kind wins: VALUE_OVERLAP > NAME_MATCH), sorted by descending
        confidence. Each edge has ``source_table``, ``target_table``,
        ``source_columns``, ``target_columns``, ``kind``, ``confidence``,
        ``evidence``.
    """
    fk_pairs = _declared_fk_pairs(tables)
    value_edges = _detect_value_overlap(tables, min_shared, fk_pairs)
    value_pairs = {_canonical_pair(e["source_table"], e["target_table"]) for e in value_edges}
    name_edges = _detect_name_match(tables, fk_pairs, value_pairs)

    merged: dict[tuple[str, str], dict] = {}
    for e in value_edges + name_edges:
        pair = _canonical_pair(e["source_table"], e["target_table"])
        existing = merged.get(pair)
        if existing is None or e["confidence"] > existing["confidence"]:
            merged[pair] = e

    return sorted(merged.values(), key=lambda e: -e["confidence"])
