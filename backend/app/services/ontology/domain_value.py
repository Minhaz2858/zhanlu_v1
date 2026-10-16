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

    # ── P3-7: service KPIs (CSAT proxy + outcome split) per domain ──
    # Source: ExperienceEntry user_feedback rows for the domain's agents
    # (agent_app_id → AgentApp.name matched against bindings). HONEST:
    # domains with no feedback get None, not a made-up score.
    from app.models.agent_app import AgentApp
    from app.models.experience_entry import ExperienceEntry

    app_ids_by_name: dict[str, str] = {}
    if agents_by_domain:
        apps = (
            db.query(AgentApp)
            .filter(AgentApp.org_id == org_id,
                    AgentApp.is_deleted == False)  # noqa: E712
            .all()
        )
        app_ids_by_name = {a.name: a.id for a in apps if a.name}
    feedback_by_agent: dict[str, dict] = {}
    if app_ids_by_name:
        from datetime import datetime, timedelta, timezone

        since = datetime.now(timezone.utc) - timedelta(days=days)
        fb = (
            db.query(ExperienceEntry)
            .filter(
                ExperienceEntry.org_id == org_id,
                ExperienceEntry.entry_type == "user_feedback",
                ExperienceEntry.created_date >= since,
                ExperienceEntry.agent_app_id.isnot(None),
            )
            .all()
        )
        id_to_name = {v: k for k, v in app_ids_by_name.items()}
        for e in fb:
            name = id_to_name.get(e.agent_app_id)
            if not name:
                continue
            slot = feedback_by_agent.setdefault(
                name, {"n": 0, "ratings": [], "positive": 0})
            slot["n"] += 1
            if e.user_rating is not None:
                slot["ratings"].append(float(e.user_rating))
            if (e.outcome or "").lower() == "positive":
                slot["positive"] += 1

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
        # P3-7 service KPIs — CSAT proxy (user_rating 1-5 → 0-100) and
        # positive-outcome ratio across the domain's agents' feedback.
        fb_slots = [feedback_by_agent[n] for n in agent_names
                    if n in feedback_by_agent]
        ratings = [r for s in fb_slots for r in s["ratings"]]
        n_fb = sum(s["n"] for s in fb_slots)
        row["feedback_count"] = n_fb
        row["csat"] = (
            round(sum(ratings) / len(ratings) / 5.0 * 100, 1)
            if ratings else None
        )
        row["positive_ratio"] = (
            round(sum(s["positive"] for s in fb_slots) / n_fb, 4)
            if n_fb else None
        )
        rows.append(row)

    return {
        "org_id": org_id,
        "window_days": days,
        "est_assumptions": roi.get("totals", {}).get("est_assumptions"),
        "domains": sorted(rows, key=lambda r: -r["est_value"]),
    }
