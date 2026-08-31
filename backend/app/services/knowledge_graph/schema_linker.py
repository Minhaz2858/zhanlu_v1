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
                proj_kbs = (
                    db.query(KnowledgeBase.id)
                    .filter(
                        KnowledgeBase.project_id == app.project_id,
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

def _format_slice(
    tables: list[dict], join_paths: list[dict], token_budget: int
) -> str:
    """Format tables + joins into a curated DDL-style text slice."""
    from app.services.knowledge_graph.freshness import stale_flag
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
            "row_count": r.row_count,
            "date_column": cov.get("date_column", ""),
            "max_date": cov.get("max_date", ""),
            "min_date": cov.get("min_date", ""),
            "table_role": r.table_role,
            "columns": [],  # filled below for freshness detection
        })

    # Generic freshness: attach lightweight column metadata (name+type) and
    # probe MAX(date) concurrently so STALE tables are flagged even when
    # the catalog's coverage_json is empty. Works for any connected DB.
    try:
        from app.models.knowledge_catalog import KBColumnMeta
        _ids = [r.id for r in rows]
        if _ids:
            _cols = (
                db.query(KBColumnMeta.column_name, KBColumnMeta.data_type, KBColumnMeta.table_meta_id)
                .filter(KBColumnMeta.table_meta_id.in_(_ids))
                .all()
            )
            _by_tbl: dict[str, list[dict]] = {}
            for _name, _dtype, _tid in _cols:
                _by_tbl.setdefault(_tid, []).append({"name": _name, "data_type": _dtype})
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


def format_toc_text(toc: list[dict], token_budget: int = 1200) -> str:
    """Render the full TOC as a compact text block for the LLM.

    Each table is 1-2 lines, e.g.:
      sales_detail (50,000 rows, sales detail, coverage: 2023-01~2026-08)
    """
    lines = ["-- ALL tables in this database (choose the most relevant ones):\n"]
    used = 60  # rough estimate for header line

    for t in toc:
        name = t["table_name"]
        desc = t["description"]
        rows_n = t.get("row_count")
        max_d = t.get("max_date", "")
        min_d = t.get("min_date", "")
        role = t.get("table_role", "")

        parts = [name]
        if rows_n is not None:
            parts.append(f"~{rows_n:,} rows")
        if desc:
            parts.append(desc[:60])
        if role and role != "unknown":
            parts.append(f"role={role}")
        if min_d and max_d:
            parts.append(f"coverage: {min_d}~{max_d}")
        elif max_d:
            parts.append(f"latest: {max_d}")
        # Generic freshness flag (live MAX(date) probe, TTL-cached)
        _fresh = t.get("freshness") or {}
        if _fresh.get("flag"):
            parts.append(_fresh["flag"].strip())

        line = "  " + ", ".join(parts)
        tok = max(1, len(line) // 2)
        if used + tok > token_budget:
            lines.append(f"  ... and {len(toc) - len(lines) + 1} more tables (not shown due to budget)")
            break
        lines.append(line)
        used += tok

    return "\n".join(lines)


def get_selected_tables_ddl(
    db: Session, kb_ids: list[str], selected_table_names: list[str],
    token_budget: int = 800,
) -> str:
    """Given a list of table names chosen by the LLM, return full DDL
    for those tables (with columns, types, descriptions, join paths).

    This is Phase 2 of the two-phase NL2SQL flow.
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
            "coverage_json": m.coverage_json or {},
            "rrf_score": 1.0,
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

    # Get join paths among selected tables
    join_paths = _expand_joins(db, enriched, max_hops=2)

    # Format as DDL
    return _format_slice(enriched, join_paths, token_budget)
