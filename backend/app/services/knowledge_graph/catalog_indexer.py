"""Catalog Indexer — introspect + describe + persist + embed per-KB catalog.

Called when a KnowledgeBase is first created or re-indexed.  Reads the
connector (table list, schema, FKs, row counts), generates business
descriptions via a cheap LLM, persists to kb_table_meta / kb_column_meta /
kb_table_relation, and embeds into a ChromaDB collection catalog_{kb_id}.

Design notes:
- Sync-only (called via asyncio.to_thread from triggers).
- Idempotent: UPSERT + delete-before-recreate Chroma collection.
- Error-safe: any exception → catalog_status="error", never crashes caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta, KBTableRelation
from app.services.db.connector_factory import get_connector
from app.services.knowledge_graph import column_stats
from app.services.knowledge_graph.join_edge_detector import detect_join_edges, type_bucket

logger = logging.getLogger(__name__)
# Ensure INFO logs are visible even when the root logger is at WARNING.
if logger.level == logging.NOTSET or logger.level > logging.INFO:
    logger.setLevel(logging.INFO)

# ── limits ────────────────────────────────────────────────────────────────
MAX_TABLES = 300          # safety cap — real warehouse has ~139
ROW_COUNT_TIMEOUT_S = 5   # skip row counts if the DB is slow
LLM_BATCH_SIZE = 8        # tables per description call
INTROSPECT_MAX_WORKERS = 8  # parallel table-introspection threads
LLM_MAX_CONCURRENT = 4     # parallel LLM description calls
LLM_TEMP = 0.1            # low temp for structured output

# ── LLM batch prompt ──────────────────────────────────────────────────────

_TABLE_DESCRIPTION_SYSTEM = (
    "你是一个数据分析专家。下面是数据库中一批表的结构信息。"
    "请为每张表和每个字段输出中文和英文的业务含义说明。"
    "说明应该简洁、可被其他大模型理解，用于后续的自然语言查询 (NL2SQL) 场景。"
    "如果字段含义无法从名称判断，标注为 '未知'。"
    "对数值型字段，如果能从字段名或样本值判断计量单位（如 元、吨、%、USD、件），"
    "在 unit 字段填入单位；无法判断时留空字符串。"
    "输出严格的 JSON 对象，顶层必须是一个 `tables` 数组，每张表一个对象。"
    '格式: {"tables": [{"table_name": ..., "description_zh": ..., "description_en": ..., "columns": [...]}]}'
)

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string"},
                    "description_zh": {"type": "string"},
                    "description_en": {"type": "string"},
                    "columns": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "column_name": {"type": "string"},
                                "description_zh": {"type": "string"},
                                "description_en": {"type": "string"},
                                "unit": {"type": "string"},
                            },
                            "required": ["column_name", "description_zh", "description_en"],
                        },
                    },
                },
                "required": ["table_name", "description_zh", "description_en", "columns"],
            },
        }
    },
    "required": ["tables"],
}


# ── public API ─────────────────────────────────────────────────────────────


def score_description_confidence(
    description_zh: str | None,
    description_en: str | None,
    description_source: str | None,
    *,
    unit: str | None = None,
    enum_values: list | None = None,
) -> float:
    """How much the NL2SQL pipeline should trust a description (0.0-1.0).

    The onboarding LLM writes "未知" for columns it cannot name; until now
    those guesses were indistinguishable from solid knowledge. Scale:
      0.0  blank / never described
      0.3  the LLM's own unknown-marker (a flagged defect, not knowledge)
      0.6  plain LLM description
      0.8  LLM description corroborated by DATA (a unit read off samples,
           or a small enum dictionary)
      0.9  the customer's OWN source comment (verbatim, not inference)
      1.0  reserved for human-verified edits (catalog PATCH endpoints)
    """
    zh = (description_zh or "").strip()
    en = (description_en or "").strip()
    if not zh and not en:
        return 0.0
    lowered = (zh + " " + en).lower()
    if "未知" in zh or "unknown" in lowered:
        return 0.3
    if (description_source or "") == "source_comment":
        return 0.9
    score = 0.6
    if (unit and str(unit).strip()) or (enum_values and len(enum_values) > 0):
        score = 0.8
    return round(score, 2)


async def index_kb_catalog(kb: KnowledgeBase, db: Session) -> None:
    """Main entry point — idempotent catalog index for one KnowledgeBase.

    Async: we call the LLM for descriptions (async HTTP).
    Introspection (connector) runs via asyncio.to_thread.
    Caller is responsible for committing ``kb.catalog_status`` changes.
    """
    import asyncio

    from app.services.db import readiness as src_readiness

    t0 = time.monotonic()
    # Captured BEFORE any DB work: after a failed flush the ORM object is
    # expired/poisoned, so touching ``kb.id`` from the error path raised
    # PendingRollbackError and HID the original exception (cost a full
    # diagnosis cycle when a duplicate domain name aborted an index).
    kb_id = kb.id
    try:
        _set_status(db, kb, "indexing")
        # Lifecycle: CONNECTED → DISCOVERING. Staged states are what the
        # readiness gate reads on every answering path (services/db/readiness.py),
        # so they must be written as the work actually happens — not inferred
        # afterwards from whether the job happened to finish.
        src_readiness.set_state(db, kb, src_readiness.STATE_DISCOVERING)

        # Out-param: a table we could not read means the structure is only
        # partly known, and the source must not claim to be fully ready.
        unreadable: list[str] = []
        index_stats: dict = {}
        tables = await asyncio.to_thread(
            _introspect_tables_sync, kb, db, unreadable, index_stats
        )
        logger.info("catalog_indexer: kb=%s discovered %d tables", kb.id, len(tables))
        if unreadable:
            logger.warning(
                "catalog_indexer: kb=%s discovery incomplete — %d unreadable table(s)",
                kb.id, len(unreadable),
            )
        if index_stats.get("truncated"):
            logger.warning(
                "catalog_indexer: kb=%s object set truncated — %d of %d objects "
                "catalogued (cap=%d)",
                kb.id, len(tables), index_stats.get("objects_at_source"),
                MAX_TABLES,
            )

        if tables:
            src_readiness.set_state(
                db, kb, src_readiness.STATE_INDEXED,
                reason=f"{len(tables)} tables, {sum(len(t.get('columns') or []) for t in tables)} columns discovered",
            )
            if settings.TABLE_ROLE_AUTO_CLASSIFY_ENABLED:
                await asyncio.to_thread(_classify_table_roles, tables)
            else:
                for t in tables:
                    t["table_role"] = "unknown"
            await _generate_descriptions(tables)
            await asyncio.to_thread(_persist_catalog, db, kb.id, tables)
            await asyncio.to_thread(_persist_relations, db, kb.id, tables)
            if settings.CATALOG_JOIN_EDGES_ENABLED:
                await asyncio.to_thread(_persist_join_edges, db, kb.id, tables)
            # Multi-tenant stamping (audit fix): every derived row carries the
            # owning KB's org_id, so the semantic stores can enforce org
            # filters without hiding data. One idempotent alignment pass per
            # index (covers re-indexed rows created before org discipline).
            await asyncio.to_thread(_align_org, db, kb.id, getattr(kb, "org_id", None))
            await _bootstrap_metrics(db, kb, tables)
            # Lifecycle: ENRICHED — structure + descriptions + relations persisted.
            src_readiness.set_state(
                db, kb, src_readiness.STATE_ENRICHED,
                reason=_enrichment_gap(tables, embedded=False),
            )
        await asyncio.to_thread(_embed_catalog, kb.id, tables)

        # Business domains need the persisted rows (FK graph) and the fresh
        # embeddings, so they run last. Flag-gated; failure is non-fatal.
        if getattr(settings, "KG_DOMAIN_DISCOVERY_ENABLED", False):
            try:
                from app.services.knowledge_graph.domain_discovery import (
                    discover_domains,
                )

                domains = (
                    db.query(KBTableMeta)
                    .filter(
                        KBTableMeta.kb_id == kb.id,
                        KBTableMeta.is_deleted.is_(False),
                    )
                    .all()
                )
                result = await discover_domains(db, kb.id, domains)
                logger.info(
                    "catalog_indexer: kb=%s domain discovery → %s", kb.id, result
                )
            except Exception:
                logger.exception(
                    "catalog_indexer: domain discovery failed (non-fatal) kb=%s", kb.id
                )

        # ── VALIDATED: discover this source's capability profile from the ──
        # source itself (dialect quoting, pagination, date functions, null
        # behaviour, views/window support, timezone, locale, limits, read-only
        # account). Recorded per source; never a default dialect.
        caps = None
        caps_error = ""
        try:
            from app.services.db import capability_probe

            caps = await asyncio.to_thread(
                capability_probe.detect_and_store, db, kb, tables=tables
            )
            if caps is None:
                caps_error = "capability probe disabled"
            elif getattr(caps, "detection_error", None):
                caps_error = str(caps.detection_error)
        except Exception as exc:  # noqa: BLE001 — never fail the index for this
            caps_error = str(exc)
            logger.exception("catalog_indexer: kb=%s capability probe failed", kb.id)

        verdict = _classify_completion(
            tables,
            unreadable=unreadable,
            caps_error=caps_error,
            truncated=int(index_stats.get("truncated") or 0),
        )
        final_state, final_reason = verdict

        _set_status(db, kb, "ready" if final_state != src_readiness.STATE_DISCOVERY_FAILED else "error")
        src_readiness.set_state(db, kb, final_state, reason=final_reason)
        kb.item_count = len(tables)
        db.commit()
        _sync_registry(db, kb, len(tables))
        elapsed = time.monotonic() - t0
        logger.info(
            "catalog_indexer: kb=%s done — %d tables (item_count=%d) state=%s in %.1fs",
            kb.id, len(tables), kb.item_count, final_state, elapsed,
        )
    except Exception:
        logger.exception("catalog_indexer: kb=%s failed", kb_id)
        # Recover the session first — a poisoned transaction would make the
        # status write below raise and leave the KB stuck on "indexing".
        try:
            db.rollback()
        except Exception:
            logger.exception("catalog_indexer: kb=%s rollback failed", kb_id)
        try:
            (
                db.query(KnowledgeBase)
                .filter(KnowledgeBase.id == kb_id)
                .update({"catalog_status": "error"}, synchronize_session=False)
            )
            db.commit()
        except Exception:
            logger.exception("catalog_indexer: kb=%s could not set error status", kb_id)
        # Lifecycle: the source failed discovery/validation and must not be
        # queried. The readiness gate reads this and refuses with a
        # business-language disclosure instead of answering from a half-built
        # catalog.
        try:
            src_readiness.set_state_by_id(
                db, kb_id, src_readiness.STATE_DISCOVERY_FAILED,
                reason="index job raised — structure could not be established",
            )
        except Exception:
            logger.exception("catalog_indexer: kb=%s could not set failed state", kb_id)


def _sync_registry(db: Session, kb: KnowledgeBase, table_count: int) -> None:
    """Upsert the KB into the Unified Resource Registry for its bound projects.

    Flag-gated (KG_RESOURCE_REGISTRY_ENABLED); best-effort — never fails
    the catalog index itself.
    """
    if not getattr(settings, "KG_RESOURCE_REGISTRY_ENABLED", False):
        return
    try:
        from app.services.knowledge_graph.registry_indexer import index_knowledge_base

        project_ids: set[str] = set()
        if getattr(kb, "project_id", None):
            project_ids.add(kb.project_id)
        legacy_name = getattr(kb, "project", None)
        if legacy_name:
            from app.models.project import Project

            for p in (
                db.query(Project)
                .filter(Project.name == legacy_name, Project.is_deleted == False)  # noqa: E712
                .all()
            ):
                project_ids.add(p.id)
        for pid in project_ids:
            if pid:
                index_knowledge_base(db, kb, project_id=pid, table_count=table_count)
        db.commit()
    except Exception:
        logger.debug("catalog_indexer: registry sync failed (non-fatal)", exc_info=True)


async def _bootstrap_metrics(
    db: Session, kb: KnowledgeBase, tables: list[dict]
) -> None:
    """LLM-propose project metrics for each project bound to this KB.

    Flag-gated (KG_METRIC_BOOTSTRAP_ENABLED) inside the bootstrap module;
    best-effort — never fails the catalog index itself.
    """
    if not getattr(settings, "KG_METRIC_BOOTSTRAP_ENABLED", False):
        return
    try:
        from app.services.knowledge_graph.metric_bootstrap import (
            bootstrap_project_metrics,
        )

        project_ids: set[str] = set()
        if getattr(kb, "project_id", None):
            project_ids.add(kb.project_id)
        legacy_name = getattr(kb, "project", None)
        if legacy_name:
            from app.models.project import Project

            for p in (
                db.query(Project)
                .filter(Project.name == legacy_name, Project.is_deleted == False)  # noqa: E712
                .all()
            ):
                project_ids.add(p.id)
        for pid in project_ids:
            if pid:
                await bootstrap_project_metrics(db, pid, kb.id, tables)
    except Exception:
        logger.exception("catalog_indexer: metric bootstrap failed (non-fatal)")


# ── internals ──────────────────────────────────────────────────────────────

def _align_org(db: Session, kb_id: str, org_id: str | None) -> None:
    """Stamp the owning org onto every catalog row derived from this KB.

    Dialect-safe ORM updates (no UPDATE..FROM); idempotent per index run.
    """
    if not org_id:
        return
    try:
        meta_ids = [
            mid for (mid,) in (
                db.query(KBTableMeta.id)
                .filter(KBTableMeta.kb_id == kb_id)
                .all()
            )
        ]
        db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb_id).update(
            {"org_id": org_id}, synchronize_session=False
        )
        if meta_ids:
            db.query(KBColumnMeta).filter(
                KBColumnMeta.table_meta_id.in_(meta_ids)
            ).update({"org_id": org_id}, synchronize_session=False)
        db.query(KBTableRelation).filter(KBTableRelation.kb_id == kb_id).update(
            {"org_id": org_id}, synchronize_session=False
        )
        db.commit()
    except Exception:
        logger.exception("catalog_indexer: org alignment failed kb=%s", kb_id)
        try:
            db.rollback()
        except Exception:
            pass


def _set_status(db: Session, kb: KnowledgeBase, status: str) -> None:
    kb.catalog_status = status
    db.commit()


# ── completion classification (lifecycle: ENRICHED → VALIDATED → READY) ─────
# How much of the catalog must be described before a source may claim to be
# fully ready. A source below this ratio still answers, but as
# ``enrichment_partial`` — i.e. with a disclosure — never silently.
_DESCRIPTION_FLOOR = 0.8


def _description_coverage(tables: list[dict]) -> tuple[int, int, int]:
    """(described_tables, total_tables, described_columns) for a discovery run."""
    total = len(tables)
    described = 0
    columns_described = 0
    for t in tables:
        if not _is_blank_description(t.get("description_zh")) or not _is_blank_description(
            t.get("description_en")
        ):
            described += 1
        for col in t.get("columns") or []:
            if not _is_blank_description(col.get("description_zh")) or not _is_blank_description(
                col.get("description_en")
            ):
                columns_described += 1
    return described, total, columns_described


def _enrichment_gap(tables: list[dict], *, embedded: bool) -> str:
    """Human/operator-facing reason string for how complete enrichment is."""
    described, total, cols = _description_coverage(tables)
    bits = [f"{described}/{total} tables described", f"{cols} columns described"]
    if not embedded:
        bits.append("embeddings pending")
    return "; ".join(bits)


def _classify_completion(
    tables: list[dict],
    *,
    unreadable: list[str] | None = None,
    caps_error: str = "",
    truncated: int = 0,
) -> tuple[str, str]:
    """Decide the terminal lifecycle state for a finished index run.

    Returns ``(state, reason)`` from the vocabulary in ``db/readiness.py``:

    * ``validation_failed``    — nothing usable was discovered, or the source's
      dialect could not be identified (no capability profile exists for it, so
      no SQL can be authored or executed for it).
    * ``enrichment_partial``   — structure is known, but descriptions are
      missing for a meaningful share of it, some tables were unreadable, or the
      source holds more objects than ``MAX_TABLES`` so the catalog is a subset.
      The source answers, and every answer states the limitation.
    * ``nl2sql_ready``         — discovered, described and capability-profiled,
      with the whole object set catalogued.

    Never returns a "ready" verdict on the strength of the job merely finishing.
    """
    from app.services.db import readiness as src_readiness

    if not tables:
        return (
            src_readiness.STATE_VALIDATION_FAILED,
            "no table or view could be read from this source",
        )
    if caps_error and "no dialect capability profile" in caps_error.lower():
        return (src_readiness.STATE_VALIDATION_FAILED, caps_error)

    described, total, cols = _description_coverage(tables)
    ratio = (described / total) if total else 0.0
    gaps: list[str] = []
    if unreadable:
        gaps.append(f"{len(unreadable)} of {total + len(unreadable)} tables could not be read")
    if truncated > 0:
        # Honesty about coverage, not a cosmetic label: the agent must not
        # present a capped object list as the entire database.
        gaps.append(
            f"only {total} of {total + truncated} objects were catalogued "
            f"(object cap), the rest are absent from the catalog"
        )
    if ratio < _DESCRIPTION_FLOOR:
        gaps.append(f"only {described}/{total} tables have descriptions")
    if caps_error:
        gaps.append(f"capability profile incomplete: {caps_error}")

    if gaps:
        return (
            src_readiness.STATE_ENRICHMENT_PARTIAL,
            f"{len(tables)} tables, {cols} columns; " + "; ".join(gaps),
        )
    return (
        src_readiness.STATE_READY,
        f"{len(tables)} tables, {cols} columns, capability profile recorded",
    )


def _introspect_tables_sync(
    kb: KnowledgeBase,
    db: Session,
    failures: list[str] | None = None,
    stats: dict | None = None,
) -> list[dict]:
    """Sync wrapper: introspect full schema via connector (runs in thread).

    Uses a ThreadPoolExecutor to introspect tables in parallel — each worker
    gets its own connector (via context manager) so SQLAlchemy connections
    are never shared across threads.

    ``failures`` is an optional out-param: names of tables that were listed by
    the source but could not be introspected. A non-empty list means the
    structure is only partly known, which the lifecycle records as
    ``enrichment_partial`` rather than claiming a complete catalog.

    ``stats`` is an optional out-param that reports ``objects_at_source`` and
    ``truncated`` when ``MAX_TABLES`` cut the object list short. Truncation used
    to be a log line only, so a source with more objects than the cap could
    still be reported as fully prepared — and the agent then presented a partial
    catalog as the whole database.
    """
    with get_connector(kb) as conn:
        table_names = _safe_list_tables(conn)
    if len(table_names) > MAX_TABLES:
        logger.warning(
            "catalog_indexer: kb=%s has %d tables, capping at %d",
            kb.id, len(table_names), MAX_TABLES,
        )
        if stats is not None:
            stats["objects_at_source"] = len(table_names)
            stats["truncated"] = len(table_names) - MAX_TABLES
        table_names = table_names[:MAX_TABLES]

    # Per-table worker — each opens its own connector.  Capped at
    # INTROSPECT_MAX_WORKERS (default 8) to avoid overwhelming MySQL with
    # parallel connections.
    tables: list[dict] = []
    with ThreadPoolExecutor(max_workers=INTROSPECT_MAX_WORKERS) as ex:
        futures = {
            ex.submit(_introspect_one_table, kb, name): name
            for name in table_names
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                result = fut.result()
            except Exception:
                logger.exception(
                    "catalog_indexer: kb=%s failed to introspect table '%s' — skipping",
                    kb.id, name,
                )
                if failures is not None:
                    failures.append(name)
                continue
            if result is None and failures is not None:
                failures.append(name)
            if result is not None:
                tables.append(result)
    return tables


def _introspect_one_table(kb: KnowledgeBase, table_name: str) -> dict | None:
    """Introspect a single table in a worker thread.

    Each worker creates its own connector (via context manager) so that
    SQLAlchemy connections are never shared across threads.  Returns None
    on failure (logged by caller).

    Collected per object (2026-09-20): columns (+ the source's own comments and
    index roles), declared foreign keys, an exact row count *or* the engine's
    estimate with its provenance, the source's table comment / engine /
    allocation size, and the index inventory. Everything optional degrades to
    "not collected" — a missing fact is never written as zero or as a guess.
    """
    try:
        with get_connector(kb) as conn:
            desc = conn.describe_table(table_name)
            exact_count = _safe_row_count(conn, table_name)
            fks = _safe_foreign_keys(conn, table_name)
            facts = _safe_table_facts(conn, table_name)
            indexes = _safe_indexes(conn, table_name)
            est_rows = facts.get("est_rows")
            schema_name, table_type, columns = _normalize_desc(desc)
            # Sampling stays gated on an EXACT count: it is the only signal that
            # the table really is non-empty (an estimate can be stale).
            if exact_count and columns:
                _sample_column_values(conn, table_name, columns)
                if settings.CATALOG_JOIN_EDGES_ENABLED:
                    _sample_joinable_values(conn, table_name, columns)
            # ── per-column statistics (2026-09-20, slice 2) — ONE bounded row
            # sample per object. Gated on an exact count OR an engine estimate:
            # unlike the per-column DISTINCT probes above this costs a single
            # statement, and a large fact table (where COUNT(*) times out and
            # only the estimate survives) is exactly where a null rate and a
            # value range matter most. Nothing is recorded when the object
            # reads back empty — the stats pass returns {} and the column keeps
            # no entry, rather than a zero.
            stats: dict = {}
            if (exact_count or est_rows) and columns:
                stats = column_stats.profile_table_columns(conn, table_name, columns)
                for c in columns:
                    name = str(c.get("column_name") or "")
                    if name and name in stats:
                        c["stats"] = stats[name]
            coverage = None
            if settings.KG_COVERAGE_PROBE_ENABLED and columns:
                coverage = _probe_coverage(conn, table_name, columns)

        # ``est_rows`` was read inside the ``with`` block (the stats gate needs
        # it before the connector closes).
        if exact_count is not None:
            row_count, row_count_source = exact_count, "exact"
        elif est_rows is not None:
            row_count, row_count_source = int(est_rows), "estimated"
        else:
            row_count, row_count_source = None, None

        return {
            "schema_name": schema_name,
            "table_name": table_name,
            "table_type": facts.get("table_type") or table_type,
            "row_count": row_count,
            "row_count_source": row_count_source,
            "est_rows": est_rows,
            "size_bytes": facts.get("size_bytes"),
            "engine": facts.get("engine"),
            "table_comment": facts.get("comment"),
            "indexes": indexes,
            "columns": columns,
            "foreign_keys": fks,
            "coverage_json": coverage,
        }
    except Exception:
        logger.exception(
            "catalog_indexer: kb=%s failed to introspect table '%s'",
            kb.id, table_name,
        )
        return None


# ── table role classification (Entity Master Filter) ───────────────────────

# Column-name signals for role classification.  Purely structural — these are
# generic patterns that hold across any business schema (ERP, SaaS,
# manufacturing, healthcare …).  Zero domain keywords, zero hardcoded table
# names.
_ROLE_NAME_RE = re.compile(r"(^|_)(name|fname|title|label)(_|$)", re.IGNORECASE)
_ROLE_CATEGORY_RE = re.compile(
    r"(type|category|class|status|grade|group|kind|segment|variety)",
    re.IGNORECASE,
)
_ROLE_DATE_RE = re.compile(
    r"(date|time|dt$|_at$|created|updated|period|entry|shipment|statistic)",
    re.IGNORECASE,
)
_ROLE_MEASURE_RE = re.compile(
    r"(qty|quantity|amount|price|cost|revenue|sales|volume|value|fee|total|"
    r"balance|weight|count)",
    re.IGNORECASE,
)
_ROLE_ID_RE = re.compile(r"(^|_)(id|code|no|number)(_|$)", re.IGNORECASE)

# Suffix-anchored complements.  The boundary-anchored patterns above only match
# snake_case (e.g. ``product_id``, ``customer_name``).  Compound names where the
# keyword is embedded as a suffix WITHOUT a leading separator — Kingdee F-prefix
# (``FCUSTID``), SAP B1 (``BKCU_ID``), PascalCase (``OrderId``), CamelCase
# (``materialID``) or bare concatenations (``CUSTID``, ``PRODUCTNAME``) — need a
# suffix anchor.  Purely structural: no vendor knowledge, works on any database.
_ROLE_ID_SUFFIX_RE = re.compile(r"(id|code|no|number)$", re.IGNORECASE)
_ROLE_NAME_SUFFIX_RE = re.compile(r"(name|title|label)$", re.IGNORECASE)


def _classify_table_roles(tables: list[dict]) -> None:
    """Mutate each table dict with a structural ``table_role`` classification
    and a ``grain`` guess ("one row = what").

    Called during catalog indexing when ``TABLE_ROLE_AUTO_CLASSIFY_ENABLED``.
    Uses the full table set so FK in-degree (how many other tables point TO a
    given table) is available — a strong signal that the target is a master.
    Best-effort: imperfect classifications are refined at runtime by the LLM
    and overridden per-project via ProjectCatalogOverlay.
    """
    name_to_idx = {t["table_name"]: i for i, t in enumerate(tables)}
    indegree: dict[int, int] = {}
    for t in tables:
        for fk in t.get("foreign_keys", []):
            ref = fk.get("ref_table", "")
            if ref in name_to_idx:
                j = name_to_idx[ref]
                indegree[j] = indegree.get(j, 0) + 1

    for i, t in enumerate(tables):
        t["table_role"] = _classify_table_role(t, indegree.get(i, 0))
        t["grain"] = _infer_grain(t)


# Name hints for the extended role taxonomy (P-SV). Checked BEFORE the
# structural heuristics — a table NAMED sales_summary is an aggregate even
# if it looks structurally like a fact.
_AGGREGATE_RE = re.compile(
    r"(summary|agg|aggregate|_by_day|_by_month|_daily|_monthly|_rollup)", re.I)
_REPORT_VIEW_RE = re.compile(r"(^vw_|_view$|^v_|_report$)", re.I)
_FORECAST_RE = re.compile(r"(forecast|prediction|_pred$|projected)", re.I)
_ARCHIVE_RE = re.compile(r"(archive|backup|_bak$|_hist(ory)?$|_old$)", re.I)


def _infer_grain(table: dict, role: str | None = None) -> str | None:
    """Guess "one row = what" for the Source Verification Gate.

    Heuristics, most specific first:
    - explicit business-key columns (order_no, invoice_no…) → one row per that
    - fact with N FK targets → one row per <target> × <target> (× date)
    - single-PK entity → one row per <pk>
    - date + measures → one row per <date col>
    Returns a human-readable phrase or None (unknown grain is honest).
    """
    role = role or table.get("table_role") or ""
    cols = table.get("columns", [])
    col_names = [c.get("column_name", "") for c in cols]

    biz_keys = [cn for cn in col_names
                if re.search(r"(no$|_no$|number$|_code$|_key$)", cn, re.I)
                and not re.search(r"(type|status|flag)", cn, re.I)]
    if biz_keys:
        return f"one row per {biz_keys[0].lower()}"

    fks = [fk.get("ref_table", "") for fk in table.get("foreign_keys", [])
           if fk.get("ref_table")]
    if role == "fact" and fks:
        parts = [f"one {f}" for f in fks[:2]]
        has_date = any(
            _ROLE_DATE_RE.search(cn) or _is_date_type(c.get("data_type", ""))
            for cn, c in ((c.get("column_name", ""), c) for c in cols))
        if has_date:
            parts.append("one date")
        return "one row per " + " × ".join(parts)

    pks = [c.get("column_name", "") for c in cols if c.get("is_primary_key")]
    if len(pks) == 1:
        return f"one row per {pks[0].lower()}"

    if role == "aggregate":
        has_date = any(
            _ROLE_DATE_RE.search(cn) or _is_date_type(c.get("data_type", ""))
            for cn, c in ((c.get("column_name", ""), c) for c in cols))
        group_cols = [cn for cn in col_names
                      if _ROLE_ID_RE.search(cn) or _ROLE_NAME_RE.search(cn)]
        what = " × ".join(group_cols[:2]) if group_cols else "pre-aggregated period"
        return f"one row per {what} (pre-aggregated)" + (" + date" if has_date else "")
    return None





def _classify_table_role(table: dict, fk_indegree: int) -> str:
    """Classify a single table's role via structural heuristics.

    Returns: ``entity_master | fact | dimension | bridge | aggregate |
    reporting_view | forecast | archive | unknown``.
    Priority order:
      0. name hints  — a table NAMED sales_summary / vw_xxx / xxx_forecast /
         xxx_archive is what its name says, even if it *looks* structurally
         like a fact. Aggregate/report/forecast/archive objects are exactly
         the ones the Source Verification Gate must steer the LLM away from
         for raw business measures, so the name signal outranks structure.
      1. entity_master — small row count, id+name columns, a category/type
         column OR referenced by FKs from other tables.
      2. fact        — large row count, a temporal column + measure columns.
      3. bridge      — 2+ FK columns, no measure columns (junction table).
      4. dimension   — moderate row count with id+name columns.
      5. unknown     — fallback.
    """
    # 0) Name hints (checked BEFORE structure — see _AGGREGATE_RE etc.).
    name = str(table.get("table_name") or "")
    if _AGGREGATE_RE.search(name):
        return "aggregate"
    if _FORECAST_RE.search(name):
        return "forecast"
    if _ARCHIVE_RE.search(name):
        return "archive"
    if _REPORT_VIEW_RE.search(name):
        return "reporting_view"

    row_count = table.get("row_count") or 0
    cols = table.get("columns", [])
    col_names = [c.get("column_name", "") for c in cols]
    col_types = [c.get("data_type", "") for c in cols]

    has_id = any(
        _ROLE_ID_RE.search(cn) or _ROLE_ID_SUFFIX_RE.search(cn) for cn in col_names
    )
    has_name = any(
        _ROLE_NAME_RE.search(cn) or _ROLE_NAME_SUFFIX_RE.search(cn) for cn in col_names
    )
    has_category = any(_ROLE_CATEGORY_RE.search(cn) for cn in col_names)
    has_date = any(
        _ROLE_DATE_RE.search(cn) or _is_date_type(ct)
        for cn, ct in zip(col_names, col_types)
    )
    has_measure = any(_ROLE_MEASURE_RE.search(cn) for cn in col_names)
    fk_col_count = sum(
        1 for cn in col_names if _ROLE_ID_RE.search(cn) or _ROLE_ID_SUFFIX_RE.search(cn)
    )

    # 1) entity_master — small, name-bearing, category'd or FK-referenced.
    if (
        row_count and row_count < settings.ENTITY_MASTER_MAX_ROW_COUNT
        and has_id and has_name
        and (has_category or fk_indegree > 0)
    ):
        return "entity_master"

    # 2) fact — large, temporal, measurable.
    if row_count and row_count > 10000 and has_date and has_measure:
        return "fact"

    # 3) bridge — multi-FK junction without measures.
    if fk_col_count >= 2 and not has_measure:
        return "bridge"

    # 4) dimension — moderate size with id+name.
    if has_id and has_name and (not row_count or row_count < 100000):
        return "dimension"

    return "unknown"


def _normalize_desc(desc: Any) -> tuple[str, str, list[dict]]:
    """Normalize `conn.describe_table()` output into catalog column rows.

    Real connectors (mysql/postgres/mssql) return a plain list of column
    dicts keyed `name/type/nullable/default/pk`. Some code paths/mocks may
    return a dict shaped `{schema, type, columns:[...]}`. Normalize both
    into `(schema_name, table_type, columns)` where each column uses the
    catalog's canonical keys.
    """
    if isinstance(desc, dict):
        schema_name = str(desc.get("schema", "") or "")
        table_type = str(desc.get("type", "TABLE") or "TABLE")
        raw_cols = desc.get("columns", []) or []
    else:
        schema_name = ""
        table_type = "TABLE"
        raw_cols = desc or []

    columns: list[dict] = []
    for i, c in enumerate(raw_cols, start=1):
        if not isinstance(c, dict):
            continue
        raw_comment = c.get("comment")
        raw_key = c.get("key")
        columns.append({
            "column_name": c.get("name") or c.get("column_name") or "",
            "ordinal": c.get("ordinal", i),
            "data_type": c.get("type") or c.get("data_type") or "",
            "is_nullable": bool(c.get("nullable", c.get("is_nullable", True))),
            "is_primary_key": bool(c.get("pk", c.get("primary_key", False))),
            "default_value": c.get("default", c.get("default_value")),
            # The source's own words + index role. None / "" mean "the source
            # did not supply one" — never replaced by our inference here.
            "comment": raw_comment.strip() if isinstance(raw_comment, str) and raw_comment.strip() else None,
            "key": raw_key.strip().upper() if isinstance(raw_key, str) else "",
        })
    return schema_name, table_type, columns


def _safe_list_tables(conn: Any) -> list[str]:
    """List tables, propagating connection-level failures.

    A failure here means the KB's DB connection itself is broken (e.g.
    unreachable host). Returning ``[]`` would silently mark the KB as
    "ready" with zero tables — misleading. Per-table introspection failures
    are still tolerated in ``_introspect_tables_sync``.
    """
    try:
        return conn.list_tables()
    except Exception:
        logger.exception("catalog_indexer: list_tables failed for kb")
        raise


def _safe_row_count(conn: Any, table: str) -> int | None:
    try:
        rows = conn.execute(
            f"SELECT COUNT(*) FROM {_maybe_quote(conn, table)}",
            timeout_s=ROW_COUNT_TIMEOUT_S,
        )
        if rows:
            # connectors return list[dict] — take the first column value
            return int(list(rows[0].values())[0])
    except Exception:
        pass
    return None


# Name-like string columns whose distinct values carry the business
# vocabulary users actually ask with (generic terms only).
_SAMPLEABLE_RE = re.compile(
    r"(name|product|region|area|spec|model|category|brand|grade|"
    r"supplier|customer|variety|item|material|type)",
    re.IGNORECASE,
)
_MAX_SAMPLE_COLS = 12
_SAMPLE_LIMIT = 40
_SAMPLE_VALUE_LEN = 20

# A column whose sampled distinct values stop short of the query limit is
# bounded — those values are a usable dictionary. Above it we know only that
# the column is high-cardinality (free text, names, codes).
_ENUM_MAX_VALUES = 25

# Generic column-role patterns. These describe SHAPE, never a customer's
# business vocabulary: an identifier is an identifier in any industry.
_ID_HINT_RE = re.compile(
    r"(^|_)(id|ids|code|no|num|number|key|uuid|guid|sk|pk)($|_)", re.IGNORECASE
)
_FLAG_HINT_RE = re.compile(
    r"(^|_)(is|has|flag|yn|enable|enabled|active|deleted|valid|status_flag)($|_)",
    re.IGNORECASE,
)


def classify_column_semantics(column: dict, table_role: str = "unknown") -> tuple[str, list | None]:
    """Return ``(semantic_type, enum_values)`` for one introspected column.

    ``semantic_type`` is one of ``identifier`` / ``temporal`` / ``flag`` /
    ``measure`` / ``dimension`` / ``free_text`` / ``unknown`` — derived from the
    column's declared type, key/role metadata and its *sampled* values, so it
    works for any dialect and any industry without a vocabulary list.

    ``enum_values`` is populated only when the sampled distinct values are few
    and provably complete: either the DISTINCT probe stopped before its limit,
    or the row sample read the whole object and the column has no more distinct
    values than we kept (``column_stats`` — slice 2).
    """
    name = str(column.get("column_name") or "")
    dtype = str(column.get("data_type") or "").lower()
    samples = column.get("value_samples") or []
    stats: dict = {}
    _stats = column.get("stats")
    if isinstance(_stats, dict):
        stats = _stats

    enum_values: list | None = None
    if (
        isinstance(samples, list)
        and 1 < len(samples) < _SAMPLE_LIMIT
        and len(samples) <= _ENUM_MAX_VALUES
    ):
        # Distinct (the SQL used DISTINCT), bounded, and human-sized.
        enum_values = [str(v) for v in samples]
    elif (
        stats.get("basis") == "full"
        and 0 < int(stats.get("distinct") or 0) <= column_stats.TOP_VALUE_LIMIT
        and stats.get("top_values")
    ):
        # The sample read the WHOLE object and every distinct value fitted in
        # the retained top-values list — so this enumeration is complete, not a
        # truncation of the data. Columns the DISTINCT probe never reached
        # (beyond its per-table cap, or numeric) get a dictionary this way.
        enum_values = [
            str(item.get("value"))
            for item in stats["top_values"]
            if isinstance(item, dict) and item.get("value") is not None
        ] or None

    if column.get("is_primary_key"):
        return "identifier", enum_values

    if _is_date_type(dtype):
        return "temporal", None

    numeric = any(
        tok in dtype
        for tok in ("int", "decimal", "numeric", "float", "double", "real", "money", "number")
    )
    boolean = any(tok in dtype for tok in ("bool", "bit", "tinyint(1)"))

    if boolean or (numeric and _FLAG_HINT_RE.search(name)):
        return "flag", enum_values
    if _ID_HINT_RE.search(name):
        return "identifier", None
    if numeric:
        return "measure", None
    if _is_string_type(dtype):
        if enum_values:
            return "dimension", enum_values
        # Data-driven guard: a string whose sampled values are all distinct has
        # no cohort to group by, whatever its name — calling it a `dimension`
        # invites the role gate to offer it as a grouping key. The name-based
        # length rule stays as the answer when the sample is too small to
        # support the cardinality claim.
        sampled = int(stats.get("rows_sampled") or 0)
        cardinality = int(stats.get("distinct") or 0)
        if (
            sampled >= column_stats.MIN_ROWS_FOR_CARDINALITY
            and cardinality >= sampled
        ):
            return "free_text", None
        return "dimension" if len(name) <= 40 else "free_text", None
    return "unknown", enum_values


def _is_string_type(data_type: str) -> bool:
    dt = (data_type or "").lower()
    return any(k in dt for k in ("char", "text", "string", "enum"))


# Max columns whose values are echoed into one embedded document. An embedded
# document is a RETRIEVAL TARGET: enough vocabulary for a customer's own words
# to reach the right table, not a dump of the database.
_EMBED_VOCAB_COLUMNS = 6
_EMBED_VOCAB_VALUES = 6


def _col_name(column: dict) -> str:
    """The column's name from either dict shape — introspected or catalog-read.

    ``_introspect_one_table`` produces ``column_name``; the linker's enriched
    dicts use ``name``. Both reach these builders, and an embed pass must never
    abort the index over a key name.
    """
    return str(column.get("column_name") or column.get("name") or "")


def _column_values_text(c: dict) -> str:
    """The values that actually occur in one column, as one bounded line.

    Prefers the MEASURED top values (slice 2 — they come from the row sample and
    carry counts) and falls back to the DISTINCT-probe samples. Empty when
    nothing was sampled, so a document never advertises invented vocabulary.
    """
    stats = c.get("stats") if isinstance(c.get("stats"), dict) else {}
    values: list[str] = []
    for item in (stats.get("top_values") or [])[:_EMBED_VOCAB_VALUES]:
        if isinstance(item, dict) and item.get("value") is not None:
            values.append(str(item["value"]))
    if not values:
        values = [
            str(v) for v in (c.get("value_samples") or [])[:_EMBED_VOCAB_VALUES]
        ]
    if not values:
        return ""
    return "Values: " + ", ".join(values)[:120]


def _table_values_text(columns: list[dict]) -> str:
    """``\\nValues — col: v1, v2; …`` suffix for the embedded TABLE document."""
    parts: list[str] = []
    for c in columns:
        line = _column_values_text(c)
        if line:
            parts.append(f"{_col_name(c)}: {line[len('Values: '):]}")
        if len(parts) >= _EMBED_VOCAB_COLUMNS:
            break
    return ("\nValues — " + "; ".join(parts)) if parts else ""


def _table_doc_text(t: dict) -> str:
    """Embedded document for one table: meaning, size, provenance, vocabulary."""
    text = (
        f"[TABLE] {t['table_name']}\n"
        f"{t.get('description_zh', '')}\n"
        f"{t.get('description_en', '')}\n"
        f"Type: {t.get('table_type', 'TABLE')}  Rows: {t.get('row_count', '?')}"
    )
    if t.get("row_count_source"):
        provenance = "实测" if t["row_count_source"] == "exact" else "估算"
        text += f" ({provenance})"
    if t.get("table_comment"):
        text += f"\nComment: {t['table_comment']}"
    return text + _table_values_text(t.get("columns") or [])


def _column_doc_text(table_name: str, c: dict) -> str:
    """Embedded document for one column: meaning, measurement, vocabulary."""
    text = (
        f"[COLUMN] {table_name}.{_col_name(c)} [{c.get('data_type', '')}]\n"
        f"{c.get('description_zh', '')}\n"
        f"{c.get('description_en', '')}"
    )
    if c.get("comment"):
        text += f"\nComment: {c['comment']}"
    measured = column_stats.stats_annotation(c.get("stats"))
    if measured:
        text += f"\nMeasured: {measured}"
    values = _column_values_text(c)
    if values:
        text += f"\n{values}"
    return text


def _sample_column_values(conn: Any, table: str, columns: list[dict]) -> None:
    """Attach ``value_samples`` (distinct values) to string columns.

    These samples bridge the vocabulary gap between cryptic DDL names and
    how users phrase questions; they feed the description prompt, the
    embedded doc text, and — for low-cardinality columns — the persisted
    ``enum_values`` dictionary the answering path can filter on.

    Every string column is sampled (name-like ones first), because which
    column carries the business vocabulary is a property of the data, not of
    its name.
    """
    string_cols = [
        c for c in columns if _is_string_type(c.get("data_type", ""))
    ]
    name_like = [c for c in string_cols if _SAMPLEABLE_RE.search(c.get("column_name", ""))]
    others = [c for c in string_cols if c not in name_like]
    candidates = (name_like + others)[:_MAX_SAMPLE_COLS]
    for c in candidates:
        col = c.get("column_name", "")
        if not col:
            continue
        try:
            rows = conn.execute(
                _distinct_sample_sql(conn, table, col, _SAMPLE_LIMIT),
                max_rows=_SAMPLE_LIMIT,
                timeout_s=ROW_COUNT_TIMEOUT_S,
            )
            values = []
            for r in rows or []:
                v = list(r.values())[0] if isinstance(r, dict) else r[0]
                if v is None:
                    continue
                s = str(v).strip()[:_SAMPLE_VALUE_LEN]
                if s:
                    values.append(s)
            if values:
                c["value_samples"] = values
        except Exception:
            continue


# Max joinable columns sampled per table for value-overlap inference.
_MAX_JOINABLE_SAMPLE_COLS = 8


def _sample_joinable_values(conn: Any, table: str, columns: list[dict]) -> None:
    """Attach ``value_samples`` to joinable columns (int + short varchar).

    These samples feed ``detect_join_edges`` for VALUE_OVERLAP inference.
    Type-based selection only (no name keywords); columns already sampled by
    ``_sample_column_values`` are skipped to avoid duplicate queries.
    Best-effort: any failure is swallowed — join inference simply has fewer
    samples.
    """
    candidates = [
        c
        for c in columns
        if c.get("column_name")
        and type_bucket(c.get("data_type")) is not None
        and not c.get("value_samples")
    ]
    limit = settings.JOIN_EDGE_SAMPLE_LIMIT
    for c in candidates[:_MAX_JOINABLE_SAMPLE_COLS]:
        col = c.get("column_name", "")
        try:
            rows = conn.execute(
                _distinct_sample_sql(conn, table, col, limit),
                max_rows=limit,
                timeout_s=ROW_COUNT_TIMEOUT_S,
            )
            values = []
            for r in rows or []:
                v = list(r.values())[0] if isinstance(r, dict) else r[0]
                if v is None:
                    continue
                s = str(v).strip()[:_SAMPLE_VALUE_LEN]
                if s:
                    values.append(s)
            if values:
                c["value_samples"] = values
        except Exception:
            continue


def _safe_foreign_keys(conn: Any, table: str) -> list[dict]:
    try:
        if hasattr(conn, "get_foreign_keys"):
            return conn.get_foreign_keys(table) or []
    except Exception:
        pass
    return []


def _safe_table_facts(conn: Any, table: str) -> dict:
    """Engine-reported facts about a table, or ``{}`` — never a fabrication.

    A dialect that does not implement ``table_facts`` (mssql / oracle today)
    yields ``{}``, which the catalog records as "not collected"; a probe that
    fails yields ``{}`` as well and the log says which happened. Neither case
    is allowed to look like a real answer of zero.
    """
    fn = getattr(conn, "table_facts", None)
    if fn is None:
        return {}
    try:
        facts = fn(table)
    except Exception:
        logger.debug(
            "catalog_indexer: table_facts failed for '%s' (non-fatal)", table,
            exc_info=True,
        )
        return {}
    return facts if isinstance(facts, dict) else {}


def _safe_indexes(conn: Any, table: str) -> list[dict] | None:
    """Index inventory, or ``None`` when the source could not be asked.

    ``[]`` is a real answer — "the source declares no index on this object" —
    and must stay distinguishable from ``None`` ("we could not ask"), because
    only the first one justifies telling a user there is no key to join on.
    """
    fn = getattr(conn, "get_indexes", None)
    if fn is None:
        return None
    try:
        indexes = fn(table)
    except Exception:
        logger.debug(
            "catalog_indexer: get_indexes failed for '%s' (non-fatal)", table,
            exc_info=True,
        )
        return None
    if not isinstance(indexes, list):
        return None
    return indexes


# ── coverage probe ─────────────────────────────────────────────────────────

# Column-name tokens that strongly imply a temporal column.
_DATE_COLUMN_RE = re.compile(
    r"(date|time|day|month|year|dt$|created|updated|shipment|order_?date|"
    r"period|statistic|entry)",
    re.IGNORECASE,
)
# Explicit time-keyword columns are preferred over generic matches.
_STRONG_DATE_RE = re.compile(
    r"^(.*_)?(date|dt|time|day|month|year|created|updated)$",
    re.IGNORECASE,
)


def _is_date_type(data_type: str) -> bool:
    dt = (data_type or "").lower()
    return any(k in dt for k in ("date", "time", "timestamp"))


def _pick_date_column(columns: list[dict]) -> str | None:
    """Choose the most likely temporal column for coverage probing.

    Preference: (1) type is date/timestamp AND name matches a strong date
    keyword; (2) type is date/timestamp; (3) name matches a date keyword.
    Returns the column name or None if the table has no temporal column.
    """
    typed: list[dict] = []
    for c in columns:
        if _is_date_type(c.get("data_type", "")):
            typed.append(c)
        elif _DATE_COLUMN_RE.search(c.get("column_name", "")):
            typed.append(c)

    if not typed:
        return None

    # Strong keyword + typed wins.
    for c in typed:
        if _is_date_type(c.get("data_type", "")) and _STRONG_DATE_RE.search(
            c.get("column_name", "")
        ):
            return c.get("column_name")

    # Any typed column next.
    for c in typed:
        if _is_date_type(c.get("data_type", "")):
            return c.get("column_name")

    # Fallback: name-only match.
    return typed[0].get("column_name") or None


def _probe_coverage(conn: Any, table: str, columns: list[dict]) -> dict | None:
    """Probe ``MIN``/``MAX`` of the best temporal column (timeout-capped).

    Returns ``{date_column, min_date, max_date, probed_at}`` or None when the
    table has no temporal column or the probe fails/times out. Mirrors the
    ``_safe_row_count`` timeout discipline so a slow warehouse never stalls
    the index.
    """
    date_col = _pick_date_column(columns)
    if not date_col:
        return None
    try:
        rows = conn.execute(
            f"SELECT MIN({_maybe_quote(conn, date_col)}) AS min_date, "
            f"MAX({_maybe_quote(conn, date_col)}) AS max_date "
            f"FROM {_maybe_quote(conn, table)}",
            timeout_s=ROW_COUNT_TIMEOUT_S,
        )
        if not rows:
            return None
        r = rows[0]
        min_date = r.get("min_date") if isinstance(r, dict) else r[0]
        max_date = r.get("max_date") if isinstance(r, dict) else r[1]
        # Normalize date/datetime to a comparable ISO string.
        return {
            "date_column": date_col,
            "min_date": _normalize_temporal(min_date),
            "max_date": _normalize_temporal(max_date),
            "probed_at": datetime.utcnow().isoformat() + "Z",
        }
    except Exception:
        logger.debug(
            "catalog_indexer: coverage probe failed for table '%s' (non-fatal)",
            table, exc_info=True,
        )
        return None


def _normalize_temporal(value: Any) -> str | None:
    """Coerce a DB temporal value to ``YYYY-MM-DD`` (or ISO datetime) string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value[:10]
    if isinstance(value, (int, float)):
        # Some drivers return a unix epoch — best-effort.
        try:
            return datetime.utcfromtimestamp(value).strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _maybe_quote(conn: Any, name: str) -> str:
    """Quote an identifier with the CONNECTED source's dialect rules.

    Previously a no-op that returned the bare name, which is only safe while
    every probe target happens to be a non-reserved, lower-case identifier — on
    a source with a table called ``order`` or ``user`` the probe SQL broke.
    The dialect layer raises for unsafe names; we degrade to the bare name only
    in that case, and to unquoted output when the dialect is unknown (the probe
    then fails visibly instead of running with another vendor's quoting).
    """
    from app.services.db.dialect import maybe_profile

    profile = maybe_profile(getattr(conn, "dialect", None))
    if profile is None:
        return name
    try:
        return profile.quote_ident(name)
    except ValueError:
        return name


