"""Schema Linker — query-time table retrieval + join-path expansion.

Given a natural-language question and KB ids, retrieves the most relevant
tables from the semantic catalog (ChromaDB RRF over table + column embeddings),
expands join paths via kb_table_relation, and returns a curated ~800-token
DDL-style slice for the LLM's SQL generation context.

Design:
- Use hybrid_query_collection (dense + sparse + RRF) from hybrid_retrieval.
- Prefer table-level docs; columns boost table scores via RRF aggregation.
- Fallback: return None if no catalog exists → caller uses describe_all.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8       # tables returned to the caller
DEFAULT_HOPS = 2        # max join-path depth
DEFAULT_TOKEN_BUDGET = 800
DENSE_TOP_K = 100       # candidates from Chroma before RRF
# CJK-weighted fusion: the default dense model (MiniLM-L6-v2) is weak on
# Chinese, while bigram lexical matching is reliable — lean on sparse.
CATALOG_DENSE_WEIGHT = 0.25
CATALOG_SPARSE_WEIGHT = 0.75


# ── public API ─────────────────────────────────────────────────────────────

async def link_schema(
    question: str,
    kb_ids: list[str],
    db: Session,
    top_k: int = DEFAULT_TOP_K,
    max_hops: int = DEFAULT_HOPS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
) -> dict | None:
    """Retrieve relevant tables + join paths for a natural-language question.

    Returns None when no catalog collection exists for any kb_id.
    """
    if not settings.SCHEMA_LINKING_ENABLED:
        return None

    # ── 1. RRF-based table retrieval per KB ──
    retrieved: list[dict] = []
    for kb_id in kb_ids:
        hits = _retrieve_from_catalog(question, kb_id, top_k=top_k)
        if hits is not None:
            retrieved.extend(hits)

    if not retrieved:
        return None

    # Deduplicate by table_meta_id (a table may appear in multiple KBs)
    seen: set[str] = set()
    unique: list[dict] = []
    for r in retrieved:
        mid = r.get("table_meta_id", "")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(r)
    retrieved = unique

    # ── 1b. Entity-graph boost (flag-gated, best-effort) ──
    _apply_entity_boost(db, retrieved, question, kb_ids)

    # ── 2. Enrich from DB (descriptions, columns) ──
    enriched = _enrich_tables(db, retrieved, top_k)

    # ── 3. Expand join paths ──
    join_paths = _expand_joins(db, enriched, max_hops=max_hops)

    # ── 4. Format curated slice ──
    slice_text = _format_slice(enriched, join_paths, token_budget)

    result: dict = {
        "tables": enriched,
        "join_paths": join_paths,
        "slice_text": slice_text,
        "total_tables_in_catalog": _count_catalog_tables(db, kb_ids),
    }
    return result


# ── retrieval ──────────────────────────────────────────────────────────────

def _apply_entity_boost(
    db: Session, retrieved: list[dict], question: str, kb_ids: list[str]
) -> None:
    """Boost tables linked to project entities mentioned in the question.

    Flag-gated (ENTITY_GRAPH_ENABLED). Best-effort: never raises.
    Adds a fixed score bonus (0.1) to any retrieved table whose name
    appears in the entity-link graph for entities matched in the question.
    """
    if not getattr(settings, "ENTITY_GRAPH_ENABLED", False):
        return
    try:
        from app.models.knowledge_base import KnowledgeBase
        from app.models.knowledge_catalog import ProjectEntity, ProjectEntityLink

        project_ids = set(
            pid
            for (pid,) in db.query(KnowledgeBase.project_id)
            .filter(
                KnowledgeBase.id.in_(kb_ids),
                KnowledgeBase.project_id.isnot(None),
            )
            .all()
        )
        if not project_ids:
            return

        entities = (
            db.query(ProjectEntity)
            .filter(
                ProjectEntity.project_id.in_(project_ids),
                ProjectEntity.is_deleted == False,  # noqa: E712
            )
            .all()
        )

        q_lower = (question or "").lower()
        matched_table_names: set[str] = set()
        for ent in entities:
            names = [ent.name.lower()] + [
                a.lower() for a in (ent.aliases or [])
            ]
            if any(n and n in q_lower for n in names):
                links = (
                    db.query(ProjectEntityLink)
                    .filter(
                        ProjectEntityLink.entity_id == ent.id,
                        ProjectEntityLink.target_type == "table",
                        ProjectEntityLink.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                for link in links:
                    matched_table_names.add(link.target_id)

        if not matched_table_names:
            return

        for r in retrieved:
            if r.get("table_name", "") in matched_table_names:
                r["rrf_score"] = r.get("rrf_score", 0.0) + 0.1
    except Exception:
        pass  # entity boost is best-effort — never disrupt retrieval


def _retrieve_from_catalog(
    question: str, kb_id: str, top_k: int
) -> list[dict] | None:
    """Query Chroma catalog_{kb_id} collection, return table-level hits or None."""
    from app.services.document_ingestion.store import _get_client
    from app.services.rag.hybrid_retrieval import hybrid_query_collection

    client = _get_client()
    collection_name = f"catalog_{kb_id}"
    try:
        coll = client.get_collection(collection_name)
    except Exception:
        return None  # catalog doesn't exist → graceful fallback

    # Run hybrid query (dense + sparse + RRF), CJK-weighted, full sparse scan
    hits = hybrid_query_collection(
        coll,
        question,
        top_k=DENSE_TOP_K,
        dense_weight=CATALOG_DENSE_WEIGHT,
        sparse_weight=CATALOG_SPARSE_WEIGHT,
        prefetch_limit=0,  # scan the whole catalog collection lexically
    )

    # hits are (doc_id, rrf_score) tuples
    # Extract table-level metadata and aggregate column scores into tables
    table_scores: dict[str, tuple[float, dict]] = {}
    col_best: dict[str, float] = {}  # best column hit per table (max, not sum)
    for doc_id, score in hits:
        try:
            meta = coll.get(ids=[doc_id], include=["metadatas"])
        except Exception:
            continue
        m = (meta.get("metadatas") or [{}])[0] if meta else {}
        kind = m.get("kind", "table")
        mid = m.get("table_meta_id", "")
        if not mid:
            continue

        if kind == "table":
            entry = table_scores.get(mid)
            if entry is None or score > entry[0]:
                table_scores[mid] = (score, {
                    "table_meta_id": mid,
                    "table_name": m.get("table_name", ""),
                    "rrf_score": score,
                    "kb_id": kb_id,
                })
        elif kind == "column":
            # Column evidence surfaces the parent table too — columns
            # outnumber tables ~18:1 in the catalog, so without this the
            # top-K hits can be all columns and retrieval returns empty.
            col_best[mid] = max(col_best.get(mid, 0.0), score)
            if mid not in table_scores:
                table_scores[mid] = (0.0, {
                    "table_meta_id": mid,
                    "table_name": m.get("table_name", ""),
                    "rrf_score": 0.0,
                    "kb_id": kb_id,
                })

    # Final score: best table-doc hit + 0.5 × best column hit. Using max
    # (not sum) for columns prevents wide tables from winning on hit count.
    for mid, best in col_best.items():
        entry = table_scores.get(mid)
        if entry is None:
            continue
        new_score = entry[0] + best * 0.5
        entry_data = dict(entry[1])
        entry_data["rrf_score"] = new_score
        table_scores[mid] = (new_score, entry_data)

    # Return top-k sorted by score
    sorted_tables = sorted(table_scores.values(), key=lambda x: -x[0])
    return [t[1] for t in sorted_tables[:top_k]]


# ── enrichment ─────────────────────────────────────────────────────────────

def _enrich_tables(db: Session, tables: list[dict], top_k: int) -> list[dict]:
    """Fetch full column + description info from kb_table_meta + kb_column_meta."""
    meta_ids = [t["table_meta_id"] for t in tables if t.get("table_meta_id")]
    if not meta_ids:
        return tables

    metas = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.id.in_(meta_ids))
        .all()
    )
    meta_map = {m.id: m for m in metas}

    from app.models.knowledge_catalog import KBColumnMeta
    all_cols = (
        db.query(KBColumnMeta)
        .filter(KBColumnMeta.table_meta_id.in_(meta_ids))
        .order_by(KBColumnMeta.table_meta_id, KBColumnMeta.ordinal)
        .all()
    )
    cols_by_table: dict[str, list] = {}
    for c in all_cols:
        cols_by_table.setdefault(c.table_meta_id, []).append(c)

    result: list[dict] = []
    for t in tables:
        mid = t.get("table_meta_id", "")
        meta = meta_map.get(mid)
        if not meta:
            continue
        cols = cols_by_table.get(mid, [])
        result.append({
            "table_meta_id": mid,
            "table_name": meta.table_name,
            "schema_name": meta.schema_name,
            "table_type": meta.table_type,
            "row_count": meta.row_count,
            "description_zh": meta.description_zh,
            "description_en": meta.description_en,
            "coverage_json": meta.coverage_json,
            "rrf_score": t.get("rrf_score", 0),
            "columns": [
                {
                    "name": c.column_name,
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.is_primary_key,
                    "description_zh": c.description_zh,
                    "description_en": c.description_en,
                }
                for c in cols
            ],
        })
    return result[:top_k]


# ── join-path expansion ────────────────────────────────────────────────────

def _expand_joins(
    db: Session, tables: list[dict], max_hops: int = 2
) -> list[dict]:
    """Find FK join paths among the retrieved tables."""
    meta_ids = [t["table_meta_id"] for t in tables]
    if len(meta_ids) < 2:
        return []

    relations = (
        db.query(KBTableRelation)
        .filter(
            KBTableRelation.source_table_meta_id.in_(meta_ids),
            KBTableRelation.target_table_meta_id.in_(meta_ids),
        )
        .all()
    )

    # Build name lookup
    id_to_name = {t["table_meta_id"]: t["table_name"] for t in tables}

    paths: list[dict] = []
    seen: set[tuple] = set()
    for r in relations:
        src = id_to_name.get(r.source_table_meta_id, "")
        tgt = id_to_name.get(r.target_table_meta_id, "")
        if src and tgt:
            key = (src, tgt)
            if key not in seen:
                seen.add(key)
                paths.append({
                    "from_table": src,
                    "to_table": tgt,
                    "on": _join_on_text(r.source_columns, r.target_columns, src, tgt),
                    "hops": 1,
                    "relation_type": r.relation_type,
                })

    # 2-hop expansion (simplified: self-join through shared FK targets)
    if max_hops >= 2 and len(meta_ids) > 2:
        sources = set(meta_ids)
        second_hop = (
            db.query(KBTableRelation)
            .filter(KBTableRelation.target_table_meta_id.in_(sources))
            .all()
        )
        for r in second_hop:
            src_name = _resolve_name(db, r.source_table_meta_id)
            tgt_name = id_to_name.get(r.target_table_meta_id, "")
            if src_name and tgt_name and src_name not in id_to_name:
                key = (src_name, tgt_name)
                if key not in seen:
                    seen.add(key)
                    paths.append({
                        "from_table": src_name,
                        "to_table": tgt_name,
                        "on": _join_on_text(r.source_columns, r.target_columns, src_name, tgt_name),
                        "hops": 2,
                        "relation_type": r.relation_type,
                    })

    return paths


def _resolve_name(db: Session, meta_id: str) -> str:
    meta = db.query(KBTableMeta).filter(KBTableMeta.id == meta_id).first()
    return meta.table_name if meta else ""


def _join_on_text(
    src_cols: list, tgt_cols: list, src_name: str, tgt_name: str
) -> str:
    if src_cols and tgt_cols:
        return f"{src_name}.{src_cols[0]} = {tgt_name}.{tgt_cols[0]}"
    return ""


# ── formatting ─────────────────────────────────────────────────────────────

def _format_slice(
    tables: list[dict], join_paths: list[dict], token_budget: int
) -> str:
    """Format tables + joins into a curated DDL-style text slice."""
    parts: list[str] = ["-- Semantic catalog: relevant tables for this query\n"]
    used_tokens = _estimate_tokens(parts[0])
    included = 0

    for t in tables:
        # DDL header
        chunk = f"\nCREATE TABLE {t['table_name']} (\n"
        # Columns
        for c in t.get("columns", []):
            nullable = "" if c.get("is_nullable") is False else " NULL"
            pk = " PRIMARY KEY" if c.get("is_primary_key") else ""
            desc = f" -- {c.get('description_zh', '')}" if c.get("description_zh") else ""
            chunk += f"  {c['name']} {c['data_type']}{nullable}{pk}{desc}\n"
        chunk += ");"
        if t.get("description_zh"):
            chunk += f"  -- {t['description_zh']}"
        if t.get("row_count"):
            chunk += f"  -- ~{t['row_count']:,} rows"
        _cov = t.get("coverage_json") or {}
        if _cov.get("max_date"):
            chunk += (
                f"  -- coverage: latest {_cov.get('date_column') or 'date'} = "
                f"{_cov['max_date']}"
            )

        tok = _estimate_tokens(chunk)
        if used_tokens + tok > token_budget:
            break
        parts.append(chunk)
        used_tokens += tok
        included += 1

    # Joins
    if join_paths and included > 0:
        parts.append("\n-- Known join paths:")
        for jp in join_paths:
            line = f"-- {jp['from_table']} → {jp['to_table']} ON {jp['on']} ({jp.get('relation_type', 'FK')})"
            tok = _estimate_tokens(line)
            if used_tokens + tok <= token_budget:
                parts.append(line)
                used_tokens += tok

    logger.debug(
        "schema_linker: %d/%d tables, %d join paths → %d tokens",
        included, len(tables), min(len(join_paths), included), used_tokens,
    )
    return "\n".join(parts)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate — ~2 chars per token for CJK, ~4 for ASCII.
    Use a conservative 2-chars/token estimate since our text is mostly Chinese."""
    return max(1, len(text) // 2)


def _count_catalog_tables(db: Session, kb_ids: list[str]) -> int:
    return (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id.in_(kb_ids))
        .count()
    )
