"""Why do the lz_v_*_data views keep coming back with blank column descriptions?"""
import asyncio
import json

from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.services.knowledge_graph import catalog_indexer as ci
from app.services.llm_service import call_llm

TABLE = "lz_v_间戊二烯_data"

db = SessionLocal()
kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == "aipdp_data_warehouse_prod").first()
meta = db.query(KBTableMeta).filter(
    KBTableMeta.kb_id == kb.id, KBTableMeta.table_name == TABLE).first()
print("table_meta:", meta.id, "| table desc:", repr(meta.description_zh), repr(meta.description_en))
cols = db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id == meta.id).order_by(KBColumnMeta.ordinal).all()
print("columns:", [(c.column_name, c.data_type, repr(c.description_zh)) for c in cols])

blank = [c for c in cols if not (c.description_zh or "").strip() and not (c.description_en or "").strip()]
print("blank count:", len(blank))

proxy_cols = [{
    "column_name": c.column_name, "data_type": c.data_type,
    "is_primary_key": c.is_primary_key, "is_nullable": c.is_nullable,
    "description_zh": c.description_zh, "description_en": c.description_en,
} for c in blank]
prompt = ci._build_description_prompt([{"table_name": TABLE, "table_type": "VIEW",
                                         "row_count": None, "columns": proxy_cols}])
print("\n--- PROMPT ---")
print(prompt[0])

result = asyncio.run(call_llm(
    messages=[{"role": "system", "content": ci._TABLE_DESCRIPTION_SYSTEM},
              {"role": "user", "content": prompt[0]}],
    temperature=ci.LLM_TEMP, response_json_schema=ci._JSON_SCHEMA,
    task_type="catalog_description_backfill"))
print("\n--- RAW RESULT (truncated) ---")
print(json.dumps(result, ensure_ascii=False)[:1500])
db.close()
