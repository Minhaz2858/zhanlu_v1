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
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation

logger = logging.getLogger(__name__)


# ── Schema-linker allowlist (2026-08-25) ───────────────────────────
# When the calling agent's domain config opts in
# (schema_linker_allowlist_enabled + schema_linker_table_allowlist),
# restrict retrieval to a curated table list (business views + price
# views + masters). This avoids wrong-table picks like intelligence_events
# or decision_log shadowing real sales data, and cuts search time.
def _resolve_table_allowlist_for_kb(db: Session, kb_ids: list[str]) -> list[str] | None:
    """Return a per-app table allowlist when the calling project binds to an
    agent that ships a domain config with ``schema_linker_allowlist_enabled``.

    DE-HARDCODED (2026-08-27): the allowlist no longer lives in settings —
    it is per-app DATA loaded from ``domain_configs/<agent_name>.json``. Any
    app can opt in with its own table list; apps without a config get no
    restriction (fully generic schema discovery).
    """
    if not kb_ids:
        return None
    try:
        from app.services.domain_config import get_schema_allowlist
        from app.models.agent_app import AgentApp
        from app.models.knowledge_base import KnowledgeBase
        target_kb_ids = {k for k in kb_ids if k}
        if not target_kb_ids:
            return None
        apps = (
            db.query(AgentApp)
            .filter(
                AgentApp.is_deleted == False,  # noqa: E712
            )
            .all()
        )
        for app in apps:
            allowlist = get_schema_allowlist(app.name)
            if not allowlist:
                continue
            # Binding is project-scoped: the agent inherits every KB whose
            # project_id matches the agent's project.  (The AgentApp JSON
            # `knowledge_bases` column is NOT populated in this deployment;
            # matching on it alone silently disables the allowlist.)
            bound = set(app.knowledge_bases or [])
            if app.project_id:
                # Home project OR legacy name OR explicit share link
                # (multi-project sharing, 2026-10-31).
                from sqlalchemy import or_ as _or

                from app.services.project_kb_membership import (
                    project_kb_link_subquery,
                )

                proj_kbs = (
                    db.query(KnowledgeBase.id)
                    .filter(
                        _or(
                            KnowledgeBase.project_id == app.project_id,
                            KnowledgeBase.id.in_(
                                project_kb_link_subquery(
                                    app.project_id,
                                    org_id=getattr(app, "org_id", None),
                                    app_id=getattr(app, "app_id", None),
                                )
                            ),
                        ),
                        KnowledgeBase.is_deleted == False,  # noqa: E712
                    )
                    .all()
                )
                bound |= {row[0] for row in proj_kbs}
            if bound & target_kb_ids:
                logger.info(
                    "schema_linker: allowlist ENABLED for project %s (agent=%s, %d tables)",
                    app.project_id, app.name, len(allowlist),
                )
                return list(allowlist)
    except Exception as e:
        logger.debug("schema_linker: allowlist resolve failed (non-fatal): %s", e)
    return None

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
    # The caller (db_tools / nl_answer_service) already checks the
    # SCHEMA_LINKING_ENABLED flag and context-level opt-in before calling
    # this function, so we do NOT re-check settings.SCHEMA_LINKING_ENABLED
    # here.  This allows context-level schema_linking_enabled=True to work
    # even when the global default is off.

    # ── 1. RRF-based table retrieval per KB ──
    retrieved: list[dict] = []
    table_allowlist = _resolve_table_allowlist_for_kb(db, kb_ids)
    for kb_id in kb_ids:
        hits = _retrieve_from_catalog(
            question, kb_id, top_k=top_k,
            table_allowlist=table_allowlist,
        )
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

    # ── 2b. Data-freshness annotation (generic, live DB) ──
    # Tables whose newest row stopped arriving long ago are flagged
    # STALE so the LLM won't pick a dead table for a recent-period
    # question. Works for any connected database — no hardcoded names.
    try:
        from app.services.knowledge_graph.freshness import annotate_tables
        by_kb: dict[str, list[dict]] = {}
        for _t in enriched:
            by_kb.setdefault(_t.get("kb_id") or "", []).append(_t)
        for _kb_id, _tabs in by_kb.items():
            if _kb_id:
                annotate_tables(db, _kb_id, _tabs)
    except Exception as _fexc:  # noqa: BLE001 — freshness must never break linking
        logger.debug("schema_linker: freshness annotation skipped: %s", _fexc)

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
    question: str, kb_id: str, top_k: int,
    table_allowlist: list[str] | None = None,
) -> list[dict] | None:
    """Query Chroma catalog_{kb_id} collection, return table-level hits or None.

    When ``table_allowlist`` is provided, post-filter hits so only docs whose
    ``table_name`` metadata is in the allowlist survive.  This is the
    Enterprise-BI narrow-search optimization.
    """
    from app.services.document_ingestion.store import _get_client
    from app.services.rag.hybrid_retrieval import hybrid_query_collection

    client = _get_client()
    collection_name = f"catalog_{kb_id}"
    allowlist_set = (
        {t.lower() for t in table_allowlist} if table_allowlist else None
    )
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

        # ── Allowlist post-filter ──
        # If a table_name is in metadata but NOT in the allowlist, drop
        # the hit.  This prevents AI-domain tables (intelligence_events,
        # decision_log) from shadowing real sales tables.
        if allowlist_set:
            tname = (m.get("table_name") or "").lower()
            if tname and tname not in allowlist_set:
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
            "kb_id": meta.kb_id,
            "table_name": meta.table_name,
            "schema_name": meta.schema_name,
            "table_type": meta.table_type,
            "row_count": meta.row_count,
            "description_zh": meta.description_zh,
            "description_en": meta.description_en,
            "coverage_json": meta.coverage_json,
            "domain_name": getattr(meta, "domain_name", None),
            "grain": getattr(meta, "grain", None),
            "rrf_score": t.get("rrf_score", 0),
            "columns": [
                {
                    "name": c.column_name,
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.is_primary_key,
                    "description_zh": c.description_zh,
                    "description_en": c.description_en,
                    # P0-4: provenance feeds the [desc: ...] markers.
                    "description_source": getattr(c, "description_source", None),
                    "semantic_type": getattr(c, "semantic_type", None),
                    "enum_values": getattr(c, "enum_values", None),
                    # The measured distribution (slice 2). Rendered into the
                    # DDL slice by _format_slice; absent (None) for a column
                    # that was never measured, which is why the renderer emits
                    # nothing rather than a zero.
                    "stats": getattr(c, "stats_json", None),
                    "unit": getattr(c, "unit", None),
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
                    "confidence": r.confidence if r.confidence is not None else 1.0,
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
                        "confidence": r.confidence if r.confidence is not None else 1.0,
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

def _render_table_chunk(t: dict, *, detail: str = "full") -> str:
    """One table as a DDL chunk at one of three detail levels.

    ``full``   — the value dictionary AND the measured distribution.
    ``stats``  — the measured distribution only: the value dictionary is the
                 longest annotation and the first thing to give way when the
                 prompt budget is tight, while the distribution (null rate,
                 cardinality, range) is what stops a wrong column choice.
    ``plain``  — neither (the pre-slice-2 content).

    Even ``plain`` keeps the description, semantic type/unit, freshness flag
    and coverage: those are the reasons the author can use the table at all.
    """
    from app.services.knowledge_graph.freshness import stale_flag
    from app.services.knowledge_graph import column_stats

    chunk = f"\nCREATE TABLE {t['table_name']} (\n"
    for c in t.get("columns", []):
        nullable = "" if c.get("is_nullable") is False else " NULL"
        pk = " PRIMARY KEY" if c.get("is_primary_key") else ""
        annotations: list[str] = []
        if c.get("description_zh"):
            annotations.append(str(c["description_zh"]))
        elif c.get("description_en"):
            annotations.append(str(c["description_en"]))
        # P0-4: column-description provenance — visible next to the text it
        # describes, so a guessed meaning can never impersonate a verified one.
        _cprov = description_provenance_tag(c.get("description_source"))
        if _cprov and (c.get("description_zh") or c.get("description_en")):
            annotations.append(f"[desc: {_cprov}]")
        stype = c.get("semantic_type")
        unit = c.get("unit")
        if stype and stype not in ("unknown", "identifier"):
            annotations.append(f"{stype}" + (f", unit={unit}" if unit else ""))
        elif unit:
            annotations.append(f"unit={unit}")
        if detail == "full":
            enums = c.get("enum_values")
            if isinstance(enums, list) and enums:
                shown = ", ".join(str(v) for v in enums[:12])
                annotations.append(f"values: {shown[:80]}")
        if detail in ("full", "stats"):
            # The MEASUREMENT (slice 2): null rate, cardinality, range. It is
            # what tells the SQL author that a column is unusable (97 % NULL),
            # that a filter value must sit inside a range, or that a column is
            # effectively one record per row. Every figure carries its own
            # provenance ("全表" vs "N 行抽样"), so a sampled bound can never be
            # read as a measured total. Empty for an unmeasured column.
            _measured = column_stats.stats_annotation(c.get("stats"))
            if _measured:
                annotations.append(_measured)
        desc = f" -- {' | '.join(annotations)}" if annotations else ""
        chunk += f"  {c.get('name') or c.get('column_name')} {c.get('data_type', '')}{nullable}{pk}{desc}\n"
    chunk += ");"
    if t.get("domain_name"):
        chunk += f"  -- domain: {t['domain_name']}"
    # P-SV: grain ("one row = what") — the SQL author must know the row
    # semantics BEFORE it aggregates (e.g. a per-order grain table cannot be
    # summed together with a per-month rollup).
    if t.get("grain"):
        chunk += f"  -- grain: {t['grain']}"
    _table_desc = t.get("description_zh") or t.get("description_en")
    if _table_desc:
        chunk += f"  -- {_table_desc}"
        # P0-4: table-description provenance, same rule as column level.
        _tprov = t.get("description_provenance") or description_provenance_tag(
            t.get("description_source")
        )
        if _tprov:
            chunk += f" [desc: {_tprov}]"
    if t.get("row_count"):
        chunk += f"  -- ~{t['row_count']:,} rows"
    # Data-freshness marker (generic): a STALE table has no recent
    # rows — the LLM must not use it for recent-period questions.
    _fresh = stale_flag(t)
    if _fresh:
        chunk += f"  --{_fresh}"
    _cov = t.get("coverage_json") or {}
    if _cov.get("max_date"):
        chunk += (
            f"  -- coverage: latest {_cov.get('date_column') or 'date'} = "
            f"{_cov['max_date']}"
        )
    return chunk


# ── description provenance (P0-4, 2026-10-30 audit D3) ─────────────────────
# Descriptions come from four sources with very different trust: a human
# curator, the connected database's own comments, and the indexer's LLM.
# Until now the LLM saw them all as identical text. These tags make the
# distinction survive into SQL generation — an inferred description must
# NEVER read as verified knowledge.
_DESCRIPTION_PROVENANCE_TAGS = {
    "human": "human-verified",
    "source_comment": "source-comment",
    "llm": "ai-inferred",
}


def description_provenance_tag(source) -> str:
    """Map a ``description_source`` value to its prompt-visible tag."""
    if not source:
        return ""
    return _DESCRIPTION_PROVENANCE_TAGS.get(str(source).lower(), "")


def _format_slice(
    tables: list[dict], join_paths: list[dict], token_budget: int
) -> str:
    """Format tables + joins into a curated DDL-style text slice.

    Budget policy: the detail gives way before the table does. Measured
    annotations roughly double a wide ERP table's chunk (live: 26-column
    ``v_sale_order``), and the old loop ``break``-ed on the first
    over-budget candidate — so adding measurements would have turned a wide
    table's slice into a header with NO DDL at all, leaving the SQL author
    nothing to author against. Now each table is rendered WITH the measured
    detail first, then re-rendered without it, and the first table is included
    even if even the lean form does not fit: the caller asked for these tables
    by name, and a slice with no DDL is strictly worse than an over-budget one.
    """
    parts: list[str] = ["-- Semantic catalog: relevant tables for this query\n"]
    used_tokens = _estimate_tokens(parts[0])
    included = 0

    for t in tables:
        # Degrade the DETAIL, never the table: full -> stats-only -> plain.
        chunk = ""
        for detail in ("full", "stats", "plain"):
            candidate = _render_table_chunk(t, detail=detail)
            if used_tokens + _estimate_tokens(candidate) <= token_budget:
                chunk = candidate
                if detail != "full":
                    logger.debug(
                        "schema_linker: %s included at detail=%s (budget)",
                        t.get("table_name"), detail,
                    )
                break
        if not chunk:
            if included:
                break
            # The FIRST table is never dropped for size: an empty slice gives
            # the SQL author nothing to author against at all.
            chunk = _render_table_chunk(t, detail="plain")
            logger.info(
                "schema_linker: first table %s exceeds the %d-token budget — "
                "included without measured detail",
                t.get("table_name"), token_budget,
            )
        tok = _estimate_tokens(chunk)
        parts.append(chunk)
        used_tokens += tok
        included += 1

    # Joins
    if join_paths and included > 0:
        parts.append("\n-- Known join paths:")
        for jp in join_paths:
            conf = jp.get('confidence', 1.0)
            line = f"-- {jp['from_table']} → {jp['to_table']} ON {jp['on']} ({jp.get('relation_type', 'FK')}, conf={conf:.2f})"
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


# ── full table-of-contents (TOC) ───────────────────────────────────────────

def build_full_toc(db: Session, kb_ids: list[str]) -> list[dict]:
    """Return a lightweight table-of-contents for tables in the catalog.

    Each entry has: table_name, description (zh/en), row_count,
    coverage (date_column, max_date), table_role.
    This is used by the two-phase NL2SQL flow so the LLM can see
    the available tables before choosing which ones to query.

    When the calling agent's domain config opts into a schema-linker
    allowlist, the TOC is RESTRICTED to the curated allowlist — the LLM never
    sees stale shadow tables (e.g. erp_paez_t_lz_price with 2025-06 max
    date) that would otherwise be picked over the real sales views.
    """
    rows = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id.in_(kb_ids))
        .order_by(KBTableMeta.table_name)
        .all()
    )
    allowlist = _resolve_table_allowlist_for_kb(db, kb_ids)
    if allowlist:
        allowed = set(allowlist)
        before = len(rows)
        rows = [r for r in rows if r.table_name in allowed]
        logger.info(
            "schema_linker: TOC allowlist filtered %d -> %d tables",
            before, len(rows),
        )
    result = []
    for r in rows:
        cov = r.coverage_json or {}
        desc = r.description_en or r.description_zh or ""
        result.append({
            "table_name": r.table_name,
            "description": desc,
            # BOTH language variants are carried, not just the display one:
            # retrieval matches a question against the language it was written
            # in, and picking one here is how a Chinese question lost every
            # table whose English description happened to be filled in.
            "description_zh": r.description_zh or "",
            "description_en": r.description_en or "",
            # P0-4: provenance travels with the description so the selector
            # knows whether it is reading verified knowledge or a guess.
            "description_provenance": description_provenance_tag(
                getattr(r, "description_source", None)
            ),
            "row_count": r.row_count,
            "date_column": cov.get("date_column", ""),
            "max_date": cov.get("max_date", ""),
            "min_date": cov.get("min_date", ""),
            "table_role": r.table_role,
            # P-SV: "one row = what" — surfaced in the TOC and selector line
            # so the selection LLM distinguishes a fact table from its
            # pre-aggregated rollup twin.
            "grain": getattr(r, "grain", None),
            "domain_name": getattr(r, "domain_name", None),
            # Measured vocabulary for this table (slice 2) — the values that
            # ACTUALLY occur, which is what lets a question phrased in the
            # customer's own words match the table that holds those words.
            "sample_values": r.sample_values,
            "columns": [],  # filled below for freshness detection
        })

    # Generic freshness: attach lightweight column metadata (name+type) and
    # probe MAX(date) concurrently so STALE tables are flagged even when
    # the catalog's coverage_json is empty. Works for any connected DB.
    try:
        from app.models.knowledge_catalog import KBColumnMeta
        _ids = [r.id for r in rows]
        if _ids:
            # Column MEANING is fetched, not just name+type: the description,
            # semantic type and unit are what let retrieval see that a question
            # about 金额/单价/数量 belongs to the table that has such a column.
            _cols = (
                db.query(
                    KBColumnMeta.column_name,
                    KBColumnMeta.data_type,
                    KBColumnMeta.table_meta_id,
                    KBColumnMeta.semantic_type,
                    KBColumnMeta.unit,
                    KBColumnMeta.description_zh,
                    KBColumnMeta.description_en,
                )
                .filter(KBColumnMeta.table_meta_id.in_(_ids))
                .all()
            )
            _by_tbl: dict[str, list[dict]] = {}
            for _name, _dtype, _tid, _stype, _unit, _dzh, _den in _cols:
                _by_tbl.setdefault(_tid, []).append(
                    {
                        "name": _name,
                        "data_type": _dtype,
                        "semantic_type": _stype or "unknown",
                        "unit": _unit or "",
                        "description": (_dzh or _den or "")[:120],
                    }
                )
            _tbl_by_id = {r.id: r.table_name for r in rows}
            for _tid, _c in _by_tbl.items():
                _t = next((x for x in result if x["table_name"] == _tbl_by_id.get(_tid)), None)
                if _t is not None:
                    _t["columns"] = _c
        from app.services.knowledge_graph.freshness import (
            annotate_tables_parallel, stale_flag,
        )
        for _kb_id in kb_ids:
            annotate_tables_parallel(db, _kb_id, result)
        for _t in result:
            if _t.get("last_data_date"):
                _t["freshness"] = {
                    "last_data_date": _t["last_data_date"],
                    "stale_days": _t.get("stale_days"),
                    "flag": stale_flag(_t),
                }
    except Exception as exc:
        logger.debug("schema_linker: TOC freshness annotation skipped: %s", exc)
    return result


