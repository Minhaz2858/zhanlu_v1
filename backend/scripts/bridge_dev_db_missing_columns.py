"""One-off bridge: add missing nullable columns to the dev Postgres so the
current ORM models are readable (the dev DB alembic lineage diverged long
ago — established practice is idempotent additive SQL, never alembic).

Covers ONLY the tables the ontology-evolution reconcile path touches.
Additive only: ADD COLUMN IF NOT EXISTS, always nullable, no backfill.
"""
from sqlalchemy import text

from app.database import engine
from app.models.agent_invocation import *  # noqa
from app.models.base import Base
from app.models.identity import IdentityMatch  # noqa: F401
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.models.metric_definition import MetricDefinition
from app.models.ontology import (
    AgentDomainBinding,
    OntologyDomain,
    OntologyEvolutionState,
    OntologyLink,
    OntologyObject,
)
from app.models.ontology_revision import OntologyRevision

TABLES = {m.__tablename__: m for m in (
    KnowledgeBase, KBTableMeta, KBColumnMeta, MetricDefinition,
    OntologyDomain, OntologyObject, OntologyLink, OntologyRevision,
    OntologyEvolutionState, AgentDomainBinding,
)}


def _sql_type(col):
    t = str(col.type)
    if "JSON" in t.upper():
        return "JSON"
    for probe, sql in (("VARCHAR", "TEXT"), ("TEXT", "TEXT")):
        if probe in t.upper():
            return sql
    if "TIMESTAMP" in t.upper():
        return "TIMESTAMP"
    if "BOOLEAN" in t.upper():
        return "BOOLEAN"
    if "INTEGER" in t.upper():
        return "INTEGER"
    if "FLOAT" in t.upper() or "NUMERIC" in t.upper() or "REAL" in t.upper():
        return "DOUBLE PRECISION"
    if "DATETIME" in t.upper() or "DATE" in t.upper():
        return "TIMESTAMP"
    return "TEXT"  # safest additive fallback


with engine.connect() as conn:
    for table, model in TABLES.items():
        existing = {
            r[0] for r in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :t"), {"t": table}).all()
        }
        if not existing:
            print(f"SKIP {table}: table missing entirely (not created here)")
            continue
        for name, col in model.__table__.columns.items():
            if name in existing:
                continue
            stmt = (f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "
                    f"{name} {_sql_type(col)}")
            conn.execute(text(stmt))
            print(f"ADDED {table}.{name} {_sql_type(col)}")
    conn.commit()
print("bridge done")
