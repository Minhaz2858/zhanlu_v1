import asyncio, logging, sys
logging.basicConfig(level=logging.WARNING)
from app.database import SessionLocal
from app.models.knowledge_base import KnowledgeBase
from app.services.knowledge_graph.catalog_indexer import index_kb_catalog

db = SessionLocal()
kb = db.query(KnowledgeBase).filter(KnowledgeBase.id == sys.argv[1]).first()
assert kb, "KB not found"
asyncio.get_event_loop().run_until_complete(index_kb_catalog(kb, db))
print("FINAL STATUS:", kb.catalog_status)