def _toc_line(t: dict, *, compact: bool = False) -> str:
    """Render ONE catalog table as the single line the selector reads.

    Shared by ``format_toc_text`` (the legacy budget-capped renderer) and
    ``format_toc_for_selection`` (the candidate-first renderer) so the two can
    never drift into different shapes — the selection prompt parses these lines
    back into table names, and a shape mismatch there is how a correct catalog
    turns into a hallucinated table name.
    """
    name = t["table_name"]
    desc = t["description"]
    rows_n = t.get("row_count")
    max_d = t.get("max_date", "")
    min_d = t.get("min_date", "")
    role = t.get("table_role", "")
    domain = t.get("domain_name", "")

    parts = [name]
    if domain:
        parts.append(f"domain={domain}")
    if rows_n is not None:
        parts.append(f"~{rows_n:,} rows")
    if desc and not compact:
        parts.append(desc[:60])
        # P0-4: provenance rides with the description on the selector line —
        # an ai-inferred guess must never read like verified knowledge.
        _prov = t.get("description_provenance")
        if _prov:
            parts.append(f"desc:{_prov}")
    if role and role != "unknown" and not compact:
        parts.append(f"role={role}")
    # P-SV: grain on the selector line — a compact but decisive hint that
    # keeps rollup tables from being chosen for detail questions.
    if t.get("grain") and not compact:
        parts.append(f"grain={t['grain'][:50]}")
    # Measured vocabulary. This line is what the table SELECTOR reads, and both
    # blocks of it need the hint: the ranked candidates (so the hint can confirm
    # a retrieval hit) AND the rest of the catalog, where two ERP objects are
    # routinely named alike — erp_t_sale_orderentry vs v_sale_order —
    # and the only thing that separates them is which values actually occur in
    # them. The compact form still keeps its size promise, just tighter: 1
    # column / 3 values / 30 chars, against 3/4/60 for a full line.
    from app.services.knowledge_graph import column_stats
    _cap = (1, 3, 30) if compact else (3, 4, 60)
    _vocab = column_stats.vocabulary_hint(
        t.get("sample_values"), columns=_cap[0], values=_cap[1], width=_cap[2]
    )
    if _vocab:
        parts.append(_vocab)
    if min_d and max_d:
        parts.append(f"coverage: {min_d}~{max_d}")
    elif max_d:
        parts.append(f"latest: {max_d}")
    # Generic freshness flag (live MAX(date) probe, TTL-cached)
    _fresh = t.get("freshness") or {}
    if _fresh.get("flag"):
        parts.append(_fresh["flag"].strip())
    return "  " + ", ".join(parts)


