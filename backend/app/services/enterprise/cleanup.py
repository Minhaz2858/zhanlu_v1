"""Enterprise resource deletion & orphan cleanup (Phase 0B, 2026-11-01).

Single idempotent, org-scoped, auditable cleanup invoked from the REAL
deletion paths after authorization:

- generic entity DELETE (``routers/entities.py``) for KnowledgeBase,
  McpServer, AutomationTask;
- dedicated MCP disconnect (``routers/mcp.py``).

Semantics:
- SOFT everything: catalog rows, agent bindings and EBPM inventory are
  soft-deleted/deactivated — historical metadata is preserved for audit;
- ontology objects are NEVER deleted: a backing reference to the removed
  resource is set ``binding_status='orphaned'`` and CLEARED (so it cannot
  resolve to live data) with the prior ref preserved in the object's
  provenance and the audit trail; human review decisions and
  relationships are untouched;
- ``ProcessEvidence`` is never modified — evidence is history;
- idempotent: re-running yields zero-effect counts, never corruption;
- org isolation: every query is org-filtered and the resource row itself
  must belong to ``org_id`` (mismatch → cross-org rejection);
- no Celery / event bus: synchronous, caller-commit transaction
  convention; on failure the service rolls back and re-raises — callers
  must NOT report success silently.

Known limitation (documented, not invented around): the ``Datasource``
model has no deletion endpoint today; the service supports the
``datasource`` type for programmatic/future callers only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SUPPORTED_RESOURCE_TYPES = {
    "knowledge_base", "datasource", "mcp_server", "automation_task",
}

# generic-entity name → cleanup resource type (entities.py hook)
ENTITY_TO_RESOURCE_TYPE = {
    "KnowledgeBase": "knowledge_base",
    "McpServer": "mcp_server",
    "AutomationTask": "automation_task",
}


def handle_resource_deleted(
    db: Session,
    *,
    resource_type: str,
    resource_id: str,
    org_id: str,
    actor_id: str | None = None,
) -> dict:
    """Clean up everything referencing a just-deleted enterprise resource.

    Returns a counts dict ({component: affected_rows}) plus ``warnings``.
    Raises ``LookupError`` if the resource row does not exist in
    ``org_id`` and ``PermissionError`` on a cross-org mismatch (callers
    convert to 404). Commits on success; rolls back and re-raises on
    failure.
    """
    if resource_type not in SUPPORTED_RESOURCE_TYPES:
        raise ValueError(f"unsupported resource_type: {resource_type}")

    from app.models.audit_event import AuditEvent

    counts: dict[str, int] = {}
    warnings: list[str] = []

    # --- verify the resource row belongs to this org (fail-closed) -----
    resource_row = _load_resource_row(db, resource_type, resource_id)
    if resource_row is None:
        raise LookupError(
            f"{resource_type} {resource_id} not found in org {org_id}")
    row_org = getattr(resource_row, "org_id", None)
    if row_org != org_id:
        raise PermissionError(
            f"cross-organization cleanup refused: resource org={row_org}")

    try:
        # --- A. Catalog metadata (databases KBs) -------------------------
        counts.update(_cleanup_catalog(db, resource_type, resource_id, org_id))

        # --- B. Agent bindings -------------------------------------------
        counts.update(_cleanup_agent_bindings(
            db, resource_type, resource_id, org_id))

        # --- C. Ontology backing references -------------------------------
        counts.update(_cleanup_ontology_backings(
            db, resource_type, resource_id, org_id))

        # --- D. EBPM inventory (evidence is NEVER touched) ----------------
        counts.update(_cleanup_ebpm_inventory(
            db, resource_type, resource_id, org_id))

        # --- E. Audit ------------------------------------------------------
        db.add(AuditEvent(
            org_id=org_id,
            event_type="resource_deleted",
            event_source="enterprise",
            actor_type="user",
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome="success",
            detail_json={"counts": counts, "warnings": warnings},
            occurred_at=datetime.now(timezone.utc),
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.exception(
            "cleanup failed for %s %s (org %s) — rolled back",
            resource_type, resource_id, org_id)
        raise

    result = {"resource_type": resource_type, "resource_id": resource_id,
              "org_id": org_id, **counts, "warnings": warnings}
    logger.info("resource cleanup: %s", result)
    return result


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------

def _load_resource_row(db: Session, resource_type: str, resource_id: str):
    from app.models.agent_app import AgentApp
    from app.models.automation_task import AutomationTask
    from app.models.datasource import Datasource
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import McpServer

    model = {
        "knowledge_base": KnowledgeBase,
        "datasource": Datasource,
        "mcp_server": McpServer,
        "automation_task": AutomationTask,
        "agent_app": AgentApp,
    }[resource_type]
    # Phase 7C (D0): load by id REGARDLESS of is_deleted. Production call
    # sites soft-delete the resource FIRST and run cleanup AFTER; the old
    # `is_deleted == False` filter made the load return None on every
    # production path (LookupError → swallowed) so cleanup never ran.
    # The load exists for org verification — the delete flag is irrelevant.
    return db.query(model).filter(model.id == resource_id).first()


def _cleanup_catalog(db: Session, resource_type: str, resource_id: str,
                     org_id: str) -> dict:
    """Soft-delete catalog metadata so a removed database KB can never be
    queried as a live resource. Only database KBs carry catalog rows."""
    if resource_type != "knowledge_base":
        return {}
    from app.models.knowledge_catalog import (
        KBColumnMeta, KBTableMeta, KBTableRelation,
    )

    tables = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == resource_id, KBTableMeta.org_id == org_id,
                KBTableMeta.is_deleted == False)  # noqa: E712
        .all()
    )
    table_ids = [t.id for t in tables]
    counts = {}
    for t in tables:
        t.is_deleted = True
    counts["catalog_tables"] = len(tables)

    cols = []
    if table_ids:
        cols = (
            db.query(KBColumnMeta)
            .filter(KBColumnMeta.org_id == org_id,
                    KBColumnMeta.table_meta_id.in_(table_ids),
                    KBColumnMeta.is_deleted == False)  # noqa: E712
            .all()
        )
    for c in cols:
        c.is_deleted = True
    counts["catalog_columns"] = len(cols)

    rels = (
        db.query(KBTableRelation)
        .filter(KBTableRelation.kb_id == resource_id,
                KBTableRelation.org_id == org_id,
                KBTableRelation.is_deleted == False)  # noqa: E712
        .all()
    )
    for r in rels:
        r.is_deleted = True
    counts["catalog_relations"] = len(rels)
    return counts


def _cleanup_agent_bindings(db: Session, resource_type: str, resource_id: str,
                            org_id: str) -> dict:
    """Remove stale references: AgentDataBinding rows + KB ids inside the
    AgentApp.knowledge_bases JSON arrays (org-scoped; JSON edited
    surgically so unrelated ids survive)."""
    from app.models.agent_app import AgentApp
    from app.models.agent_data_binding import AgentDataBinding

    counts: dict[str, int] = {}

    bindings = (
        db.query(AgentDataBinding)
        .filter(AgentDataBinding.datasource_id == resource_id,
                AgentDataBinding.org_id == org_id,
                AgentDataBinding.is_deleted == False)  # noqa: E712
        .all()
    )
    for b in bindings:
        b.is_deleted = True
    counts["agent_data_bindings"] = len(bindings)

    apps = (
        db.query(AgentApp)
        .filter(AgentApp.org_id == org_id,
                AgentApp.is_deleted == False)  # noqa: E712
        .all()
    )
    updated = 0
    for app in apps:
        kb_list = app.knowledge_bases
        if not isinstance(kb_list, list) or resource_id not in kb_list:
            continue
        app.knowledge_bases = [k for k in kb_list if k != resource_id]
        updated += 1
    counts["agent_apps_updated"] = updated
    return counts


def _cleanup_ontology_backings(db: Session, resource_type: str,
                               resource_id: str, org_id: str) -> dict:
    """Orphan ontology objects backed by the removed resource.

    Only ``backing_kind='table'`` refs (``kb:<kb_id>:<table>``) point at
    knowledge bases. The object, its links and its review state survive;
    the backing reference is CLEARED (cannot resolve to live data) and
    the prior ref is preserved in the object provenance + audit trail.
    """
    if resource_type != "knowledge_base":
        return {}
    from app.models.ontology import OntologyObject
    now = datetime.now(timezone.utc).isoformat()
    prefix = f"kb:{resource_id}:"

    objects = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == org_id,
                OntologyObject.backing_kind == "table",
                OntologyObject.backing_ref.isnot(None),
                OntologyObject.is_deleted == False)  # noqa: E712
        .all()
    )
    orphaned = 0
    for obj in objects:
        ref = obj.backing_ref or ""
        if not ref.startswith(prefix):
            continue
        prov = dict(obj.provenance_json or {})
        orphan_history = list(prov.get("orphaned_backings") or [])
        orphan_history.append({"ref": ref, "resource_id": resource_id,
                               "at": now})
        prov["orphaned_backings"] = orphan_history
        obj.provenance_json = prov
        obj.binding_status = "orphaned"
        obj.backing_ref = None
        orphaned += 1
    return {"ontology_objects_orphaned": orphaned}


def _cleanup_ebpm_inventory(db: Session, resource_type: str,
                            resource_id: str, org_id: str) -> dict:
    """Mark matching EnterpriseResource inventory entries deleted. The
    kind→ref mapping is loose on purpose (a KB is a datasource when
    source_kind='database', a document otherwise) — match by ref_id."""
    from app.models.enterprise_process import EnterpriseResource

    rows = (
        db.query(EnterpriseResource)
        .filter(EnterpriseResource.org_id == org_id,
                EnterpriseResource.ref_id == resource_id,
                EnterpriseResource.is_deleted == False)  # noqa: E712
        .all()
    )
    now = datetime.now(timezone.utc).isoformat()
    for r in rows:
        r.is_deleted = True
        status = dict(r.status_json or {})
        status["deleted"] = True
        status["deleted_at"] = now
        status["deleted_resource_type"] = resource_type
        r.status_json = status
    return {"ebpm_inventory": len(rows)}
