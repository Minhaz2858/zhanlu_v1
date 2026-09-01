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

async def index_kb_catalog(kb: KnowledgeBase, db: Session) -> None:
    """Main entry point — idempotent catalog index for one KnowledgeBase.

    Async: we call the LLM for descriptions (async HTTP).
    Introspection (connector) runs via asyncio.to_thread.
    Caller is responsible for committing ``kb.catalog_status`` changes.
    """
    import asyncio

    t0 = time.monotonic()
    try:
        _set_status(db, kb, "indexing")

        tables = await asyncio.to_thread(_introspect_tables_sync, kb, db)
        logger.info("catalog_indexer: kb=%s discovered %d tables", kb.id, len(tables))

        if tables:
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
            await _bootstrap_metrics(db, kb, tables)
        await asyncio.to_thread(_embed_catalog, kb.id, tables)

        _set_status(db, kb, "ready")
        kb.item_count = len(tables)
        db.commit()
        _sync_registry(db, kb, len(tables))
        elapsed = time.monotonic() - t0
        logger.info(
            "catalog_indexer: kb=%s done — %d tables (item_count=%d) in %.1fs",
            kb.id, len(tables), kb.item_count, elapsed,
        )
    except Exception:
        logger.exception("catalog_indexer: kb=%s failed", kb.id)
        _set_status(db, kb, "error")


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

def _set_status(db: Session, kb: KnowledgeBase, status: str) -> None:
    kb.catalog_status = status
    db.commit()


def _introspect_tables_sync(kb: KnowledgeBase, db: Session) -> list[dict]:
    """Sync wrapper: introspect full schema via connector (runs in thread).

    Uses a ThreadPoolExecutor to introspect tables in parallel — each worker
    gets its own connector (via context manager) so SQLAlchemy connections
    are never shared across threads.
    """
    with get_connector(kb) as conn:
        table_names = _safe_list_tables(conn)
    if len(table_names) > MAX_TABLES:
        logger.warning(
            "catalog_indexer: kb=%s has %d tables, capping at %d",
            kb.id, len(table_names), MAX_TABLES,
        )
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
                continue
            if result is not None:
                tables.append(result)
    return tables


