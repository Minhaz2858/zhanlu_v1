"""Unified evidence taxonomy for the enterprise ontology (PRD P1, G3).

Canonical statuses — the SINGLE source of truth (frontend
`src/lib/evidenceStatuses.js` mirrors this list):

- verified:   directly supported AND human-authorized (explicit bind by
              an org admin after 2026-10-30; never assigned automatically)
- computed:   produced by deterministic registered logic (metric backing,
              activated in PRD P5)
- inferred:   machine-proposed with recorded evidence (discovery autobind)
- proposed:   potential element, awaiting evidence or confirmation
- unavailable: supporting evidence vanished (schema drift; PRD P6)

Non-negotiables (PRD):
- discovery/automation can NEVER set `verified`;
- `confidence` is recorded only when a real score exists (LLM verdicts),
  never fabricated for human-confirmed or hint-matched rows;
- provenance_json is REASSIGNED, never mutated in place (SQLAlchemy JSON
  mutation gotcha — the discovery-v1 shipped bug).
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.models.ontology import OntologyAction, OntologyLink, OntologyObject

EVIDENCE_STATUSES = (
    "verified", "computed", "inferred", "proposed", "unavailable",
)

STATUS_VERIFIED = "verified"
STATUS_COMPUTED = "computed"
STATUS_INFERRED = "inferred"
STATUS_PROPOSED = "proposed"
STATUS_UNAVAILABLE = "unavailable"


def is_valid_status(s: object) -> bool:
    return s in EVIDENCE_STATUSES


def normalize_status(s: str) -> str:
    v = (s or "").strip().lower()
    if not is_valid_status(v):
        raise ValueError(f"unknown evidence status: {s!r}")
    return v


# ── Backfill rules (migration 103; pure, unit-tested) ───────────────────

def legacy_backfill_status(backing_kind: str | None, backing_ref: str | None) -> str:
    """Legacy objects: a real binding is EVIDENCE (inferred), never
    VERIFIED — no human authorization was recorded when it was made."""
    if backing_kind and backing_kind != "manual" and (backing_ref or "").strip():
        return STATUS_INFERRED
    return STATUS_PROPOSED


def legacy_backfill_status_action(tool_name: str | None) -> str:
    if (tool_name or "").strip():
        return STATUS_INFERRED
    return STATUS_PROPOSED


# ── Stamping helpers (the ONLY writers of evidence fields) ──────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _merge_provenance(row, patch: dict) -> None:
    """REASSIGN the JSON column (in-place mutation is silently lost)."""
    base = dict(row.provenance_json) if row.provenance_json else {}
    row.provenance_json = {**base, **patch}


def stamp_human_bind(obj: OntologyObject, *, bound_by: str | None) -> None:
    """Explicit human authorization via PATCH /bind — the ONLY path to
    `verified`. Human confirmation is not a score: confidence stays None."""
    obj.evidence_status = STATUS_VERIFIED
    obj.confidence = None
    _merge_provenance(obj, {
        "verified_by": bound_by,
        "verified_at": _now_iso(),
        "backing_kind": obj.backing_kind,
        "backing_ref": obj.backing_ref,
    })


def stamp_unbind(obj: OntologyObject) -> None:
    """Unbinding (manual/unbound) returns the object to a proposal.

    Supersedes stale evidence: confidence is cleared (a proposal must not
    display a score — 'proposed · 90%' reads as fabricated evidence) and
    prior binding/verification provenance keys are nulled so an
    unbind→re-discovery cycle cannot carry a misleading audit trail."""
    obj.evidence_status = STATUS_PROPOSED
    obj.confidence = None
    _merge_provenance(obj, {
        "unbound_at": _now_iso(),
        "verified_by": None,
        "verified_at": None,
        "method": None,
        "confidence": None,
        "kb_id": None,
        "table": None,
    })


def stamp_discovery(
    obj: OntologyObject, *, method: str, confidence: float | None,
    kb_id: str, table: str, status: str = STATUS_INFERRED,
) -> None:
    """Machine binding — always `inferred` (or weaker); refusal to ever
    mark `verified` from automation (raises, so a caller can't drift)."""
    if status != STATUS_INFERRED:
        raise ValueError(
            "discovery stamps are always 'inferred' — verified requires "
            "stamp_human_bind"
        )
    obj.evidence_status = STATUS_INFERRED
    obj.confidence = confidence
    _merge_provenance(obj, {
        "method": method,
        "confidence": confidence,
        "kb_id": kb_id,
        "table": table,
        "stamped_at": _now_iso(),
    })


def stamp_human_document_approval(obj: OntologyObject, *, reviewer_id: str,
                                  proposal, content_hash: str | None = None,
                                  decision: str = "approved") -> None:
    """P4 D2 — human authorization of a DOCUMENT-derived element. The ONLY
    document-approval entry point to `verified` (the sole writer of
    document provenance entries, D7):

    - requires an authenticated human reviewer identity — a machine caller
      (missing/blank reviewer) is refused exactly like the discovery guard
      above: automation can never reach `verified`;
    - appends ONE provenance entry {source_kb_id, doc_version_id,
      content_hash, location, quote, reviewer, decision, audience_snapshot}
      — per-entry authorization (D7); two documents on one object keep
      independent entries;
    - preserves existing provenance (append semantics) and reassigned the
      JSON column (P1 mutation gotcha);
    - human confirmation is not a score: confidence stays None."""
    if not (reviewer_id or "").strip():
        raise ValueError(
            "document approval requires an authenticated human reviewer — "
            "machine-generated evidence can never set verified"
        )
    obj.evidence_status = STATUS_VERIFIED
    obj.confidence = None
    base = dict(obj.provenance_json) if obj.provenance_json else {}
    entries = list(base.get("document_provenance") or [])
    entries.append({
        "source_kb_id": proposal.source_kb_id,
        "doc_version_id": proposal.doc_version_id,
        "content_hash": content_hash,
        "location": dict(proposal.location_json or {}),
        "quote": proposal.quote,
        "reviewer": reviewer_id,
        "decision": decision,
        "audience_snapshot": dict(proposal.audience_json or {}),
        "approved_at": _now_iso(),
    })
    # Per-entry ONLY (T8 review F4): no object-level reviewer/approval keys —
    # everything rides inside the filtered entries so hiding an entry hides
    # ALL traces of that document's approval.
    obj.provenance_json = {**base, "document_provenance": entries}


def stamp_action_binding(action: OntologyAction, *, bound_by: str | None) -> None:
    """Human wires an action to a registry tool → inferred (the binding is
    machine-validated but is not itself data evidence); unbound → proposed."""
    if (action.tool_name or "").strip():
        action.evidence_status = STATUS_INFERRED
    else:
        action.evidence_status = STATUS_PROPOSED
    _merge_provenance(action, {"bound_by": bound_by, "bound_at": _now_iso()})


def stamp_link(link: OntologyLink, *, by: str | None,
               status: str = STATUS_INFERRED) -> None:
    """Links created/confirmed by humans or automation land here; legacy
    links backfilled as proposed. `verified` requires an explicit human
    confirmation flow (PRD P3 identity-review queue will call this —
    deliberately kept ahead of that phase; not yet wired)."""
    if not is_valid_status(status) or status == STATUS_VERIFIED:
        status = STATUS_INFERRED
    link.evidence_status = status
    _merge_provenance(link, {"by": by, "stamped_at": _now_iso()})
