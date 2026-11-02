"""Phase 7C T4 — production cleanup ordering regression.

Production call sites soft-delete the resource FIRST, then invoke
``handle_resource_deleted``. The service's org-verification load must
therefore work on an ALREADY-SOFT-DELETED row — the Phase 0B load filtered
``is_deleted == False`` and silently no-op'd every production cleanup
(LookupError swallowed by the router as "nothing to clean").
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.audit_event import AuditEvent
from app.models.enterprise_process import EnterpriseResource
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import (
    KBColumnMeta, KBTableMeta, KBTableRelation)
from app.models.ontology import OntologyObject
from app.services.enterprise.cleanup import handle_resource_deleted

ORG = "org-acme"
ORG2 = "org-other"
KB = "kb-del"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[
        KnowledgeBase.__table__, KBTableMeta.__table__,
        KBColumnMeta.__table__, KBTableRelation.__table__,
        AgentApp.__table__, AgentDataBinding.__table__,
        OntologyObject.__table__, EnterpriseResource.__table__,
        AuditEvent.__table__,
    ])
    s = sessionmaker(bind=eng)()
    # Production order parity seed: a live KB with every artifact the
    # cleanup touches, then SOFT-DELETE the KB before cleanup runs.
    s.add(KnowledgeBase(id=KB, name="Sales DB", org_id=ORG,
                        source_kind="database"))
    s.add(KBTableMeta(id="tm-1", kb_id=KB, org_id=ORG, table_name="orders"))
    s.add(KBColumnMeta(id="cm-1", org_id=ORG, table_meta_id="tm-1",
                       column_name="order_no"))
    s.add(KBTableRelation(id="kr-1", kb_id=KB, org_id=ORG,
                          source_table_meta_id="tm-1",
                          target_table_meta_id="tm-1", relation_type="fk"))
    s.add(AgentApp(id="app-1", name="GA", org_id=ORG,
                   knowledge_bases=[KB, "kb-keep"]))
    s.add(AgentDataBinding(id="bd-1", org_id=ORG, agent_app_id="app-1",
                           datasource_id=KB))
    s.add(OntologyObject(id="obj-1", org_id=ORG, domain_id="dom-1",
                         name="Order", backing_kind="table",
                         backing_ref=f"kb:{KB}:orders",
                         evidence_status="verified"))
    s.add(EnterpriseResource(id="er-1", org_id=ORG, resource_kind="datasource",
                             ref_id=KB, name="Sales DB"))
    s.commit()
    # --- the production order: soft-delete, THEN cleanup -----------------
    kb = s.get(KnowledgeBase, KB)
    kb.is_deleted = True
    s.commit()
    yield s
    s.close()
    eng.dispose()


def test_cleanup_runs_after_soft_delete(db):
    out = handle_resource_deleted(db, resource_type="knowledge_base",
                                  resource_id=KB, org_id=ORG, actor_id="u1")
    assert out["catalog_tables"] == 1
    assert out["catalog_columns"] == 1
    assert out["agent_data_bindings"] == 1
    assert out["agent_apps_updated"] == 1
    assert out["ontology_objects_orphaned"] == 1
    assert out["ebpm_inventory"] == 1
    # artifacts are actually cleaned (production wiring was dead before 7C)
    assert db.query(KBTableMeta).filter_by(id="tm-1").first().is_deleted is True
    assert db.query(AgentDataBinding).filter_by(id="bd-1").first() \
        .is_deleted is True
    obj = db.query(OntologyObject).filter_by(id="obj-1").first()
    assert obj.binding_status == "orphaned" and obj.backing_ref is None
    agent = db.get(AgentApp, "app-1")
    assert KB not in (agent.knowledge_bases or [])
    assert "kb-keep" in (agent.knowledge_bases or [])


def test_cleanup_after_soft_delete_is_idempotent(db):
    handle_resource_deleted(db, resource_type="knowledge_base",
                            resource_id=KB, org_id=ORG, actor_id="u1")
    out2 = handle_resource_deleted(db, resource_type="knowledge_base",
                                   resource_id=KB, org_id=ORG, actor_id="u1")
    assert out2["catalog_tables"] == 0
    assert out2["agent_data_bindings"] == 0


def test_cleanup_after_soft_delete_still_cross_org_safe(db):
    with pytest.raises(PermissionError):
        handle_resource_deleted(db, resource_type="knowledge_base",
                                resource_id=KB, org_id=ORG2, actor_id="u1")
