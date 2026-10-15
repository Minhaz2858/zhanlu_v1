"""Per-domain value rollups (P1-4, IBM gaps G1 + G7).

"What does AI return in my procurement department?" — the coverage map
gains a value dimension by joining the org's per-agent ROI breakdown
(roi_service, incl. booked LLM cost + estimated hours saved) with the
ontology's agent↔domain bindings.

Honesty rules:
- domains with bound agents but zero activity in the window still appear
  (0s, not absence) — a gap you can see
- est_value is clearly derived from the SAME documented ROI_* assumptions
  as roi_service (manual-time equivalent, never booked revenue)
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.ontology import AgentDomainBinding
from app.services.ontology.queries import list_domains


def domain_value_rollups(db: Session, org_id: str, days: int = 30) -> dict:
    from app.services.ontology.coverage import compute_coverage
    from app.services.roi_service import roi_summary

    roi = roi_summary(db, org_id, days=days)
    by_agent = {a["agent_name"]: a for a in roi.get("by_agent", [])}
    rate = float(
        (roi.get("totals", {}).get("est_assumptions") or {}).get("hourly_rate", 40)
    )

    domains = list_domains(db, org_id)
    bindings = (
        db.query(AgentDomainBinding)
        .filter(AgentDomainBinding.org_id == org_id)
        .all()
    )
    agents_by_domain: dict[str, set[str]] = {}
    for b in bindings:
        agents_by_domain.setdefault(b.domain_id, set()).add(b.agent_name)

    cov = {c["domain_id"]: c for c in compute_coverage(db, org_id)}

    rows: list[dict] = []
    for d in domains:
        agent_names = sorted(agents_by_domain.get(d.id, set()))
        slots = [by_agent[n] for n in agent_names if n in by_agent]
        row = {
            "domain_id": d.id,
            "domain_code": d.code,
            "domain_name": d.name,
            "agents": agent_names,
            "agents_with_activity": len(slots),
            "conversations": sum(s["conversations"] for s in slots),
            "user_turns": sum(s["user_turns"] for s in slots),
            "artifacts_created": sum(s["artifacts_created"] for s in slots),
            "cost_total": round(sum(s["cost_total"] for s in slots), 4),
            "est_hours_saved": round(sum(s["est_hours_saved"] for s in slots), 2),
            "acceptance_rate": (
                # mean of known per-agent acceptance rates (None-safe)
                round(
                    sum(s["acceptance_rate"] for s in slots
                        if s.get("acceptance_rate") is not None)
                    / max(1, sum(1 for s in slots
                                 if s.get("acceptance_rate") is not None)),
                    4,
                )
                if any(s.get("acceptance_rate") is not None for s in slots)
                else None
            ),
            "readiness": (cov.get(d.id) or {}).get("readiness"),
        }
        row["est_value"] = round(row["est_hours_saved"] * rate, 2)
        rows.append(row)

    return {
        "org_id": org_id,
        "window_days": days,
        "est_assumptions": roi.get("totals", {}).get("est_assumptions"),
        "domains": sorted(rows, key=lambda r: -r["est_value"]),
    }
