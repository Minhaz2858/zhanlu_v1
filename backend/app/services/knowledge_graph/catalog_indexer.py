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

import json
import logging
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBTableMeta, KBColumnMeta, KBTableRelation
from app.services.db.connector_factory import get_connector

logger = logging.getLogger(__name__)

# ── limits ────────────────────────────────────────────────────────────────
MAX_TABLES = 300          # safety cap — real warehouse has ~139
ROW_COUNT_TIMEOUT_S = 5   # skip row counts if the DB is slow
LLM_BATCH_SIZE = 8        # tables per description call
LLM_TEMP = 0.1            # low temp for structured output

# ── LLM batch prompt ──────────────────────────────────────────────────────

_TABLE_DESCRIPTION_SYSTEM = (
    "你是一个数据分析专家。下面是数据库中一批表的结构信息。"
    "请为每张表和每个字段输出中文和英文的业务含义说明。"
    "说明应该简洁、可被其他大模型理解，用于后续的自然语言查询 (NL2SQL) 场景。"
    "如果字段含义无法从名称判断，标注为 '未知'。"
    "输出严格的 JSON 数组，每张表一个对象。"
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
            await _generate_descriptions(tables)
            await asyncio.to_thread(_persist_catalog, db, kb.id, tables)
            await asyncio.to_thread(_persist_relations, db, kb.id, tables)
        await asyncio.to_thread(_embed_catalog, kb.id, tables)

        _set_status(db, kb, "ready")
        elapsed = time.monotonic() - t0
        logger.info(
            "catalog_indexer: kb=%s done — %d tables in %.1fs",
            kb.id, len(tables), elapsed,
        )
    except Exception:
        logger.exception("catalog_indexer: kb=%s failed", kb.id)
        _set_status(db, kb, "error")


# ── internals ──────────────────────────────────────────────────────────────

def _set_status(db: Session, kb: KnowledgeBase, status: str) -> None:
    kb.catalog_status = status
    db.commit()


def _introspect_tables_sync(kb: KnowledgeBase, db: Session) -> list[dict]:
    """Sync wrapper: introspect full schema via connector (runs in thread)."""
    with get_connector(kb) as conn:
        table_names = _safe_list_tables(conn)
        if len(table_names) > MAX_TABLES:
            logger.warning(
                "catalog_indexer: kb=%s has %d tables, capping at %d",
                kb.id, len(table_names), MAX_TABLES,
            )
            table_names = table_names[:MAX_TABLES]

        tables: list[dict] = []
        for name in table_names:
            try:
                desc = conn.describe_table(name)
                row_count = _safe_row_count(conn, name)
                fks = _safe_foreign_keys(conn, name)
                schema_name, table_type, columns = _normalize_desc(desc)
                tables.append({
                    "schema_name": schema_name,
                    "table_name": name,
                    "table_type": table_type,
                    "row_count": row_count,
                    "columns": columns,
                    "foreign_keys": fks,
                })
            except Exception:
                logger.exception(
                    "catalog_indexer: kb=%s failed to introspect table '%s' — skipping", kb.id, name,
                )
    return tables


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
        rows = conn.execute(f"SELECT COUNT(*) FROM {_maybe_quote(conn, table)}", timeout=ROW_COUNT_TIMEOUT_S)
        if rows:
            return int(rows[0][0])
    except Exception:
        pass
    return None


def _safe_foreign_keys(conn: Any, table: str) -> list[dict]:
    try:
        if hasattr(conn, "get_foreign_keys"):
            return conn.get_foreign_keys(table) or []
    except Exception:
        pass
    return []


def _maybe_quote(conn: Any, name: str) -> str:
    """Best-effort quoting — connector usually has quote_ident or we skip."""
    return name


# ── LLM descriptions ───────────────────────────────────────────────────────

async def _generate_descriptions(tables: list[dict]) -> None:
    """Batch LLM description pass — mutates tables in-place with descriptions."""
    if not tables:
        return
    from app.services.llm_service import call_llm

    batch_count = 0
    for i in range(0, len(tables), LLM_BATCH_SIZE):
        batch = tables[i : i + LLM_BATCH_SIZE]
        batch_count += 1
        prompt_parts = _build_description_prompt(batch)
        user_prompt = "\n\n".join(prompt_parts)

        try:
            result = await call_llm(
                prompt=user_prompt,
                messages=[{"role": "system", "content": _TABLE_DESCRIPTION_SYSTEM}],
                temperature=LLM_TEMP,
                response_json_schema=_JSON_SCHEMA,
                task_type="catalog_description",
            )
            _apply_descriptions(batch, result)
        except Exception:
            logger.exception(
                "catalog_indexer: LLM batch %d/%d failed — leaving descriptions empty",
                batch_count, (len(tables) + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE,
            )


def _build_description_prompt(batch: list[dict]) -> list[str]:
    parts: list[str] = []
    for t in batch:
        cols = "\n".join(
            f"  - {c['column_name']} ({c['data_type']}{', PK' if c['is_primary_key'] else ''}{', nullable' if c['is_nullable'] else ''})"
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
        if existing:
            meta = existing
            meta.table_type = t.get("table_type", "TABLE")
            meta.row_count = t.get("row_count")
            meta.description_zh = t.get("description_zh")
            meta.description_en = t.get("description_en")
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
