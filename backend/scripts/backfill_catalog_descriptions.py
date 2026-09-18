#!/usr/bin/env python3
"""Repair an already-indexed KB catalog: leaked column labels + blank descriptions.

Two defects in the persisted catalog, both user visible:

1. ``kb_column_meta.column_name`` holds ``tax_price(含税单价)`` for 210 columns
   (the ``lz_v_*_data`` views). Two consequences: the DDL handed to the model is
   unqueryable, and the description pass can never match the column it just
   described (the model answers ``tax_price``, the catalog row says
   ``tax_price(含税单价)``) so those columns stayed blank across every index run.
   ``--normalize-names`` moves the parenthetical into the description and leaves
   the identifier as the name. The same split is now enforced in
   ``catalog_indexer._persist_catalog``, so a re-index cannot reintroduce it.

2. 1361 of 2678 columns had NO description (live ``aipdp_data_warehouse_prod``).
   With no recorded meaning the model picks between look-alike columns on name
   alone — ``FCLOSESTATUS`` vs ``FDOCUMENTSTATUS`` on ``erp_t_crm_contract``
   resolved to a different column on two consecutive runs. The backfill
   re-asks per table for the columns that came back empty.

Usage (inside the backend container — see zhanlu-backend-test-pitfalls for why
``/app`` and not ``backend/scripts/``):

    docker cp backend/scripts/backfill_catalog_descriptions.py \
        zhanlu-copy-backend:/tmp/backfill_catalog_descriptions.py
    docker exec -w /app zhanlu-copy-backend python /tmp/backfill_catalog_descriptions.py \
        --kb aipdp_data_warehouse_prod --normalize-names --limit 60

``--dry-run`` reports what would change without calling the model or writing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "/app")  # /tmp probes otherwise get sys.path[0]=/tmp

from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_base import KnowledgeBase  # noqa: E402
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta  # noqa: E402
from app.services.knowledge_graph import catalog_indexer as ci  # noqa: E402


def _normalize_names(db, kb_id: str) -> tuple[int, int]:
    """Rename `col(label)` rows → `col` with the label in the description."""
    rows = (
        db.query(KBColumnMeta)
        .join(KBTableMeta, KBTableMeta.id == KBColumnMeta.table_meta_id)
        .filter(KBTableMeta.kb_id == kb_id, KBColumnMeta.column_name.like("%(%"))
        .all()
    )
    renamed = skipped = 0
    for row in rows:
        name, label = ci._split_column_label(row.column_name)
        if not label or name == row.column_name:
            continue
        clash = (
            db.query(KBColumnMeta)
            .filter(
                KBColumnMeta.table_meta_id == row.table_meta_id,
                KBColumnMeta.column_name == name,
            )
            .first()
        )
        if clash is not None:
            skipped += 1
            continue
        row.column_name = name
        if not (str(row.description_zh or "").strip() or str(row.description_en or "").strip()):
            zh, en = ci._label_description(label)
            row.description_zh, row.description_en = zh, en
        renamed += 1
    return renamed, skipped


def _load_targets(db, kb_id: str, table_like: str | None):
    """Rebuild the indexer's `tables` structure from the persisted catalog."""
    q = db.query(KBTableMeta).filter(
        KBTableMeta.kb_id == kb_id, KBTableMeta.is_deleted.is_(False)
    )
    if table_like:
        q = q.filter(KBTableMeta.table_name.like(table_like))

    tables = []
    for meta in q.all():
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.table_meta_id == meta.id,
                    KBColumnMeta.is_deleted.is_(False))
            .order_by(KBColumnMeta.ordinal)
            .all()
        )
        tables.append({
            "table_name": meta.table_name,
            "table_type": meta.table_type,
            "row_count": meta.row_count,
            "description_zh": meta.description_zh,
            "description_en": meta.description_en,
            "_meta": meta,
            "_column_rows": cols,
            "columns": [
                {
                    "column_name": c.column_name,
                    "data_type": c.data_type,
                    "is_primary_key": c.is_primary_key,
                    "is_nullable": c.is_nullable,
                    "description_zh": c.description_zh,
                    "description_en": c.description_en,
                    # The code set (e.g. ["C","D","A"]) makes the prompt's
                    # description materially better than the bare column name.
                    "value_samples": list(c.enum_values or [])[:12],
                }
                for c in cols
            ],
        })
    return tables


def _blank_snapshot(tables) -> int:
    return sum(
        len(cols) for _, cols, _ in ci._missing_description_targets(tables)
    ) + sum(1 for t in tables if _table_blank(t))


def _table_blank(t) -> bool:
    return (
        ci._is_blank_description(t.get("description_en"))
        and ci._is_blank_description(t.get("description_zh"))
    )


def _persist_filled(db, tables) -> int:
    """UPDATE only descriptions that were blank and are now filled."""
    written = 0
    for t in tables:
        for c, row in zip(t["columns"], t["_column_rows"]):
            zh = str(c.get("description_zh") or "").strip()
            en = str(c.get("description_en") or "").strip()
            if not zh and not en:
                continue
            if zh and zh != (row.description_zh or ""):
                row.description_zh = zh
                written += 1
            if en and en != (row.description_en or ""):
                row.description_en = en
                written += 1
        # Table-level descriptions must be written too — this loop used to be a
        # no-op, so the model's table descriptions were fetched and discarded
        # while the columns persisted (live: 32 tables still blank after a run
        # that reported it had filled them).
        meta = t.get("_meta")
        if meta is not None:
            for field_name in ("description_zh", "description_en"):
                val = str(t.get(field_name) or "").strip()
                if val and val != (getattr(meta, field_name) or ""):
                    setattr(meta, field_name, val)
                    written += 1
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kb", required=True, help="knowledge base NAME")
    ap.add_argument("--limit", type=int, default=60,
                    help="max tables to re-ask (0 = no cap)")
    ap.add_argument("--table-like", default=None,
                    help="SQL LIKE filter on table_name, e.g. %%contract%%")
    ap.add_argument("--normalize-names", action="store_true",
                    help="also fix `col(label)` column names (recommended)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.name == args.kb, KnowledgeBase.is_deleted.is_(False))
            .first()
        )
        if kb is None:
            print(f"KB not found: {args.kb}")
            return 2

        if args.normalize_names:
            renamed, skipped = _normalize_names(db, kb.id)
            print(f"normalized column names: {renamed} renamed, {skipped} skipped "
                  f"(clean sibling already present)")
            if not args.dry_run:
                db.commit()
                db.expire_all()

        tables = _load_targets(db, kb.id, args.table_like)
        targets = ci._missing_description_targets(tables)
        blank_cols = sum(len(cols) for _, cols, _ in targets)
        blank_tables = sum(1 for t in tables if _table_blank(t))
        print(f"kb={kb.name} tables={len(tables)} incomplete={len(targets)} "
              f"blank_columns={blank_cols} blank_table_descriptions={blank_tables}")

        for t, cols, table_blank in targets[:10]:
            print(f"  - {t['table_name']}: {len(cols)} blank column(s)"
                  f"{' + table description' if table_blank else ''}")

        if args.dry_run or not targets:
            return 0

        ci.settings.CATALOG_DESC_BACKFILL_MAX_TABLES = args.limit or len(targets)
        asyncio.run(ci._backfill_missing_descriptions(tables))

        written = _persist_filled(db, tables)
        db.commit()

        after = ci._missing_description_targets(tables)
        print(f"wrote={written} description fields; tables still incomplete={len(after)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
