"""Stage 2B-1 one-shot backfill: populate schema classification columns for
catalog rows that predate the classifier (no re-index needed).

Mirrors scripts/backfill_grain.py: reconstructs each table's structural dict
from kb_table_meta + kb_column_meta + kb_table_relation, then applies the
SAME pure classifiers the indexer will use at index time
(app/services/db/schema_classification.py). Idempotent: rows that already
carry a classification are skipped unless --force.

Human overrides are NOT touched and ALWAYS win at read time (the P-SV facts
loader prefers project_catalog_overlay.schema_class_override /
dataset_scope_override — wired in 2B-3). The backfill deliberately still
writes the auto columns so the human verdict and the machine verdict stay
visible side by side.

Usage:
    python scripts/backfill_schema_classification.py [--kb-id KB] [--force]

NO migration is executed here; alembic revision 105_schema_classification
must be applied first (separate approval gate).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Runnable directly: python scripts/backfill_schema_classification.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import SessionLocal  # noqa: E402


def backfill(kb_id: str | None = None, force: bool = False) -> dict:
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta, KBTableRelation
    from app.services.db.schema_classification import (
        classify_system_business,
        dataset_scope_value,
        infer_scope,
    )

    db = SessionLocal()
    try:
        q = db.query(KBTableMeta).filter(KBTableMeta.is_deleted.is_(False))
        if kb_id:
            q = q.filter(KBTableMeta.kb_id == kb_id)
        metas = q.all()

        engines = {
            kb.id: str(kb.db_type or "")
            for kb in db.query(KnowledgeBase)
            .filter(KnowledgeBase.id.in_(list({m.kb_id for m in metas})))
            .all()
        } if metas else {}

        degree: dict[str, int] = {}
        if metas:
            for r in (
                db.query(KBTableRelation)
                .filter(KBTableRelation.is_deleted.is_(False))
                .all()
            ):
                for side in (r.source_table_meta_id, r.target_table_meta_id):
                    if side in {m.id for m in metas}:
                        degree[side] = degree.get(side, 0) + 1

        classified = scoped = skipped = 0
        by_kb: dict[str, list] = {}
        for m in metas:
            by_kb.setdefault(m.kb_id, []).append(m)

        for kb, metas_in_kb in by_kb.items():
            table_dicts: list[dict] = []
            column_stats: dict[str, dict] = {}
            for m in metas_in_kb:
                cols = (
                    db.query(KBColumnMeta)
                    .filter(KBColumnMeta.table_meta_id == m.id)
                    .order_by(KBColumnMeta.ordinal)
                    .all()
                )
                table_dicts.append({
                    "table_name": m.table_name,
                    "schema_name": m.schema_name or "",
                    "engine": engines.get(kb, ""),
                    "row_count": m.row_count or 0,
                    "columns": [
                        {"column_name": c.column_name, "data_type": c.data_type}
                        for c in cols
                    ],
                })
                col_stats = {
                    c.column_name: {"values": c.enum_values}
                    for c in cols
                    if isinstance(c.enum_values, list) and c.enum_values
                }
                if col_stats:
                    column_stats[m.table_name] = col_stats

            for m in metas_in_kb:
                if m.schema_class and not force:
                    skipped += 1
                    continue
                td = next(t for t in table_dicts if t["table_name"] == m.table_name)
                verdict = classify_system_business(
                    td,
                    relation_degree=degree.get(m.id, 0),
                    domain_support=bool(m.domain_name),
                    table_role=m.table_role or "unknown",
                )
                m.schema_class = verdict.schema_class
                m.schema_class_confidence = verdict.confidence
                m.schema_class_signals_json = {"signals": list(verdict.signals)}
                classified += 1

            scope_targets = [m for m in metas_in_kb if not m.dataset_scope or force]
            if not scope_targets:
                continue
            verdicts = infer_scope(table_dicts, column_stats=column_stats)
            for m in scope_targets:
                v = verdicts.get(m.table_name)
                if v is None:
                    continue
                # Review point 1 (2026-10-30): THREE distinct states —
                # "partition:<key>" only when the key is CONFIRMED (P2);
                # a key-less family is "partition_family", never a
                # partition:<key> that a scope gate could read as verified.
                scope_value = dataset_scope_value(v)
                if scope_value is None:
                    if m.dataset_scope and not force:
                        continue
                    m.dataset_scope = None  # honest unknown
                else:
                    m.dataset_scope = scope_value
                m.dataset_scope_confidence = v.confidence
                m.dataset_scope_signals_json = {
                    "signals": list(v.signals),
                    # Structured scope fields (2B-2): the question-scope
                    # resolver needs the OBSERVED partition value per table.
                    "level": v.level,
                    "partition_key": v.partition_key,
                    "partition_value": v.partition_value,
                }
                scoped += 1

        db.commit()
        return {"classified": classified, "scoped": scoped, "skipped": skipped}
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kb-id", default=None, help="Limit to one knowledge base")
    parser.add_argument("--force", action="store_true", help="Re-run over existing values")
    args = parser.parse_args()
    result = backfill(kb_id=args.kb_id, force=args.force)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