def _distinct_sample_sql(conn: Any, table: str, column: str, n: int) -> str:
    """Distinct-value probe SQL using the source's own pagination syntax.

    ``LIMIT`` was hardcoded here; SQL Server needs ``SELECT DISTINCT TOP n`` and
    Oracle needs ``FETCH FIRST n ROWS ONLY``, so the statement was a syntax
    error on both. The dialect layer builds it; an unknown dialect falls back to
    the bare (unpaginated) statement so the probe fails on its own terms rather
    than borrowing another vendor's clause.
    """
    from app.services.db.dialect import maybe_profile

    profile = maybe_profile(getattr(conn, "dialect", None))
    if profile is None:
        return (
            f"SELECT DISTINCT {_maybe_quote(conn, column)} "
            f"FROM {_maybe_quote(conn, table)}"
        )
    try:
        return profile.distinct_sample_sql(table, column, n)
    except ValueError:
        return (
            f"SELECT DISTINCT {_maybe_quote(conn, column)} "
            f"FROM {_maybe_quote(conn, table)}"
        )


# ── LLM descriptions ───────────────────────────────────────────────────────

async def _generate_descriptions(tables: list[dict]) -> None:
    """Batch LLM description pass — mutates tables in-place with descriptions.

    Runs up to ``LLM_MAX_CONCURRENT`` LLM calls in parallel via
    ``asyncio.gather`` (capped to avoid hitting upstream rate limits).
    """
    if not tables:
        return
    from app.services.llm_service import call_llm

    # Build (batch_index, batch) pairs
    batches = [
        tables[i : i + LLM_BATCH_SIZE]
        for i in range(0, len(tables), LLM_BATCH_SIZE)
    ]

    sem = asyncio.Semaphore(LLM_MAX_CONCURRENT)

    async def _run_one(idx: int, batch: list[dict]) -> None:
        async with sem:
            prompt_parts = _build_description_prompt(batch)
            user_prompt = "\n\n".join(prompt_parts)
            try:
                result = await call_llm(
                    messages=[
                        {"role": "system", "content": _TABLE_DESCRIPTION_SYSTEM},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=LLM_TEMP,
                    response_json_schema=_JSON_SCHEMA,
                    task_type="catalog_description",
                )
                _apply_descriptions(batch, result)
            except Exception:
                logger.exception(
                    "catalog_indexer: LLM batch %d/%d failed — leaving descriptions empty",
                    idx + 1, len(batches),
                )

    # Fire all batches under the semaphore — gather runs them concurrently.
    await asyncio.gather(*[_run_one(i, b) for i, b in enumerate(batches)])

    # A batch that fails, or a model that answers with only part of a wide
    # table's columns, leaves those columns with a blank description — which is
    # indistinguishable from "no meaning recorded" downstream. Repair it here.
    await _backfill_missing_descriptions(tables)


def _is_blank_description(value: Any) -> bool:
    return not str(value or "").strip()


# Source comments arrive from arbitrary DDL — multi-line, padded, occasionally
# enormous. They are stored verbatim (they are evidence), so only whitespace is
# normalised and an absurd length is cut.
_MAX_SOURCE_COMMENT = 2000


def _clean_comment(value: Any) -> str | None:
    """Normalise a database comment: collapse whitespace, bound length, or None."""
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    if not text:
        return None
    return text[:_MAX_SOURCE_COMMENT]


# ── `column(业务含义)` labels that leaked into the column NAME ───────────────
# 210 columns of the live catalog are stored as `tax_price(含税单价)` instead of
# `tax_price`. Two consequences, both user visible: the DDL handed to the model
# is unqueryable, and the description pass can never match the column it just
# described (the model answers `tax_price`, the row says `tax_price(含税单价)`),
# so those columns stayed blank across every index run. The parenthetical is a
# comment; the name is the identifier.
_LABEL_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\(([^()]{1,40})\)$")
# A view column can legitimately BE a call expression (`count(*)`, `sum(amount)`).
# Treating that as "name(label)" would corrupt a real column, so the base of a
# known SQL function is never split.
_SQL_FUNCTION_BASES = frozenset({
    "count", "sum", "avg", "min", "max", "ifnull", "coalesce", "concat",
    "cast", "convert", "round", "abs", "length", "char_length", "substring",
    "substr", "trim", "upper", "lower", "date", "year", "month", "day",
    "now", "left", "right", "replace", "group_concat", "json_extract",
})
_LABEL_OPERATOR_CHARS = frozenset("*'\"`,;=+/<>%")


def _split_column_label(raw_name: Any) -> tuple[str, str]:
    """``"tax_price(含税单价)"`` → ``("tax_price", "含税单价")``.

    Returns the input unchanged (and an empty label) when the shape is not a
    ``name(label)`` pair or the base looks like a SQL call expression.
    """
    raw = str(raw_name or "")
    if "(" not in raw:
        return raw, ""
    m = _LABEL_PATTERN.match(raw.strip())
    if not m:
        return raw, ""
    name, label = m.group(1), m.group(2).strip()
    if not label or name.lower() in _SQL_FUNCTION_BASES:
        return raw, ""
    if any(ch in _LABEL_OPERATOR_CHARS for ch in label):
        return raw, ""
    return name, label


def _label_description(label: str) -> tuple[str, str]:
    """Route an extracted label to the description column that matches its script."""
    if any("\u4e00" <= ch <= "\u9fff" for ch in label):
        return label, ""
    return "", label


def _missing_description_targets(tables: list[dict]) -> list[tuple[dict, list[dict], bool]]:
    """Tables whose description pass came back incomplete.

    Returns ``(table, columns_without_description, table_description_missing)``.
    A blank column description is NOT the same as an unknown meaning: the
    prompt instructs the model to write ``未知`` when it cannot tell. Blank means
    the model's JSON omitted the column — the normal outcome for an 8-table
    batch of 60-100 column ERP tables, and exactly what leaves look-alike
    columns (``FCLOSESTATUS`` / ``FDOCUMENTSTATUS``) to be picked on name alone.
    """
    hits: list[tuple[dict, list[dict], bool]] = []
    for t in tables or []:
        cols = [
            c for c in (t.get("columns") or [])
            if _is_blank_description(c.get("description_en"))
            and _is_blank_description(c.get("description_zh"))
        ]
        table_blank = (
            _is_blank_description(t.get("description_en"))
            and _is_blank_description(t.get("description_zh"))
        )
        if cols or table_blank:
            hits.append((t, cols, table_blank))
    return hits


async def _backfill_missing_descriptions(tables: list[dict]) -> None:
    """Second, narrower description pass for whatever the batch pass missed.

    One LLM call per incomplete table, asking only for the columns still
    blank — a wide table's prompt is what caused the omission, so re-asking it
    in a batch of eight would reproduce the same truncation. Bounded by
    ``CATALOG_DESC_BACKFILL_MAX_TABLES``; failures are non-fatal (the column
    simply stays blank, exactly as before).
    """
    if not getattr(settings, "CATALOG_DESC_BACKFILL_ENABLED", True):
        return
    budget = int(getattr(settings, "CATALOG_DESC_BACKFILL_MAX_TABLES", 60) or 0)
    if budget <= 0:
        return

    targets = _missing_description_targets(tables)
    if not targets:
        return

    import asyncio

    from app.services.llm_service import call_llm

    selected = targets[:budget]
    blank_cols = sum(len(cols) for _, cols, _ in targets)
    logger.info(
        "catalog_indexer: %d table(s) returned blank descriptions (%d blank columns); "
        "backfilling %d table(s), one call each",
        len(targets), blank_cols, len(selected),
    )
    if len(targets) > len(selected):
        logger.info(
            "catalog_indexer: backfill cap reached (%d) — %d table(s) left blank this run",
            budget, len(targets) - len(selected),
        )

    sem = asyncio.Semaphore(LLM_MAX_CONCURRENT)

    async def _run_one(table: dict, cols: list[dict], table_blank: bool) -> None:
        async with sem:
            proxy = dict(table)
            proxy["columns"] = cols
            if cols:
                user_prompt = _build_description_prompt([proxy])
                prompt = user_prompt[0] if user_prompt else f"表名: {table.get('table_name')}"
                prompt += (
                    f"\n\n(该表未完成说明的字段如上，共 {len(cols)} 个，请补齐"
                    "；已有说明的字段不必重复输出)"
                )
            else:
                # Table-description-only repair. Passing an empty column list
                # made the model answer with nothing, so give it the column
                # names as context and ask for the table description alone —
                # the column descriptions are already there and must not be
                # overwritten.
                names = ", ".join(
                    str(c.get("column_name")) for c in (table.get("columns") or [])[:40]
                )
                prompt = (
                    f"表名: {table.get('table_name')}\n"
                    f"  类型: {table.get('table_type', 'TABLE')}\n"
                    f"  字段: {names or '(无字段信息)'}\n\n"
                    "(该表所有字段均已有说明，只缺表级说明。"
                    "请只输出这张表的中英文业务含义，columns 返回空数组。)"
                )
            try:
                result = await call_llm(
                    messages=[
                        {"role": "system", "content": _TABLE_DESCRIPTION_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=LLM_TEMP,
                    response_json_schema=_JSON_SCHEMA,
                    task_type="catalog_description_backfill",
                )
                if cols:
                    # Column dicts inside the proxy are the SAME objects as in
                    # the original table, so their descriptions land where they
                    # belong; the table-level fields need copying back.
                    _apply_descriptions([proxy], result)
                if table_blank:
                    described = _table_description_from_result(result, table.get("table_name"))
                    table["description_zh"] = (
                        proxy.get("description_zh") if cols else described[0]
                    ) or table.get("description_zh") or ""
                    table["description_en"] = (
                        proxy.get("description_en") if cols else described[1]
                    ) or table.get("description_en") or ""
            except Exception:
                logger.exception(
                    "catalog_indexer: description backfill failed for table %s "
                    "(columns stay blank)",
                    table.get("table_name"),
                )

    await asyncio.gather(*[_run_one(*t) for t in selected])

    attempted = [t for t, _, _ in selected]
    still_blank = sum(len(cols) for _, cols, _ in _missing_description_targets(attempted))
    if still_blank:
        logger.info(
            "catalog_indexer: %d column description(s) still blank after backfill",
            still_blank,
        )


def _column_prompt_line(c: dict) -> str:
    """One column line for the description prompt (samples + source comment).

    Carries three kinds of evidence, each labelled as what it is: the values
    seen (``values:``), the measured distribution (``实测`` — see
    ``column_stats.stats_prompt_suffix``; a bounded sample is marked as one), and
    the customer's own comment (``数据库注释``), which is authoritative.
    """
    line = (
        f"  - {c['column_name']} ({c['data_type']}"
        f"{', PK' if c['is_primary_key'] else ''}"
        f"{', nullable' if c['is_nullable'] else ''})"
    )
    if c.get("value_samples"):
        line += f" values: {', '.join(c['value_samples'][:12])}"
    line += column_stats.stats_prompt_suffix(c.get("stats"))
    # The source's own column comment is authoritative context from the
    # customer's DDL — the description must agree with it, not replace it.
    if c.get("comment"):
        line += f" // 数据库注释: {c['comment']}"
    return line


def _build_description_prompt(batch: list[dict]) -> list[str]:
    parts: list[str] = []
    for t in batch:
        cols = "\n".join(_column_prompt_line(c) for c in t["columns"])
        sample = ""
        if t.get("row_count"):
            provenance = "实测" if t.get("row_count_source") == "exact" else "估算"
            sample = f"\n  行数: {t['row_count']:,} ({provenance})"
        header = f"表名: {t['table_name']}\n  类型: {t['table_type']}{sample}"
        if t.get("table_comment"):
            header += f"\n  数据库注释: {t['table_comment']}"
        parts.append(f"{header}\n  字段:\n{cols}")
    return parts


def _table_description_from_result(llm_result: dict, table_name: str | None) -> tuple[str, str]:
    """Pull just the table-level (zh, en) description out of an LLM reply."""
    data = llm_result.get("data") if isinstance(llm_result, dict) else {}
    if data is None:
        data = {}
    described = data if isinstance(data, list) else data.get("tables", [])
    if not isinstance(described, list):
        return "", ""
    for d in described:
        if not isinstance(d, dict):
            continue
        if table_name and d.get("table_name") and d.get("table_name") != table_name:
            continue
        return (
            str(d.get("description_zh") or "").strip(),
            str(d.get("description_en") or "").strip(),
        )
    return "", ""


def _apply_descriptions(batch: list[dict], llm_result: dict) -> None:
    data = llm_result.get("data") if isinstance(llm_result, dict) else {}
    if data is None:
        data = {}
    if isinstance(data, list):
        # Tolerate models that emit a bare array despite the object schema.
        described = data
    else:
        described = data.get("tables", [])
    if isinstance(described, list):
        desc_map = {d.get("table_name", ""): d for d in described}
        for t in batch:
            d = desc_map.get(t["table_name"], {})
            t["description_zh"] = d.get("description_zh", "")
            t["description_en"] = d.get("description_en", "")
            col_map = {c.get("column_name", ""): c for c in d.get("columns", [])}
            # A catalog row can still hold `tax_price(含税单价)` while the model
            # answers `tax_price`; match on the clean identifier too, or the
            # description is silently dropped.
            base_map = {}
            for key, val in col_map.items():
                base, _ = _split_column_label(key)
                base_map.setdefault(base, val)
            for c in t["columns"]:
                cd = col_map.get(c["column_name"], {})
                if not cd:
                    base, _ = _split_column_label(c["column_name"])
                    cd = base_map.get(base, {})
                c["description_zh"] = cd.get("description_zh", "")
                c["description_en"] = cd.get("description_en", "")
                c["unit"] = (cd.get("unit") or "").strip() or None


# ── persistence ────────────────────────────────────────────────────────────

def _persist_catalog(db: Session, kb_id: str, tables: list[dict]) -> None:
    """UPSERT into kb_table_meta + kb_column_meta (idempotent on re-index)."""
    existing_table_ids: dict[str, str] = {}

    for t in tables:
        # Upsert table meta
        existing = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id == kb_id,
                KBTableMeta.schema_name == t.get("schema_name", ""),
                KBTableMeta.table_name == t["table_name"],
            )
            .first()
        )
        role = t.get("table_role", "unknown") or "unknown"
        table_comment = _clean_comment(t.get("table_comment"))
        indexes_json = t.get("indexes")
        if existing:
            meta = existing
            meta.table_type = t.get("table_type", "TABLE")
            meta.row_count = t.get("row_count")
            meta.description_zh = t.get("description_zh")
            meta.description_en = t.get("description_en")
            meta.coverage_json = t.get("coverage_json")
            # P-SV: grain is recomputed each index (structure may have
            # changed). A human correction lives in
            # project_catalog_overlay.grain, which overrides at read time —
            # so overwriting the auto value here is safe.
            meta.grain = t.get("grain")
            # Preserve a human/LLM-set role; only overwrite with a fresh
            # auto-classification (index-time) if still "unknown" or coming
            # from this same index pass.
            if role != "unknown":
                meta.table_role = role
        else:
            import uuid as _uuid
            meta = KBTableMeta(
                id=str(_uuid.uuid4()),
                kb_id=kb_id,
                schema_name=t.get("schema_name", ""),
                table_name=t["table_name"],
                table_type=t.get("table_type", "TABLE"),
                row_count=t.get("row_count"),
                description_zh=t.get("description_zh"),
                description_en=t.get("description_en"),
                coverage_json=t.get("coverage_json"),
                table_role=role,
                grain=t.get("grain"),
            )
            db.add(meta)
        # ── source truth (2026-09-20) — written on every index, never merged
        # into the LLM fields, so "what the database says" stays auditable.
        # Both tables and views carry a comment / index inventory; a table with
        # no declared index gets [] (a real answer), not None (not collected).
        meta.source_comment = table_comment
        meta.indexes_json = indexes_json if isinstance(indexes_json, list) else None
        meta.size_bytes = t.get("size_bytes")
        meta.row_count_source = t.get("row_count_source")
        if _is_blank_description(meta.description_zh) and _is_blank_description(meta.description_en):
            if table_comment:
                # Never leave the customer's own label unused: when the
                # description pass produced nothing, the source's comment is
                # the best description that exists — and it is marked as such.
                meta.description_zh = meta.description_zh or table_comment
                meta.description_en = meta.description_en or table_comment
                meta.description_source = "source_comment"
            # else: stays blank. A blank description is a defect to fix, and
            # inventing one from a name would hide that.
        else:
            meta.description_source = "llm"
        # OCI #1: trust score — human-verified rows keep their 1.0.
        if (meta.description_source or "") != "human":
            meta.description_confidence = score_description_confidence(
                meta.description_zh, meta.description_en, meta.description_source,
            )
        db.flush()
        existing_table_ids[t["table_name"]] = meta.id

        # Upsert columns
        table_samples: dict[str, list[str]] = {}
        for c in t.get("columns", []):
            # A `column(业务含义)` label must never reach the catalog as the
            # column NAME — see _split_column_label. Applied here because this
            # is the only place that writes kb_column_meta.column_name.
            col_name, label = _split_column_label(c["column_name"])
            desc_zh = c.get("description_zh", "")
            desc_en = c.get("description_en", "")
            if label and not (_is_blank_description(desc_zh) and _is_blank_description(desc_en)):
                label = ""            # a real description already exists — keep it
            elif label:
                label_zh, label_en = _label_description(label)
                desc_zh, desc_en = label_zh or desc_zh, label_en or desc_en
            existing_col = (
                db.query(KBColumnMeta)
                .filter(
                    KBColumnMeta.table_meta_id == meta.id,
                    KBColumnMeta.column_name == col_name,
                )
                .first()
            )
            if existing_col:
                col = existing_col
                col.ordinal = c.get("ordinal", 0)
                col.data_type = c.get("data_type", "")
                col.is_nullable = c.get("is_nullable", True)
                col.is_primary_key = c.get("is_primary_key", False)
                col.default_value = c.get("default_value")
                col.description_zh = desc_zh
                col.description_en = desc_en
            else:
                col = KBColumnMeta(
                    id=str(_uuid.uuid4()),
                    table_meta_id=meta.id,
                    column_name=col_name,
                    ordinal=c.get("ordinal", 0),
                    data_type=c.get("data_type", ""),
                    is_nullable=c.get("is_nullable", True),
                    is_primary_key=c.get("is_primary_key", False),
                    default_value=c.get("default_value"),
                    description_zh=desc_zh,
                    description_en=desc_en,
                )
                db.add(col)
            # ── source truth, per column ──
            col.source_comment = _clean_comment(c.get("comment"))
            col.key_role = (c.get("key") or "").upper() or None
            if _is_blank_description(desc_zh) and _is_blank_description(desc_en):
                if col.source_comment:
                    col.description_zh = col.source_comment
                    col.description_en = col.source_comment
                    col.description_source = "source_comment"
                else:
                    col.description_source = ""
            else:
                col.description_source = "llm"
            # Semantic typing is recomputed on every index (it reads the fresh
            # value samples), so a re-index repairs drift automatically.
            semantic_type, enum_values = classify_column_semantics(c, role)
            col.semantic_type = semantic_type
            col.enum_values = enum_values
            col.unit = c.get("unit") or None
            # OCI #1: trust score — human-verified rows keep their 1.0.
            # Runs AFTER unit/enum assignment so data corroboration is fresh.
            if (col.description_source or "") != "human":
                col.description_confidence = score_description_confidence(
                    desc_zh, desc_en, col.description_source,
                    unit=col.unit, enum_values=col.enum_values,
                )
            # ── per-column statistics (slice 2) ──
            # ``stats_json`` is the measurement; ``sample_values`` is the
            # vocabulary bridge `{value: count}` the answer-verification gate
            # and the description prompt read.  No stats were collected (empty
            # object, unreadable, binary-only) → both stay None rather than
            # becoming an empty dict, so "we did not look" is visible.
            col.stats_json = c.get("stats") or None
            col.sample_values = column_stats.column_sample_values(c.get("stats"))
            values = list(col.sample_values or {})
            if values:
                table_samples[col_name] = values[: column_stats.TOP_VALUE_LIMIT]
        # Table-level ``{column: [values]}`` — the shape `domain_discovery` and
        # `semantic_store.sample_values()` read.  Written on every index so a
        # source whose data changed gets a fresh vocabulary.
        meta.sample_values = table_samples or None
    db.commit()

    # Store table_meta_id lookup on the table dict for relation persistence
    for t in tables:
        t["_meta_id"] = existing_table_ids.get(t["table_name"])


def _persist_relations(db: Session, kb_id: str, tables: list[dict]) -> None:
    """Persist FK relations into kb_table_relation."""
    name_to_id = {t["table_name"]: t.get("_meta_id") for t in tables}
    seen: set[tuple] = set()

    for t in tables:
        for fk in t.get("foreign_keys", []):
            ref_table = fk.get("ref_table", "")
            src_id = name_to_id.get(t["table_name"])
            tgt_id = name_to_id.get(ref_table)
            if not src_id or not tgt_id:
                continue

            key = (src_id, tgt_id, fk.get("column", ""), fk.get("ref_column", ""))
            if key in seen:
                continue
            seen.add(key)

            existing = (
                db.query(KBTableRelation)
                .filter(
                    KBTableRelation.kb_id == kb_id,
                    KBTableRelation.source_table_meta_id == src_id,
                    KBTableRelation.target_table_meta_id == tgt_id,
                )
                .first()
            )
            if not existing:
                rel = KBTableRelation(
                    id=str(uuid.uuid4()),
                    kb_id=kb_id,
                    source_table_meta_id=src_id,
                    target_table_meta_id=tgt_id,
                    relation_type="FK",
                    source_columns=[fk.get("column", "")],
                    target_columns=[fk.get("ref_column", "")],
                    # P0-6: a declared FK is by definition many(child)→
                    # one(parent) — stamp it so the execution gate's fanout
                    # check never has to guess (engine-agnostic: any
                    # connector that exposes declared FKs).
                    source_cardinality="many",
                    target_cardinality="one",
                    confidence=1.0,
                    description=f"{t['table_name']}.{fk.get('column')} → {ref_table}.{fk.get('ref_column')}",
                )
                db.add(rel)
    db.commit()


def _persist_join_edges(db: Session, kb_id: str, tables: list[dict]) -> None:
    """Infer and persist VALUE_OVERLAP / NAME_MATCH edges into kb_table_relation.

    Ranking guard (explicit requirement): an existing edge for the same
    directed pair is only overwritten when the new edge has *strictly higher*
    confidence. A declared FK (confidence 1.0) is never downgraded.
    """
    edges = detect_join_edges(tables)
    if not edges:
        logger.info("join_edge: kb=%s no inferred edges", kb_id)
        return

    name_to_id = {t["table_name"]: t.get("_meta_id") for t in tables}
    added = 0
    skipped = 0

    def _edge_cardinality(e: dict) -> tuple[str | None, str | None]:
        """P0-6: derive (source, target) cardinality from structural signals.

        Uses the join columns' key roles (PRI/UNI/MUL) and the indexed row
        counts via ``join_cardinality.derive_cardinality`` — generic on every
        engine; returns (None, None) when the evidence is insufficient, which
        the fanout consumer treats as unknown, never as safe.
        """
        from app.services.knowledge_graph.join_cardinality import (
            derive_cardinality,
        )

        src_id = name_to_id.get(e["source_table"])
        tgt_id = name_to_id.get(e["target_table"])
        roles: dict[tuple[str, str], list[str]] = {}
        counts: dict[str, Any] = {}
        try:
            for tid in (src_id, tgt_id):
                if not tid:
                    continue
                meta = db.get(KBTableMeta, tid)
                if meta is not None:
                    counts[tid] = meta.row_count
                cols = (
                    db.query(KBColumnMeta)
                    .filter(KBColumnMeta.table_meta_id == tid)
                    .all()
                )
                for c in cols:
                    roles.setdefault(tid, [])
                    role = str(getattr(c, "key_role", "") or "").upper()
                    name_l = str(c.column_name or "").lower()
                    if role and name_l in {s.lower() for s in (
                        e["source_columns"] if tid == src_id else e["target_columns"]
                    )}:
                        roles[tid].append(role)
        except Exception:  # pragma: no cover — metadata optional
            return (None, None)
        return derive_cardinality(
            e.get("kind"),
            source_key_roles=roles.get(src_id, []),
            target_key_roles=roles.get(tgt_id, []),
            source_row_count=counts.get(src_id),
            target_row_count=counts.get(tgt_id),
        )

    for e in edges:
        src_id = name_to_id.get(e["source_table"])
        tgt_id = name_to_id.get(e["target_table"])
        if not src_id or not tgt_id:
            continue

        existing = (
            db.query(KBTableRelation)
            .filter(
                KBTableRelation.kb_id == kb_id,
                KBTableRelation.source_table_meta_id == src_id,
                KBTableRelation.target_table_meta_id == tgt_id,
            )
            .first()
        )

        if existing:
            # Guard 1: a declared FK is ground truth — never downgrade.
            if existing.relation_type == "FK":
                logger.warning(
                    "join_edge: skipping inferred %s edge for FK pair %s→%s",
                    e["kind"], e["source_table"], e["target_table"],
                )
                skipped += 1
                continue
            # Guard 2: overwrite only on strictly higher confidence.
            if (existing.confidence or 0.0) >= e["confidence"]:
                logger.warning(
                    "join_edge: skipping lower-confidence edge (%s %.3f) for "
                    "existing pair %s→%s (%s %.3f)",
                    e["kind"], e["confidence"], e["source_table"],
                    e["target_table"], existing.relation_type,
                    existing.confidence,
                )
                skipped += 1
                continue
            existing.relation_type = e["kind"]
            existing.source_columns = e["source_columns"]
            existing.target_columns = e["target_columns"]
            existing.confidence = e["confidence"]
            _src_card, _tgt_card = _edge_cardinality(e)
            if _src_card and _tgt_card:
                existing.source_cardinality = _src_card
                existing.target_cardinality = _tgt_card
            existing.description = json.dumps(e.get("evidence", {}), ensure_ascii=False)
            added += 1
        else:
            _src_card, _tgt_card = _edge_cardinality(e)
            db.add(KBTableRelation(
                id=str(uuid.uuid4()),
                kb_id=kb_id,
                source_table_meta_id=src_id,
                target_table_meta_id=tgt_id,
                relation_type=e["kind"],
                source_columns=e["source_columns"],
                target_columns=e["target_columns"],
                source_cardinality=_src_card,
                target_cardinality=_tgt_card,
                confidence=e["confidence"],
                description=json.dumps(e.get("evidence", {}), ensure_ascii=False),
            ))
            added += 1

    db.commit()
    logger.info(
        "join_edge: kb=%s persisted %d inferred edges (%d skipped)",
        kb_id, added, skipped,
    )


# ── embeddings (ChromaDB) ──────────────────────────────────────────────────

def _embed_catalog(kb_id: str, tables: list[dict]) -> None:
    """Embed table-level and column-level docs into Chroma catalog_{kb_id}."""
    from app.services.document_ingestion.store import _get_client
    from app.services.document_ingestion.embedder import get_embedding_function

    client = _get_client()
    ef = get_embedding_function()

    collection_name = f"catalog_{kb_id}"
    # Delete-and-recreate ensures clean state on re-index
    try:
        client.delete_collection(collection_name)
    except Exception:
        pass
    coll = client.get_or_create_collection(
        name=collection_name,
        embedding_function=ef,
        metadata={"kb_id": kb_id},
    )

    if not tables:
        return

    # ── table docs ──
    table_ids: list[str] = []
    table_texts: list[str] = []
    table_metadatas: list[dict] = []
    for t in tables:
        mid = t.get("_meta_id", "")
        text = _table_doc_text(t)
        table_ids.append(f"t:{mid}")
        table_texts.append(text)
        table_metadatas.append({
            "kb_id": kb_id,
            "table_meta_id": mid,
            "table_name": t["table_name"],
            "row_count": t.get("row_count", 0) or 0,
            "kind": "table",
        })

    # ── column docs ──
    col_ids: list[str] = []
    col_texts: list[str] = []
    col_metadatas: list[dict] = []
    for t in tables:
        mid = t.get("_meta_id", "")
        for c in t.get("columns", []):
            text = _column_doc_text(t["table_name"], c)
            col_ids.append(f"c:{mid}:{c['column_name']}")
            col_texts.append(text)
            col_metadatas.append({
                "kb_id": kb_id,
                "table_meta_id": mid,
                "table_name": t["table_name"],
                "column_name": c["column_name"],
                "data_type": c["data_type"],
                "is_pk": c.get("is_primary_key", False),
                "kind": "column",
            })

    # ── add in batches to avoid massive payloads ──
    all_ids = table_ids + col_ids
    all_texts = table_texts + col_texts
    all_metadatas = table_metadatas + col_metadatas

    BATCH = 200
    for i in range(0, len(all_ids), BATCH):
        coll.add(
            ids=all_ids[i : i + BATCH],
            documents=all_texts[i : i + BATCH],
            metadatas=all_metadatas[i : i + BATCH],
        )

    logger.info(
        "catalog_indexer: Chroma collection '%s' — %d tables, %d columns embedded",
        collection_name, len(table_ids), len(col_ids),
    )


# ── incremental updates ─────────────────────────────────────────────────────

def update_column_embedding(
    kb_id: str,
    table_meta_id: str,
    table_name: str,
    column_meta_id: str,
    column_name: str,
    description_zh: str | None,
    description_en: str | None,
) -> bool:
    """Refresh a single column's ChromaDB embedding after a human edit.

    Column docs are ids ``c:{table_meta_id}:{column_name}`` (see
    _embed_catalog). Returns True if updated; never raises — a missing
    collection or an edit-time failure must never break the turn.
    """
    try:
        from app.services.document_ingestion.store import _get_client
        from app.services.document_ingestion.embedder import get_embedding_function

        client = _get_client()
        ef = get_embedding_function()
        collection_name = f"catalog_{kb_id}"
        try:
            coll = client.get_collection(collection_name, embedding_function=ef)
        except Exception:
            logger.info(
                "update_column_embedding: collection '%s' missing — skipping",
                collection_name,
            )
            return False

        text = (
            f"[COLUMN] {table_name}.{column_name}\n"
            f"{description_zh or ''}\n"
            f"{description_en or ''}"
        )
        doc_id = f"c:{table_meta_id}:{column_name}"
        coll.update(ids=[doc_id], documents=[text])
        return True
    except Exception as e:
        logger.warning("update_column_embedding failed (non-fatal): %s", e)
        return False


def update_table_embedding(
    kb_id: str,
    table_meta_id: str,
    table_name: str,
    description_zh: str | None,
    description_en: str | None,
    row_count: int | None,
    table_type: str = "TABLE",
) -> bool:
    """Refresh a single table's ChromaDB embedding after a description edit.

    Returns True if the embedding was updated, False if the collection does
    not exist (caller can ignore).  Never raises — failures are logged and
    swallowed so user edits don't break.
    """
    try:
        from app.services.document_ingestion.store import _get_client
        from app.services.document_ingestion.embedder import get_embedding_function

        client = _get_client()
        ef = get_embedding_function()
        collection_name = f"catalog_{kb_id}"
        try:
            coll = client.get_collection(collection_name, embedding_function=ef)
        except Exception:
            logger.info(
                "update_table_embedding: collection '%s' missing — skipping",
                collection_name,
            )
            return False

        text = (
            f"[TABLE] {table_name}\n"
            f"{description_zh or ''}\n"
            f"{description_en or ''}\n"
            f"Type: {table_type}  "
            f"Rows: {row_count if row_count is not None else '?'}"
        )
        coll.update(
            ids=[f"t:{table_meta_id}"],
            documents=[text],
            metadatas=[{
                "kb_id": kb_id,
                "table_meta_id": table_meta_id,
                "table_name": table_name,
                "row_count": row_count or 0,
                "kind": "table",
            }],
        )
        logger.info(
            "update_table_embedding: kb=%s table=%s updated",
            kb_id, table_name,
        )
        return True
    except Exception:
        logger.exception("update_table_embedding failed for kb=%s", kb_id)
        return False
