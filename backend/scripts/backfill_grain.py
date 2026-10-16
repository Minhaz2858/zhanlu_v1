"""P-SV one-shot backfill: infer grain for catalog rows that predate the
Source Verification Gate (no re-index needed).

Reconstructs each table's structural dict from kb_table_meta +
kb_table_relation and applies the same heuristics the indexer uses
(_classify_table_role for name-hint roles + _infer_grain for grain).
Idempotent: rows that already carry a grain are skipped unless --force.
"""

from __future__ import annotations

import sys

from app.database import SessionLocal
from app.models.knowledge_catalog import (
    KBColumnMeta,
    KBTableMeta,
    KBTableRelation,
)
from app.services.knowledge_graph.catalog_indexer import (
    _classify_table_role,
    _infer_grain,
)


def backfill(kb_id: str | None = None, force: bool = False) -> None:
    db = SessionLocal()
    try:
        q = db.query(KBTableMeta).filter(KBTableMeta.is_deleted.is_(False))
        if kb_id:
            q = q.filter(KBTableMeta.kb_id == kb_id)
        metas = q.all()
        by_id = {m.id: m for m in metas}

        # FK targets per source table (from persisted relations).
        fk_map: dict[str, list[str]] = {}
        if by_id:
            rels = (
                db.query(KBTableRelation)
                .filter(
                    KBTableRelation.source_table_meta_id.in_(list(by_id)),
                    KBTableRelation.relation_type == "FK",
                )
                .all()
            )
            for r in rels:
                tgt = by_id.get(r.target_table_meta_id)
                if tgt:
                    fk_map.setdefault(r.source_table_meta_id, []).append(
                        tgt.table_name
                    )

        updated = 0
        for m in metas:
            if m.grain and not force:
                continue
            cols = (
                db.query(KBColumnMeta)
                .filter(KBColumnMeta.table_meta_id == m.id)
                .order_by(KBColumnMeta.ordinal)
                .all()
            )
            table = {
                "table_name": m.table_name,
                "row_count": m.row_count or 0,
                "columns": [
                    {
                        "column_name": c.column_name,
                        "data_type": c.data_type,
                        "is_primary_key": c.is_primary_key,
                    }
                    for c in cols
                ],
                "foreign_keys": [
                    {"ref_table": t} for t in fk_map.get(m.id, [])
                ],
            }
            # Refresh the role with the extended taxonomy (name hints), then
            # infer the grain from that role.
            role = _classify_table_role(table, 0)
            if role != "unknown":
                m.table_role = role
            table["table_role"] = m.table_role
            m.grain = _infer_grain(table)
            updated += 1
        db.commit()
        print(f"backfill_grain: {updated}/{len(metas)} rows updated")
    finally:
        db.close()


if __name__ == "__main__":
    kb = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "--force" else None
    backfill(kb, force="--force" in sys.argv)