def _introspect_one_table(kb: KnowledgeBase, table_name: str) -> dict | None:
    """Introspect a single table in a worker thread.

    Each worker creates its own connector (via context manager) so that
    SQLAlchemy connections are never shared across threads.  Returns None
    on failure (logged by caller).
    """
    try:
        with get_connector(kb) as conn:
            desc = conn.describe_table(table_name)
            row_count = _safe_row_count(conn, table_name)
            fks = _safe_foreign_keys(conn, table_name)
            schema_name, table_type, columns = _normalize_desc(desc)
            if row_count and columns:
                _sample_column_values(conn, table_name, columns)
                if settings.CATALOG_JOIN_EDGES_ENABLED:
                    _sample_joinable_values(conn, table_name, columns)
            coverage = None
            if settings.KG_COVERAGE_PROBE_ENABLED and columns:
                coverage = _probe_coverage(conn, table_name, columns)
        return {
            "schema_name": schema_name,
            "table_name": table_name,
            "table_type": table_type,
            "row_count": row_count,
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
    """Mutate each table dict with a structural ``table_role`` classification.

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


def _classify_table_role(table: dict, fk_indegree: int) -> str:
    """Classify a single table's role via structural heuristics.

    Returns: ``entity_master | fact | dimension | bridge | unknown``.
    Priority order:
      1. entity_master — small row count, id+name columns, a category/type
         column OR referenced by FKs from other tables.
      2. fact        — large row count, a temporal column + measure columns.
      3. bridge      — 2+ FK columns, no measure columns (junction table).
      4. dimension   — moderate row count with id+name columns.
      5. unknown     — fallback.
    """
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
        columns.append({
            "column_name": c.get("name") or c.get("column_name") or "",
            "ordinal": c.get("ordinal", i),
            "data_type": c.get("type") or c.get("data_type") or "",
            "is_nullable": bool(c.get("nullable", c.get("is_nullable", True))),
            "is_primary_key": bool(c.get("pk", c.get("primary_key", False))),
            "default_value": c.get("default", c.get("default_value")),
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
_MAX_SAMPLE_COLS = 2
_SAMPLE_LIMIT = 40
_SAMPLE_VALUE_LEN = 20


def _is_string_type(data_type: str) -> bool:
    dt = (data_type or "").lower()
    return any(k in dt for k in ("char", "text", "string", "enum"))


def _samples_text(columns: list[dict]) -> str:
    """Compact "Values: col: v1, v2; ..." suffix for embedded table docs."""
    parts = []
    for c in columns:
        samples = c.get("value_samples")
        if samples:
            parts.append(f"{c.get('column_name')}: {', '.join(samples[:_SAMPLE_LIMIT])}")
    return ("\nValues: " + "; ".join(parts)) if parts else ""


def _sample_column_values(conn: Any, table: str, columns: list[dict]) -> None:
    """Attach ``value_samples`` (distinct values) to name-like string columns.

    These samples bridge the vocabulary gap between cryptic DDL names and
    how users phrase questions; they feed the description prompt and the
    embedded doc text only (never persisted, never shown raw in prompts).
    """
    candidates = [
        c
        for c in columns
        if _is_string_type(c.get("data_type", ""))
        and _SAMPLEABLE_RE.search(c.get("column_name", ""))
    ]
    for c in candidates[:_MAX_SAMPLE_COLS]:
        col = c.get("column_name", "")
        if not col:
            continue
        try:
            rows = conn.execute(
                f"SELECT DISTINCT {_maybe_quote(conn, col)} "
                f"FROM {_maybe_quote(conn, table)} LIMIT {_SAMPLE_LIMIT}",
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
                f"SELECT DISTINCT {_maybe_quote(conn, col)} "
                f"FROM {_maybe_quote(conn, table)} LIMIT {limit}",
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
    """Best-effort quoting — connector usually has quote_ident or we skip."""
    return name


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


def _build_description_prompt(batch: list[dict]) -> list[str]:
    parts: list[str] = []
    for t in batch:
        cols = "\n".join(
            f"  - {c['column_name']} ({c['data_type']}{', PK' if c['is_primary_key'] else ''}{', nullable' if c['is_nullable'] else ''})"
            + (
                f" values: {', '.join(c['value_samples'][:12])}"
                if c.get("value_samples")
                else ""
            )
            for c in t["columns"]
        )
        sample = ""
        if t.get("row_count"):
            sample = f"\n  行数: {t['row_count']:,}"
        parts.append(f"表名: {t['table_name']}\n  类型: {t['table_type']}{sample}\n  字段:\n{cols}")
    return parts


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
            for c in t["columns"]:
                cd = col_map.get(c["column_name"], {})
                c["description_zh"] = cd.get("description_zh", "")
                c["description_en"] = cd.get("description_en", "")


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
        if existing:
            meta = existing
            meta.table_type = t.get("table_type", "TABLE")
            meta.row_count = t.get("row_count")
            meta.description_zh = t.get("description_zh")
            meta.description_en = t.get("description_en")
            meta.coverage_json = t.get("coverage_json")
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
            )
            db.add(meta)
        db.flush()
        existing_table_ids[t["table_name"]] = meta.id

        # Upsert columns
        for c in t.get("columns", []):
            existing_col = (
                db.query(KBColumnMeta)
                .filter(
                    KBColumnMeta.table_meta_id == meta.id,
                    KBColumnMeta.column_name == c["column_name"],
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
                col.description_zh = c.get("description_zh", "")
                col.description_en = c.get("description_en", "")
            else:
                col = KBColumnMeta(
                    id=str(_uuid.uuid4()),
                    table_meta_id=meta.id,
                    column_name=c["column_name"],
                    ordinal=c.get("ordinal", 0),
                    data_type=c.get("data_type", ""),
                    is_nullable=c.get("is_nullable", True),
                    is_primary_key=c.get("is_primary_key", False),
                    default_value=c.get("default_value"),
                    description_zh=c.get("description_zh", ""),
                    description_en=c.get("description_en", ""),
                )
                db.add(col)
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
            existing.description = json.dumps(e.get("evidence", {}), ensure_ascii=False)
            added += 1
        else:
            db.add(KBTableRelation(
                id=str(uuid.uuid4()),
                kb_id=kb_id,
                source_table_meta_id=src_id,
                target_table_meta_id=tgt_id,
                relation_type=e["kind"],
                source_columns=e["source_columns"],
                target_columns=e["target_columns"],
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
        text = (
            f"[TABLE] {t['table_name']}\n"
            f"{t.get('description_zh', '')}\n"
            f"{t.get('description_en', '')}\n"
            f"Type: {t.get('table_type', 'TABLE')}  "
            f"Rows: {t.get('row_count', '?')}"
            
        )
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
            text = (
                f"[COLUMN] {t['table_name']}.{c['column_name']} [{c['data_type']}]\n"
                f"{c.get('description_zh', '')}\n"
                f"{c.get('description_en', '')}"
            )
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
