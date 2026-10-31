"""Phase 0B — enterprise resource deletion & orphan cleanup.

Covers: catalog soft-deletion, agent binding/JSON cleanup (unrelated ids
survive), ontology orphaning without object deletion, EBPM inventory
update with evidence survival, cross-org rejection, idempotency, missing
optional records, and the AuditEvent trail.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.audit_event import AuditEvent
from app.models.enterprise_process import EnterpriseResource, ProcessEvidence
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta, KBTableRelation
from app.models.ontology import OntologyLink, OntologyObject
from app.services.enterprise.cleanup import handle_resource_deleted

ORG = "org-acme"
KB = "kb-del-1"


@pytest.fixture()
def db():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(eng, tables=[
        KnowledgeBase.__table__, KBTableMeta.__table__,
        KBColumnMeta.__table__, KBTableRelation.__table__,
        AgentApp.__table__, AgentDataBinding.__table__,
        OntologyObject.__table__, OntologyLink.__table__,
        EnterpriseResource.__table__, ProcessEvidence.__table__,
        AuditEvent.__table__,
    ])
    s = sessionmaker(bind=eng)()
    yield s
    s.close()
    eng.dispose()


def _seed(db):
    db.add(KnowledgeBase(id=KB, name="Sales DB", org_id=ORG,
                         source_kind="database"))
    tm = KBTableMeta(id="tm-1", kb_id=KB, org_id=ORG, table_name="orders")
    db.add(tm)
    db.add(KBColumnMeta(id="cm-1", org_id=ORG, table_meta_id="tm-1",
                        column_name="order_no"))
    db.add(KBTableRelation(id="kr-1", kb_id=KB, org_id=ORG,
                           source_table_meta_id="tm-1",
                           target_table_meta_id="tm-1", relation_type="fk"))
    # agent bound to the KB (JSON) + an unrelated KB id that must survive
    db.add(AgentApp(id="app-1", name="GA", org_id=ORG,
                    knowledge_bases=[KB, "kb-keep"]))
    # an OTHER-ORG agent bound to the same id string must NOT be touched
    db.add(AgentApp(id="app-evil", name="Evil GA", org_id="org-evil",
                    knowledge_bases=[KB]))
    db.add(AgentDataBinding(id="bd-1", org_id=ORG, agent_app_id="app-1",
                            datasource_id=KB))
    # ontology: one object backed by the removed KB, one unrelated
    db.add(OntologyObject(id="obj-1", org_id=ORG, domain_id="dom-1",
                          name="Order", backing_kind="table",
                          backing_ref=f"kb:{KB}:orders",
                          evidence_status="verified"))
    db.add(OntologyObject(id="obj-2", org_id=ORG, domain_id="dom-1",
                          name="Keep", backing_kind="table",
                          backing_ref="kb:kb-other:customers"))
    db.add(OntologyLink(id="lnk-1", org_id=ORG, from_object_id="obj-1",
                        to_object_id="obj-2", link_type="relates_to"))
    # EBPM inventory + evidence that must survive
    db.add(EnterpriseResource(id="er-1", org_id=ORG, resource_kind="datasource",
                              ref_id=KB, name="Sales DB"))
    db.add(ProcessEvidence(id="pe-1", org_id=ORG, process_id="proc-1",
                           evidence_kind="table_catalog",
                           source_ref=f"{KB}:orders", quote="table orders"))
    db.commit()


def test_kb_deletion_cleans_catalog_and_is_audited(db):
    _seed(db)
    out = handle_resource_deleted(db, resource_type="knowledge_base",
                                  resource_id=KB, org_id=ORG, actor_id="u1")
    assert out["catalog_tables"] == 1
    assert out["catalog_columns"] == 1
    assert out["catalog_relations"] == 1
    assert db.query(KBTableMeta).filter(
        KBTableMeta.id == "tm-1", KBTableMeta.is_deleted == False).count() == 0
    assert db.query(KBTableMeta).filter(KBTableMeta.id == "tm-1").count() == 1  # soft
    audit = db.query(AuditEvent).filter(
        AuditEvent.event_type == "resource_deleted").one()
    assert audit.resource_id == KB and audit.org_id == ORG
    assert audit.detail_json["counts"]["catalog_tables"] == 1


def test_agent_bindings_cleaned_unrelated_kept(db):
    _seed(db)
    handle_resource_deleted(db, resource_type="knowledge_base",
                            resource_id=KB, org_id=ORG)
    app = db.query(AgentApp).filter(AgentApp.id == "app-1").one()
    assert app.knowledge_bases == ["kb-keep"]  # deleted id gone, keep survives
    evil = db.query(AgentApp).filter(AgentApp.id == "app-evil").one()
    assert evil.knowledge_bases == [KB]  # other org untouched
    binding = db.query(AgentDataBinding).filter(
        AgentDataBinding.id == "bd-1").one()
    assert binding.is_deleted is True


def test_ontology_backing_orphaned_object_and_links_survive(db):
    _seed(db)
    out = handle_resource_deleted(db, resource_type="knowledge_base",
                                  resource_id=KB, org_id=ORG)
    assert out["ontology_objects_orphaned"] == 1
    obj = db.query(OntologyObject).filter(OntologyObject.id == "obj-1").one()
    assert obj.binding_status == "orphaned"
    assert obj.backing_ref is None  # can no longer resolve to live data
    assert obj.evidence_status == "verified"  # human decision preserved
    assert obj.provenance_json["orphaned_backings"][0]["ref"] == f"kb:{KB}:orders"
    keep = db.query(OntologyObject).filter(OntologyObject.id == "obj-2").one()
    assert keep.backing_ref == "kb:kb-other:customers"  # unrelated untouched
    assert db.query(OntologyLink).filter(OntologyLink.id == "lnk-1").count() == 1


def test_ebpm_inventory_deleted_evidence_survives(db):
    _seed(db)
    out = handle_resource_deleted(db, resource_type="knowledge_base",
                                  resource_id=KB, org_id=ORG)
    assert out["ebpm_inventory"] == 1
    er = db.query(EnterpriseResource).filter(EnterpriseResource.id == "er-1").one()
    assert er.is_deleted is True
    assert er.status_json["deleted"] is True
    ev = db.query(ProcessEvidence).filter(ProcessEvidence.id == "pe-1").one()
    assert ev.is_deleted is False and ev.quote  # history preserved


def test_cross_organization_cleanup_rejected(db):
    _seed(db)
    with pytest.raises(PermissionError):
        handle_resource_deleted(db, resource_type="knowledge_base",
                                resource_id=KB, org_id="org-evil")
    with pytest.raises(LookupError):
        handle_resource_deleted(db, resource_type="knowledge_base",
                                resource_id="nope", org_id=ORG)
    # nothing was written by the failed calls
    assert db.query(AuditEvent).filter(
        AuditEvent.event_type == "resource_deleted").count() == 0


def test_repeated_cleanup_is_idempotent(db):
    _seed(db)
    first = handle_resource_deleted(db, resource_type="knowledge_base",
                                    resource_id=KB, org_id=ORG)
    # cleanup never deletes the resource row itself (the caller's delete
    # path did) — a second cleanup runs and must be a no-op.
    second = handle_resource_deleted(db, resource_type="knowledge_base",
                                     resource_id=KB, org_id=ORG)
    assert second["catalog_tables"] == 0
    assert second["catalog_columns"] == 0
    assert second["agent_data_bindings"] == 0
    assert second["agent_apps_updated"] == 0
    assert second["ontology_objects_orphaned"] == 0
    assert second["ebpm_inventory"] == 0
    obj = db.query(OntologyObject).filter(OntologyObject.id == "obj-1").one()
    assert obj.backing_ref is None and obj.binding_status == "orphaned"
    assert first["catalog_tables"] == 1  # original counts unchanged


def test_missing_optional_records_do_not_crash(db):
    # KB with no catalog rows, no bindings, no inventory, no ontology
    db.add(KnowledgeBase(id="kb-empty", name="Empty", org_id=ORG,
                         source_kind="database"))
    db.commit()
    out = handle_resource_deleted(db, resource_type="knowledge_base",
                                  resource_id="kb-empty", org_id=ORG)
    assert out["catalog_tables"] == 0 and out["ebpm_inventory"] == 0
    assert out["ontology_objects_orphaned"] == 0


def test_mcp_and_automation_types_supported(db):
    AutomationTask = pytest.importorskip("app.models.automation_task").AutomationTask
    McpServer = pytest.importorskip("app.models.mcp_server").McpServer
    # create the missing tables in the live engine
    from app.database import Base as _B
    _B.metadata.create_all(db.bind, tables=[
        AutomationTask.__table__, McpServer.__table__])

    db.add(McpServer(id="mcp-1", name="tools", org_id=ORG,
                     server_url="http://x", transport="http"))
    db.add(AutomationTask(id="at-1", name="nightly", org_id=ORG,
                          type="data_sync"))
    db.add(EnterpriseResource(id="er-2", org_id=ORG, resource_kind="automation",
                              ref_id="at-1", name="nightly"))
    db.commit()
    out = handle_resource_deleted(db, resource_type="mcp_server",
                                  resource_id="mcp-1", org_id=ORG)
    assert out.get("catalog_tables", 0) == 0  # no catalog for MCP
    out2 = handle_resource_deleted(db, resource_type="automation_task",
                                   resource_id="at-1", org_id=ORG)
    assert out2["ebpm_inventory"] == 1
    with pytest.raises(ValueError):
        handle_resource_deleted(db, resource_type="widget",
                                resource_id="x", org_id=ORG)
