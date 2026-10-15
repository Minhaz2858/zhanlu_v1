"""Ontology coverage computation — the admin Coverage Map's data (P0-2).

Honest scoring: an object counts as live ONLY with a real backing_ref;
an action counts as executable ONLY with a registry tool that exists;
agents count only when actually bound. Unwired = visible gap, never fake
readiness (strategy anti-goal: the ontology must never be decoration).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ontology import CoverageState
from app.services.ontology.queries import (
    list_actions,
    list_domains,
    list_objects,
)


def compute_coverage(db: Session, org_id: str) -> list[dict]:
    from app.services.tool_registry import registry
    from app.models.ontology import AgentDomainBinding

    out: list[dict] = []
    for domain in list_domains(db, org_id):
        objects = list_objects(db, org_id, domain.id)
        actions = list_actions(db, org_id, domain.id)
        bindings = (
            db.query(AgentDomainBinding)
            .filter(
                AgentDomainBinding.org_id == org_id,
                AgentDomainBinding.domain_id == domain.id,
            )
            .count()
        )

        objects_live = sum(
            1 for o in objects
            if o.is_active and o.backing_ref and o.backing_kind != "manual"
        )
        actions_executable = sum(
            1 for a in actions
            if a.is_active and a.tool_name
            and registry.get_entry(a.tool_name) is not None
        )
        data_connected = 1 if objects_live else 0

        row = (
            db.query(CoverageState)
            .filter(
                CoverageState.org_id == org_id,
                CoverageState.domain_id == domain.id,
            )
            .first()
        )
        if not row:
            row = CoverageState(org_id=org_id, domain_id=domain.id)
            db.add(row)
        row.data_connected = data_connected
        row.objects_live = objects_live
        row.actions_executable = actions_executable
        row.agents_deployed = 1 if bindings else 0

        out.append({
            "domain_id": domain.id, "code": domain.code, "name": domain.name,
            "sort_order": domain.sort_order,
            "objects_total": len(objects), "objects_live": objects_live,
            "actions_total": len(actions),
            "actions_executable": actions_executable,
            "agents_deployed": 1 if bindings else 0,
            "data_connected": bool(data_connected),
            "readiness": (
                "live" if (objects_live and actions_executable)
                else ("wired" if objects_live
                      else ("drafted" if objects else "empty"))
            ),
        })
    db.commit()
    return out
