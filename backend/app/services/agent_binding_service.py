"""Agent binding service — the ONLY writer of AgentDataBinding rows.

Phase 8 ADR-1: ``AgentDataBinding`` is the authoritative record of which
resources an agent may access; ``AgentApp.knowledge_bases`` (the legacy
JSON array the runtime used to read directly) becomes a synchronized
compatibility projection, rebuilt in the SAME transaction on every write
so existing runtime consumers keep working unchanged.

Fail-closed rules:
* agent must exist in the acting org          → else LookupError
* foreign-org agent or datasource             → else PermissionError
* datasource must exist, same org, not deleted → else LookupError
* zero bindings ⇒ zero bound sources (never a fallback to auto-bind here)

Revocation is a soft delete; disable keeps the row but drops it from the
projection and from bound-source resolution. Both take effect on the
agent's NEXT resolution (the runtime resolves per turn — no cache).
"""

from sqlalchemy.orm import Session

from app.models.agent_app import AgentApp
from app.models.agent_data_binding import AgentDataBinding
from app.models.knowledge_base import KnowledgeBase


def _load_agent(db: Session, org_id: str, agent_app_id: str) -> AgentApp:
    agent = (
        db.query(AgentApp)
        .filter(AgentApp.id == agent_app_id,
                AgentApp.is_deleted == False)  # noqa: E712
        .first()
    )
    if agent is None:
        raise LookupError(f"agent {agent_app_id} not found")
    if getattr(agent, "org_id", None) != org_id:
        raise PermissionError(
            f"agent {agent_app_id} belongs to another organization")
    return agent


def _load_datasource(db: Session, org_id: str, datasource_id: str) -> None:
    ds = db.get(KnowledgeBase, datasource_id)
    if ds is None or ds.is_deleted:
        raise LookupError(f"datasource {datasource_id} not found")
    if getattr(ds, "org_id", None) != org_id:
        raise PermissionError(
            f"datasource {datasource_id} belongs to another organization")


def _load_binding_scoped(db: Session, org_id: str,
                         binding_id: str) -> AgentDataBinding:
    binding = db.get(AgentDataBinding, binding_id)
    if binding is None or binding.is_deleted:
        raise LookupError(f"binding {binding_id} not found")
    agent = db.get(AgentApp, binding.agent_app_id)
    if agent is None or getattr(agent, "org_id", None) != org_id:
        raise PermissionError(
            f"binding {binding_id} belongs to another organization")
    return binding


def rebuild_projection(db: Session, agent: AgentApp) -> None:
    """AgentApp.knowledge_bases = sorted live+enabled binding datasource ids."""
    ids = sorted(
        row.datasource_id for row in db.query(AgentDataBinding)
        .filter(AgentDataBinding.agent_app_id == agent.id,
                AgentDataBinding.is_deleted == False,  # noqa: E712
                AgentDataBinding.is_enabled == True)  # noqa: E712
        .all()
    )
    agent.knowledge_bases = ids


def upsert_binding(
    db: Session, org_id: str, agent_app_id: str, datasource_id: str, *,
    actor_id: str | None = None, access_mode: str = "read_only",
    allowed_tables: list | None = None, allowed_columns: dict | None = None,
    blocked_tables: list | None = None, row_filters: dict | None = None,
    nl2sql_enabled: bool = True,
) -> AgentDataBinding:
    """Create (or idempotently update / resurrect) one agent binding."""
    agent = _load_agent(db, org_id, agent_app_id)
    _load_datasource(db, org_id, datasource_id)

    binding = (
        db.query(AgentDataBinding)
        .filter(AgentDataBinding.agent_app_id == agent_app_id,
                AgentDataBinding.datasource_id == datasource_id)
        .first()
    )
    if binding is None:
        binding = AgentDataBinding(
            agent_app_id=agent_app_id, datasource_id=datasource_id,
            created_by_id=actor_id,
        )
        db.add(binding)
    # resurrect a soft-deleted binding rather than stacking rows
    binding.is_deleted = False
    binding.is_enabled = True
    binding.access_mode = access_mode or "read_only"
    binding.allowed_tables = allowed_tables
    binding.allowed_columns = allowed_columns
    binding.blocked_tables = blocked_tables
    binding.row_filters = row_filters
    binding.nl2sql_enabled = bool(nl2sql_enabled)
    binding.created_by_id = actor_id or binding.created_by_id

    rebuild_projection(db, agent)
    db.commit()
    db.refresh(binding)
    return binding


def set_binding_enabled(db: Session, org_id: str, binding_id: str,
                        enabled: bool, *, actor_id: str | None = None) -> None:
    binding = _load_binding_scoped(db, org_id, binding_id)
    binding.is_enabled = bool(enabled)
    agent = db.get(AgentApp, binding.agent_app_id)
    rebuild_projection(db, agent)
    db.commit()


def revoke_binding(db: Session, org_id: str, binding_id: str, *,
                   actor_id: str | None = None) -> None:
    binding = _load_binding_scoped(db, org_id, binding_id)
    binding.is_deleted = True
    binding.is_enabled = False
    agent = db.get(AgentApp, binding.agent_app_id)
    rebuild_projection(db, agent)
    db.commit()


def list_bindings(db: Session, org_id: str,
                  agent_app_id: str) -> list[AgentDataBinding]:
    _load_agent(db, org_id, agent_app_id)
    return (
        db.query(AgentDataBinding)
        .filter(AgentDataBinding.agent_app_id == agent_app_id,
                AgentDataBinding.is_deleted == False)  # noqa: E712
        .order_by(AgentDataBinding.datasource_id.asc())
        .all()
    )


def get_bound_data_source_ids_via_bindings(
    db: Session, agent_app_id: str,
) -> list[str]:
    """Live + enabled binding datasource ids — the binding-authoritative
    bound set (empty when the agent has no bindings; the runtime may fall
    back to the legacy JSON projection only when NO binding rows exist)."""
    return sorted(
        row.datasource_id for row in db.query(AgentDataBinding)
        .filter(AgentDataBinding.agent_app_id == agent_app_id,
                AgentDataBinding.is_deleted == False,  # noqa: E712
                AgentDataBinding.is_enabled == True)  # noqa: E712
        .all()
    )
