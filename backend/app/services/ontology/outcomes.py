"""Outcome measurement service (EBOM Workstream B) — governed, honest.

Chain: expected (declared) → measured (governed delta) → verified (human).

Every number flows through the governed MetricDefinition compute path.
The LLM may EXPLAIN an outcome elsewhere; it can never calculate or
certify one here. Attribution honesty: a period-over-period delta MAY
include non-action effects — attribution is the reviewer's judgment at
verification time, never automatic.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.outcome import OutcomeMeasurement

logger = logging.getLogger(__name__)

CONFOUNDER_NOTE = (
    "realized impact is the governed period-over-period delta; it MAY include "
    "non-action effects — attribution to the decision is the reviewer's "
    "judgment at verification time, never automatic"
)


def _serialize(o: OutcomeMeasurement) -> dict:
    return {
        "id": o.id, "org_id": o.org_id,
        "approval_id": o.approval_id, "action_id": o.action_id,
        "escalation_id": o.escalation_id, "draft_id": o.draft_id,
        "metric_definition_id": o.metric_definition_id,
        "metric_name": o.metric_name,
        "direction": o.direction, "expected_impact": o.expected_impact,
        "baseline_value": o.baseline_value, "observed_value": o.observed_value,
        "baseline_period": o.baseline_period,
        "observed_period": o.observed_period, "unit": o.unit,
        "realized_impact": o.realized_impact,
        "measurement_status": o.measurement_status,
        "measurement_note": o.measurement_note,
        "verification_status": o.verification_status,
        "verified_by": o.verified_by, "verified_at": o.verified_at.isoformat() if o.verified_at else None,
        "verification_note": o.verification_note,
        "evidence_refs": o.evidence_refs,
        "confounder_note": CONFOUNDER_NOTE,
    }


def record_outcome(
    db: Session, org_id: str, *, metric_ref: str, direction: str,
    baseline_period: str, observed_period: str,
    expected_impact: float | None = None,
    approval_id: str | None = None, action_id: str | None = None,
    escalation_id: str | None = None, draft_id: str | None = None,
) -> dict:
    """Declare an expected impact and snapshot the governed baseline.

    The baseline is computed through the governed metric — a declared
    baseline would let expectations masquerade as measurements."""
    from app.services.ontology.metrics import compute_metric, resolve_metric

    if direction not in ("increase", "decrease"):
        return {"success": False, "reason": "direction must be increase|decrease"}
    if baseline_period == observed_period:
        return {"success": False,
                "reason": "baseline and observed periods must differ"}

    metric, reason = resolve_metric(db, org_id, metric_ref)
    if metric is None:
        return {"success": False, "unavailable": True, "reason": reason}
    time_col = metric.time_column
    if not time_col:
        return {"success": False, "unavailable": True,
                "reason": "metric has no time dimension — outcome measurement "
                          "requires period comparison"}

    baseline = compute_metric(db, org_id, metric_ref,
                              filter_overrides={time_col: baseline_period})
    if not baseline.get("success") or baseline.get("value") is None:
        return {"success": False, "unavailable": True,
                "reason": (f"baseline period {baseline_period!r} is not "
                           f"computable: {baseline.get('reason') or 'no value'}")}

    # Idempotent: one measurement per decision+metric+period pair.
    q = db.query(OutcomeMeasurement).filter(
        OutcomeMeasurement.org_id == org_id,
        OutcomeMeasurement.metric_definition_id == metric.id,
        OutcomeMeasurement.baseline_period == baseline_period,
        OutcomeMeasurement.observed_period == observed_period,
        OutcomeMeasurement.is_deleted == False,  # noqa: E712
    )
    if approval_id:
        q = q.filter(OutcomeMeasurement.approval_id == approval_id)
    else:
        q = q.filter(OutcomeMeasurement.approval_id.is_(None))
    existing = q.first()
    if existing:
        return {"success": True, "outcome": _serialize(existing),
                "existed": True}

    row = OutcomeMeasurement(
        org_id=org_id,
        approval_id=approval_id, action_id=action_id,
        escalation_id=escalation_id, draft_id=draft_id,
        metric_definition_id=metric.id, metric_name=metric.name,
        direction=direction, expected_impact=expected_impact,
        baseline_value=baseline["value"],
        baseline_period=baseline_period, observed_period=observed_period,
        unit=metric.unit,
        measurement_status="pending",
        verification_status="unverified",
        evidence_refs={"baseline": baseline.get("provenance")},
    )
    db.add(row)
    db.commit()
    return {"success": True, "outcome": _serialize(row)}


def measure_outcome(db: Session, org_id: str, measurement_id: str,
                    *, measured_by: str | None = None) -> dict:
    """Recompute the governed metric for the observed period and derive the
    realized impact. Deterministic; frozen once verified."""
    from app.services.ontology.metrics import compute_metric

    row = db.query(OutcomeMeasurement).filter(
        OutcomeMeasurement.id == measurement_id,
        OutcomeMeasurement.org_id == org_id,
        OutcomeMeasurement.is_deleted == False,  # noqa: E712
    ).first()
    if not row:
        return {"success": False, "reason": "outcome measurement not found"}
    if row.verification_status == "verified":
        return {"success": False,
                "reason": "outcome is verified — measurement is frozen"}

    observed = compute_metric(db, org_id, row.metric_definition_id,
                              filter_overrides={
                                  _time_col(db, org_id, row.metric_definition_id):
                                      row.observed_period})
    if not observed.get("success") or observed.get("value") is None:
        row.measurement_status = "failed"
        row.measurement_note = (f"observed period {row.observed_period!r} not "
                                f"computable: {observed.get('reason') or 'no value'}")
        db.commit()
        return {"success": False, "unavailable": True,
                "reason": row.measurement_note, "outcome": _serialize(row)}

    base_v = row.baseline_value
    obs_v = observed["value"]
    row.observed_value = obs_v
    # increase → observed − baseline; decrease → baseline − observed.
    row.realized_impact = ((obs_v - base_v) if row.direction == "increase"
                           else (base_v - obs_v))
    row.measurement_status = "measured"
    row.measurement_note = CONFOUNDER_NOTE
    row.evidence_refs = {
        **(row.evidence_refs or {}),
        "observed": observed.get("provenance"),
        "metric_provenance": observed.get("provenance"),
        "execution_trace": observed.get("execution_trace"),
        "periods": {"baseline": row.baseline_period,
                    "observed": row.observed_period},
        "decision_refs": {k: getattr(row, k) for k in
                          ("approval_id", "action_id", "escalation_id", "draft_id")
                          if getattr(row, k)},
        "measured_by": measured_by,
    }
    db.commit()
    return {"success": True, "outcome": _serialize(row)}


def verify_outcome(db: Session, org_id: str, measurement_id: str, *,
                   verified_by: str | None, decision: str | None,
                   evidence_refs: dict | None = None) -> dict:
    """Authorized human certification. Requires a completed measurement."""
    row = db.query(OutcomeMeasurement).filter(
        OutcomeMeasurement.id == measurement_id,
        OutcomeMeasurement.org_id == org_id,
        OutcomeMeasurement.is_deleted == False,  # noqa: E712
    ).first()
    if not row:
        return {"success": False, "reason": "outcome measurement not found"}
    if row.measurement_status != "measured":
        return {"success": False,
                "reason": (f"outcome is {row.measurement_status!r} — verify "
                           "requires a completed measurement")}

    row.verification_status = "verified"
    row.verified_by = verified_by
    row.verified_at = datetime.now(timezone.utc)
    row.verification_note = decision
    if evidence_refs:
        row.evidence_refs = {**(row.evidence_refs or {}), "reviewer": evidence_refs}
    db.add(AuditEvent(
        org_id=org_id, event_type="outcome_verified", event_source="ontology",
        actor_type="user", actor_id=verified_by, resource_type="outcome_measurement",
        resource_id=row.id, outcome="success",
        detail_json={"decision": (decision or "")[:300],
                     "realized_impact": row.realized_impact,
                     "metric": row.metric_name},
        occurred_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return {"success": True, "outcome": _serialize(row)}


def list_outcomes(db: Session, org_id: str) -> dict:
    rows = (db.query(OutcomeMeasurement)
            .filter(OutcomeMeasurement.org_id == org_id,
                    OutcomeMeasurement.is_deleted == False)  # noqa: E712
            .order_by(OutcomeMeasurement.created_date.desc())
            .limit(200).all())
    return {"outcomes": [_serialize(o) for o in rows]}


def _time_col(db: Session, org_id: str, metric_ref: str) -> str:
    from app.services.ontology.metrics import resolve_metric

    metric, _ = resolve_metric(db, org_id, metric_ref)
    return metric.time_column if metric and metric.time_column else "period"
