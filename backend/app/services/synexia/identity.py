"""FSM tenant-isolation identity resolution (IC-1, 2026-10-31).

The governing principle (reviewer, FSM stage authorization): tenant
identity must be established BEFORE retrieving configuration,
credentials, memories, or execution capabilities.

This module is the SINGLE agent-lookup contract for the FSM pipeline.
Every former name-only or unscoped-PK ``AgentApp`` lookup (context
assembler, evaluation profiles, tool contexts) must go through
:func:`resolve_agent_in_org`:

  * PK path: ``AgentApp.id == agent_id AND AgentApp.org_id == org_id``
  * name path: ``AgentApp.name == agent_name AND AgentApp.org_id == org_id``
  * no org context → ``None`` (fail closed)
  * NO cross-org fallback and NO ``default-org`` fallback. Pre-tenancy
    rows carry ``default-org`` and are matched directly by default-org
    callers (strict equality is also the compatibility path — same
    contract proven at the DB level in P0-1).

Memory keys (D-2 decision): isolation is keyed by the resolved agent
PK; legacy rows keyed by the bare agent name remain reachable ONLY for
``default-org`` callers (the legacy bridge). Never for other orgs.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session


def resolve_agent_in_org(
    db: Session,
    *,
    agent_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    org_id: Optional[str] = None,
) -> Optional[object]:
    """Resolve the calling org's OWN AgentApp row (strict org equality).

    Returns ``None`` when the org context is missing, when neither id
    nor name is given, or when no own-org row exists — the caller must
    fail closed (no configuration, no memories, no capabilities).
    """
    if not org_id:
        return None
    try:
        from app.models.agent_app import AgentApp
    except Exception:  # pragma: no cover — models unavailable
        return None

    query = db.query(AgentApp).filter(AgentApp.is_deleted == False)  # noqa: E712
    if agent_id:
        return query.filter(
            AgentApp.id == agent_id,
            AgentApp.org_id == org_id,
        ).first()
    if agent_name:
        return query.filter(
            AgentApp.name == agent_name,
            AgentApp.org_id == org_id,
        ).first()
    return None


def memory_read_keys(
    *,
    agent_row: Optional[object],
    agent_name: Optional[str],
    org_id: Optional[str],
) -> list[str]:
    """Memory lookup keys for reads, org-scoped (D-2 bridge).

    Primary key: the resolved agent's PK. Legacy bridge: rows keyed by
    the bare agent name are ALSO readable — but only for ``default-org``
    callers, whose pre-tenancy memories were written under that key.
    Non-default orgs never see bare-name keys: historical compatibility
    must not become a second tenant-isolation bypass.
    """
    keys: list[str] = []
    if agent_row is not None and getattr(agent_row, "id", None):
        keys.append(agent_row.id)
    if org_id == "default-org" and agent_name:
        keys.append(agent_name)
    return keys


def memory_write_key(
    *,
    agent_row: Optional[object],
    agent_name: Optional[str],
    org_id: Optional[str],
) -> Optional[str]:
    """Memory key for WRITES, org-safe (D-2).

    Resolved agent PK when the org owns the agent row. Legacy bare-name
    key ONLY for default-org callers without a row (pre-tenancy parity).
    Any other org without an own row gets ``None`` — the caller must NOT
    write memories under a shared key (fail closed).
    """
    if agent_row is not None and getattr(agent_row, "id", None):
        return agent_row.id
    if org_id == "default-org":
        return agent_name
    return None
