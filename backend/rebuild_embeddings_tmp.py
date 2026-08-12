import sys
from app.database import SessionLocal
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.services.knowledge_graph.catalog_indexer import _embed_catalog

kb_id = sys.argv[1]
db = SessionLocal()
try:
    metas = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb_id).all()
    cols = db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id.in_([m.id for m in metas])).all()
    by_table = {}
    for c in cols:
        by_table.setdefault(c.table_meta_id, []).append(c)
    tables = []
    for m in metas:
        tables.append({
            "table_name": m.table_name, "table_type": m.table_type,
            "row_count": m.row_count,
            "description_zh": m.description_zh or "",
            "description_en": m.description_en or "",
            "_meta_id": m.id,
            "columns": [{"column_name": c.column_name, "data_type": c.data_type,
                         "description_zh": c.description_zh or "", "description_en": c.description_en or ""}
                        for c in by_table.get(m.id, [])],
        })
    _embed_catalog(kb_id, tables)
    print(f"REBUILT: {len(tables)} tables")
finally:
    db.close()
