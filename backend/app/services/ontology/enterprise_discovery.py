"""Enterprise discovery — inventory the org's authorized resources.

Deterministic first: the inventory is assembled ONLY from rows the org
already owns (KBs, catalogued schemas, ontology elements, automations,
agents, actions). No LLM, no guessing, no cross-org reads. LLM-assisted
process proposal lives in `propose_processes` and is explicitly marked
inferred/proposed with evidence quotes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def inventory_resources(db: Session, org_id: str) -> dict:
    """Deterministic inventory of the org's authorized enterprise resources.

    Assembles what the org ALREADY has — never assumes a source, never
    queries outside the org. Every section can be honestly empty.
    """
    from app.models.agent_run import AgentRun
    from app.models.automation_task import AutomationTask
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta, KBTableRelation
    from app.models.ontology import (
        OntologyAction, OntologyDomain, OntologyLink, OntologyObject,
    )
    from app.models.metric_definition import MetricDefinition

    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.org_id == org_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    datasources, documents = [], []
    for kb in kbs:
        entry = {
            "id": kb.id, "name": kb.name,
            "source_kind": kb.source_kind or "database",
            "db_type": getattr(kb, "db_type", None),
            "status": kb.status,
            "catalog_status": getattr(kb, "catalog_status", None),
            "indexing_status": getattr(kb, "indexing_status", None),
        }
        if (kb.source_kind or "") == "database":
            tmeta = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb.id, KBTableMeta.is_deleted == False)  # noqa: E712
                .count()
            )
            table_ids = [
                t.id for t in db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb.id,
                        KBTableMeta.is_deleted == False)  # noqa: E712
                .all()
            ]
            cmeta = (
                db.query(KBColumnMeta)
                .filter(KBColumnMeta.table_meta_id.in_(table_ids or [""]),
                        KBColumnMeta.is_deleted == False)  # noqa: E712
                .count()
            ) if table_ids else 0
            rels = (
                db.query(KBTableRelation)
                .filter(KBTableRelation.kb_id == kb.id, KBTableRelation.is_deleted == False)  # noqa: E712
                .count()
            )
            entry.update({"tables": tmeta, "columns": cmeta, "relations": rels})
            datasources.append(entry)
        else:
            entry.update({
                "file_type": kb.file_type,
                "chunk_count": kb.chunk_count or 0,
                "indexing_status": kb.indexing_status,
            })
            documents.append(entry)

    domains = db.query(OntologyDomain).filter(
        OntologyDomain.org_id == org_id, OntologyDomain.is_deleted == False).count()  # noqa: E712
    objects = db.query(OntologyObject).filter(
        OntologyObject.org_id == org_id, OntologyObject.is_deleted == False).count()  # noqa: E712
    links = db.query(OntologyLink).filter(
        OntologyLink.org_id == org_id, OntologyLink.is_deleted == False).count()  # noqa: E712
    actions = db.query(OntologyAction).filter(
        OntologyAction.org_id == org_id, OntologyAction.is_deleted == False).all()
    metrics = db.query(MetricDefinition).filter(
        MetricDefinition.org_id == org_id, MetricDefinition.is_deleted == False).count()  # noqa: E712
    automations = db.query(AutomationTask).filter(
        AutomationTask.org_id == org_id, AutomationTask.is_deleted == False).all()
    agents = db.query(AgentRun).filter(
        AgentRun.org_id == org_id).distinct(AgentRun.agent_name).count() \
        if hasattr(AgentRun, "org_id") else 0

    return {
        "org_id": org_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "datasources": datasources,
        "documents": documents,
        "ontology": {
            "domains": domains, "objects": objects, "links": links,
            "metrics": metrics,
            "actions": [
                {"id": a.id, "name": a.name, "tool_name": a.tool_name,
                 "risk_level": a.risk_level}
                for a in actions
            ],
        },
        "automations": [
            {"id": a.id, "name": getattr(a, "name", None) or a.id,
             "schedule_kind": getattr(a, "schedule_kind", None),
             "status": getattr(a, "status", None)}
            for a in automations
        ],
        "agents_active": agents,
        "summary": {
            "datasources": len(datasources),
            "documents": len(documents),
            "ontology_objects": objects,
            "actions": len(actions),
            "automations": len(automations),
        },
    }


def refresh_inventory(db: Session, org_id: str) -> dict:
    """Persist the inventory as EnterpriseResource rows (upsert by ref)."""
    from app.models.enterprise_process import EnterpriseResource

    inv = inventory_resources(db, org_id)
    now = datetime.now(timezone.utc).isoformat()

    rows: list[tuple[str, str, str, dict, str]] = []
    for ds in inv["datasources"]:
        rows.append(("datasource", ds["id"], ds["name"], ds, "verified"))
    for doc in inv["documents"]:
        rows.append(("document", doc["id"], doc["name"], doc, "verified"))
    for act in inv["ontology"]["actions"]:
        rows.append(("action_interface", act["id"], act["name"], act, "verified"))
    for auto in inv["automations"]:
        rows.append(("automation", auto["id"], auto["name"], auto, "verified"))

    existing = {
        (r.resource_kind, r.ref_id): r
        for r in db.query(EnterpriseResource)
        .filter(EnterpriseResource.org_id == org_id,
                EnterpriseResource.is_deleted == False)  # noqa: E712
        .all()
    }
    for kind, ref, name, status, ev in rows:
        row = existing.get((kind, ref))
        if row:
            row.name = name
            row.status_json = status
            row.discovered_at = now
        else:
            db.add(EnterpriseResource(
                org_id=org_id, resource_kind=kind, ref_id=ref,
                name=name, status_json=status,
                evidence_status=ev, discovered_at=now))
    db.commit()
    return inv