def format_toc_text(toc: list[dict], token_budget: int = 1200) -> str:
    """Render the full TOC as a compact text block for the LLM.

    Each table is 1-2 lines, e.g.:
      sales_detail (50,000 rows, sales detail, coverage: 2023-01~2026-08)

    NOTE: this renderer walks ``toc`` in the caller's order and stops at
    ``token_budget``. ``build_full_toc`` returns tables ordered by name, so with
    a large catalog this keeps the ALPHABETICAL head and drops the business
    tables (measured: 14 of 143 tables, none of them business tables). Prefer
    ``format_toc_for_selection``, which renders the retrieved candidates first
    and the remainder compactly, so a budget can no longer starve the selector.
    """
    lines = ["-- ALL tables in this database (choose the most relevant ones):\n"]
    used = 60  # rough estimate for header line

    for t in toc:
        line = _toc_line(t)
        tok = max(1, len(line) // 2)
        if used + tok > token_budget:
            lines.append(f"  ... and {len(toc) - len(lines) + 1} more tables (not shown due to budget)")
            break
        lines.append(line)
        used += tok

    return "\n".join(lines)


def format_toc_for_selection(
    toc: list[dict],
    candidates: Sequence[str] | None = None,
    *,
    detail_limit: int = 48,
    hard_char_cap: int = 48000,
) -> str:
    """Render the table-selection surface: candidates first, rest compact.

    Two properties this guarantees, both learned from a live failure:

    1. **A character budget can no longer starve the selector.** The retrieved
       candidates are rendered IN FULL, IN RANK ORDER, before anything else, and
       the remainder is rendered compactly (name / domain / rows / coverage —
       no description). Truncation, if the hard cap bites, can now only remove
       the least-relevant tail.
    2. **Truncation is reported truthfully.** The legacy renderer's count was
       computed from ``len(lines)``, which includes the header, so it
       understated by one. Here the count comes from the tables actually
       dropped.

    Order is the caller's (retrieval rank); duplicates between the candidate
    list and the remainder are rendered once, in candidate position.
    """
    seen: set[str] = set()
    ranked: list[dict] = []
    if candidates:
        by_name = {t.get("table_name"): t for t in toc}
        for name in candidates:
            t = by_name.get(name)
            if t is not None and name not in seen:
                ranked.append(t)
                seen.add(name)
    ranked = ranked[:detail_limit]

    detail = [_toc_line(t) for t in ranked]
    rest = [_toc_line(t, compact=True) for t in toc if t.get("table_name") not in seen]

    header = (
        "-- ALL tables in this database (choose the most relevant ones).\n"
        "-- The first block is RANKED BY RETRIEVAL over this catalog and this "
        "question; it is the most likely set. Tables after it are the rest of "
        "the catalog, with less detail.\n"
    )
    out = [header]
    if detail:
        out.append("-- Ranked candidates:")
        out.extend(detail)
    if rest:
        out.append("-- Rest of the catalog:")
        out.extend(rest)

    text = "\n".join(out)
    if len(text) <= hard_char_cap:
        return text

    # Hard cap: keep the header, the ranked candidates, and as much of the
    # remainder as fits — and state the true number dropped.
    kept: list[str] = []
    used = len(header)
    dropped = 0
    for block_line in [*detail, *rest]:
        if used + len(block_line) + 1 > hard_char_cap:
            dropped += 1
            continue
        kept.append(block_line)
        used += len(block_line) + 1
    notice = (
        f"  ... and {dropped} more table(s) dropped by the render cap "
        f"({hard_char_cap} chars)."
    )
    return "\n".join([header, *kept, notice])



def get_selected_tables_ddl(
    db: Session, kb_ids: list[str], selected_table_names: list[str],
    token_budget: int = 2000,
) -> str:
    """Given a list of table names chosen by the LLM, return full DDL
    for those tables (with columns, types, descriptions, join paths).

    This is Phase 2 of the two-phase NL2SQL flow.

    The budget is 2000, not the 800 it was. Measured annotations (slice 2)
    roughly double a wide table's chunk: measured on the live 26-column ERP
    view, ``_estimate_tokens`` gives 1822 with the measurements, 1601 with the
    distribution alone, 535 with neither. At 800 the enforcer stripped the
    measurements off exactly the view the question was about; at 1600 the full
    detail missed by 200 tokens. This is the narrowest prompt of the flow (the
    selector has already picked one or two tables), so it is the right place to
    spend tokens — and the estimate is a conservative 2 chars/token, so the
    real cost is roughly half this. It stays a budget: an unbounded catalog is
    still cut, and a long list still degrades to the distribution or to plain.
    """
    allowlist = _resolve_table_allowlist_for_kb(db, kb_ids)
    if allowlist:
        allowed = set(allowlist)
        before = len(selected_table_names)
        selected_table_names = [n for n in selected_table_names if n in allowed]
        if len(selected_table_names) != before:
            logger.info(
                "schema_linker: selected-tables DDL filtered %d -> %d (allowlist)",
                before, len(selected_table_names),
            )
    metas = (
        db.query(KBTableMeta)
        .filter(
            KBTableMeta.kb_id.in_(kb_ids),
            KBTableMeta.table_name.in_(selected_table_names),
        )
        .all()
    )
    # Build enriched table dicts (same shape as _enrich_tables output)
    from app.models.knowledge_catalog import KBColumnMeta
    enriched = []
    for m in metas:
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id == m.id)
            .order_by(KBColumnMeta.ordinal)
            .all()
        )
        enriched.append({
            "table_meta_id": m.id,
            "table_name": m.table_name,
            "schema_name": m.schema_name,
            "table_type": m.table_type,
            "row_count": m.row_count,
            "description_zh": m.description_zh,
            "description_en": m.description_en,
            # P0-4: provenance feeds the DDL chunk's [desc: ...] markers.
            "description_source": getattr(m, "description_source", None),
            "coverage_json": m.coverage_json or {},
            "domain_name": getattr(m, "domain_name", None),
            "rrf_score": 1.0,
            "columns": [
                {
                    "name": c.column_name,
                    "data_type": c.data_type,
                    "is_nullable": c.is_nullable,
                    "is_primary_key": c.is_primary_key,
                    "description_zh": c.description_zh,
                    "description_en": c.description_en,
                    # P0-4: provenance feeds the [desc: ...] markers.
                    "description_source": getattr(c, "description_source", None),
                    "semantic_type": getattr(c, "semantic_type", None),
                    "enum_values": getattr(c, "enum_values", None),
                    # The measured distribution (slice 2). Rendered into the
                    # DDL slice by _format_slice; absent (None) for a column
                    # that was never measured, which is why the renderer emits
                    # nothing rather than a zero.
                    "stats": getattr(c, "stats_json", None),
                    "unit": getattr(c, "unit", None),
                }
                for c in cols
            ],
        })

    # Get join paths among selected tables
    join_paths = _expand_joins(db, enriched, max_hops=2)

    # Format as DDL
    return _format_slice(enriched, join_paths, token_budget)
