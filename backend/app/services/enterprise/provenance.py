"""Canonical enterprise provenance — ONE reusable evidence envelope.

Phase 3C of the enterprise-intelligence roadmap: unifies NL2SQL ontology
provenance, SQL execution lineage, document evidence and business-process
evidence behind a single serialization contract (``prov-v1``).

Design rules (all verified by tests/test_canonical_provenance.py):
- States are DISTINCT: ``retrieved`` (fetched into context) ≠ ``influenced``
  (actually affected planning/ranking) ≠ ``supports_answer`` (tri-state:
  None = undetermined — retrieval NEVER auto-promotes to supporting).
- Converters are PURE functions over caller-supplied data — they never
  query the database, so they cannot leak another organization's rows;
  ``org_id`` is stamped verbatim from the caller's already-authorized org.
- Nothing is fabricated: absent optional data is OMITTED (no invented
  confidence values, no empty-string ids, no synthetic statuses).
- Statuses pass through verbatim — proposed stays proposed; the contract
  NEVER promotes evidence status.
- Missing provenance stays missing: converters on empty/None input return
  ``[]``; the NL2SQL log writer only adds the canonical section when
  provenance actually exists (historical rows are byte-identical).

Adoption (Phase 3C): the NL2SQL telemetry row
(``nl2sql_query_logs.lineage_json``) now carries ``provenance`` (canonical)
alongside the raw Phase 3B ``ontology`` section and the legacy SQL lineage
keys. Document/process converters are adopted by their own systems as a
follow-up (they are pure and side-effect free).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

PROVENANCE_VERSION = "prov-v1"

#: Evidence source types. Closed set — unknown types are rejected so the
#: envelope stays auditable (extend deliberately, never permissively).
SOURCE_TYPES = (
    "ontology_object",
    "ontology_relationship",
    "sql_lineage",
    "document",
    "process_evidence",
    "metric",
    "terminology",
    "rule",
    "execution",
)

#: Record types a canonical ``execution_ref`` may point at. Kept in lockstep
#: with ``app.services.enterprise.observations._RECORD_TYPES`` (Phase 5A).
EXECUTION_RECORD_TYPES = ("nl2sql_query", "agent_run")

_RECORD_PREFIX = {"nl2sql_query": "nl2sql_query", "agent_run": "agent_run"}


def execution_ref(record_type: str | None, record_id: str | None) -> str | None:
    """Canonical execution-reference string: ``"<record_type>:<record_id>"``.

    Missing inputs stay missing (``None``); unknown record types are
    REJECTED (the closed set keeps references auditable). The reference
    identifies a REAL persisted record — verification against the
    authorized organization is :func:`resolve_execution_ref`.
    """
    if not record_id:
        return None
    if record_type not in EXECUTION_RECORD_TYPES:
        raise ValueError(f"unknown execution record type: {record_type!r}")
    return f"{record_type}:{record_id}"


def parse_execution_ref(ref: str | None) -> tuple[str, str] | None:
    """Inverse of :func:`execution_ref`. Malformed/foreign/missing refs
    parse to ``None`` — never guessed into a record type."""
    if not ref or not isinstance(ref, str) or ":" not in ref:
        return None
    rtype, _, rid = ref.partition(":")
    if rtype not in EXECUTION_RECORD_TYPES or not rid:
        return None
    return (rtype, rid)


def resolve_execution_ref(db, org_id: str, ref: str | None):
    """Verify an execution reference against the authorized organization.

    Returns ``(record_type, record_id)`` when the referenced record exists
    AND belongs to ``org_id``; ``None`` when the record is missing or
    foreign (fail closed); ``ValueError`` on a malformed reference.
    Reuses the observation loader so org semantics stay identical to
    ``record_observation`` (AgentRun org lives in ``caller_context``).
    """
    parsed = parse_execution_ref(ref)
    if parsed is None:
        raise ValueError(f"malformed execution_ref: {ref!r}")
    from app.services.enterprise.observations import _load_record

    record = _load_record(db, org_id, parsed[0], parsed[1])
    return parsed if record is not None else None


def entries_with_verified_execution_refs(db, org_id: str, entries: list):
    """Filter canonical entries: entries whose ``execution_ref`` does NOT
    verify against ``org_id`` are OMITTED (contract: invalid or cross-org
    references are dropped, never laundered). Entries without an
    ``execution_ref`` pass through untouched."""
    out = []
    for e in entries or []:
        ref = e.get("execution_ref") if isinstance(e, dict) else None
        if ref is None:
            out.append(e)
            continue
        # org ownership: an entry stamped for a DIFFERENT org is omitted
        # even when its ref would technically resolve (never launder a
        # foreign-org claim through the filter).
        e_org = e.get("org_id")
        if e_org is not None and e_org != org_id:
            continue
        try:
            if resolve_execution_ref(db, org_id, ref) is not None:
                out.append(e)
        except ValueError:
            continue  # malformed → omitted
    return out


def from_observed_execution(
    record_type: str, record_id: str, *, org_id: str | None = None,
    process_id: str | None = None, activity_seq: int | None = None,
    match_confidence: float | None = None, match_method: str | None = None,
) -> dict:
    """Observed execution → canonical entry (``source_type="execution"``).

    STATE HONESTY: an observed execution is NOT retrieved context and
    NEVER supports the answer — ``retrieved=False, influenced=False``,
    ``supports_answer`` omitted (tri-state undetermined). The observed
    process linkage passes through verbatim (proposed evidence stays
    proposed — this contract never promotes it). ``match_confidence``
    passes through only when the matcher actually produced one.
    """
    if match_confidence is not None and not (
            isinstance(match_confidence, (int, float))
            and 0.0 <= float(match_confidence) <= 1.0):
        raise ValueError(
            f"match_confidence must be within [0, 1], got {match_confidence!r}")
    return canonical_entry(
        source_type="execution",
        source_id=record_id,
        org_id=org_id,
        process_id=process_id,
        activity_seq=activity_seq,
        kind="observed_execution",
        retrieval_method=match_method,
        confidence=match_confidence,
        retrieved=False, influenced=False, supports_answer=None,
        execution_ref=execution_ref(record_type, record_id))

_ENTRY_KEYS = (
    "source_type", "source_id", "org_id", "resource_type", "resource_id",
    "evidence_id", "source_ref", "target_ref", "ontology_object_id",
    "ontology_relationship_id", "process_id", "activity_seq", "kind",
    "retrieval_method", "evidence_status", "confidence", "retrieved",
    "influenced", "supports_answer", "execution_ref",
)


def canonical_entry(
    *,
    source_type: str,
    source_id: str | None = None,
    org_id: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    evidence_id: str | None = None,
    source_ref: str | None = None,
    target_ref: str | None = None,
    ontology_object_id: str | None = None,
    ontology_relationship_id: str | None = None,
    process_id: str | None = None,
    activity_seq: int | None = None,
    kind: str | None = None,
    retrieval_method: str | None = None,
    evidence_status: str | None = None,
    confidence: float | None = None,
    retrieved: bool = False,
    influenced: bool = False,
    supports_answer: bool | None = None,
    execution_ref: str | None = None,
) -> dict:
    """Build one canonical provenance entry.

    Optional fields whose value is ``None`` are OMITTED — "missing stays
    missing". ``supports_answer`` is tri-state: ``None`` (undetermined,
    omitted), ``True``/``False`` (determined downstream; ``True`` is
    validated by :func:`validate_canonical`).
    """
    values = {
        "source_type": source_type,
        "source_id": source_id,
        "org_id": org_id,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "evidence_id": evidence_id,
        "source_ref": source_ref,
        "target_ref": target_ref,
        "ontology_object_id": ontology_object_id,
        "ontology_relationship_id": ontology_relationship_id,
        "process_id": process_id,
        "activity_seq": activity_seq,
        "kind": kind,
        "retrieval_method": retrieval_method,
        "evidence_status": evidence_status,
        "confidence": confidence,
        "retrieved": bool(retrieved),
        "influenced": bool(influenced),
        "supports_answer": supports_answer,
        "execution_ref": execution_ref,
    }
    entry = {k: v for k, v in values.items()
             if k in ("retrieved", "influenced") or v is not None}
    validate_canonical(entry)
    return entry


def validate_canonical(entry: dict) -> bool:
    """Structural + honesty validation. Raises ``ValueError`` on violation;
    returns True otherwise. Cheap enough for per-entry use.

    Honesty rules enforced:
    - ``source_type`` must be in the closed set.
    - ``confidence`` must be a number in [0, 1] when present.
    - ``supports_answer=True`` requires ``retrieved=True`` AND an
      identifiable source (``source_ref``/``evidence_id``/``source_id``):
      evidence that was never retrieved or cannot be identified can never
      be claimed to support the answer.
    """
    st = entry.get("source_type")
    if st not in SOURCE_TYPES:
        raise ValueError(f"unknown provenance source_type: {st!r}")
    conf = entry.get("confidence")
    if conf is not None and not (isinstance(conf, (int, float))
                                 and 0.0 <= float(conf) <= 1.0):
        raise ValueError(f"confidence must be within [0, 1], got {conf!r}")
    for flag in ("retrieved", "influenced"):
        if flag in entry and not isinstance(entry[flag], bool):
            raise ValueError(f"{flag} must be boolean")
    if entry.get("supports_answer") is True:
        if not entry.get("retrieved"):
            raise ValueError(
                "supports_answer=True requires retrieved=True — retrieval "
                "state is never implied")
        if not (entry.get("source_ref") or entry.get("evidence_id")
                or entry.get("source_id")):
            raise ValueError(
                "supports_answer=True requires an identifiable source "
                "(source_ref / evidence_id / source_id)")
    return True


def serialize_provenance(entries: list[dict]) -> dict:
    """Wrap validated entries in the versioned envelope."""
    for e in entries:
        validate_canonical(e)
    return {"version": PROVENANCE_VERSION, "entries": list(entries)}


# ── converters (pure; caller supplies already-authorized org/resource) ──────

def from_sql_lineage(
    lineage: dict | None, *, org_id: str | None = None,
    resource_id: str | None = None, execution_ref: str | None = None,
) -> list[dict]:
    """P-SV2 execution lineage → canonical entries.

    Lineage tables were ACTUALLY READ by the executed statement, hence
    ``retrieved=True, influenced=True`` — the one converter where
    influence is a fact, not a ranking claim. Raw lineage carries no
    evidence status, so none is invented.
    """
    tables = (lineage or {}).get("tables") or []
    return [
        canonical_entry(
            source_type="sql_lineage", source_ref=str(t), org_id=org_id,
            resource_type="knowledge_base", resource_id=resource_id,
            retrieved=True, influenced=True, execution_ref=execution_ref)
        for t in tables if t
    ]


def from_ontology_provenance(
    prov: dict | None, *, org_id: str | None = None,
    resource_id: str | None = None, execution_ref: str | None = None,
) -> list[dict]:
    """Phase 3B ontology planning provenance → canonical entries.

    Status comes from the provenance BUCKET (objects=verified,
    hypotheses=proposed — never promoted) or the entry's own
    ``evidence_status``; influence comes from the honest ``influenced``
    flags recorded by table_retrieval; everything here was retrieved into
    the planning context.
    """
    if not prov:
        return []

    def _base(source_type, source_ref, *, status=None, target=None,
              influenced=False, kind=None):
        return canonical_entry(
            source_type=source_type, source_ref=source_ref, org_id=org_id,
            resource_type="knowledge_base", resource_id=resource_id,
            evidence_status=status, target_ref=target, kind=kind,
            retrieval_method="ontology_planning", retrieved=True,
            influenced=bool(influenced), execution_ref=execution_ref)

    entries: list[dict] = []
    for o in prov.get("objects") or []:
        entries.append(_base(
            "ontology_object", o.get("name"),
            status="verified", target=o.get("table"),
            influenced=o.get("influenced")))
    for o in prov.get("hypotheses") or []:
        entries.append(_base(
            "ontology_object", o.get("name"),
            status="proposed", target=o.get("table"),
            influenced=o.get("influenced")))
    for l in prov.get("links") or []:
        tables = l.get("tables") or []
        entries.append(_base(
            "ontology_relationship",
            f"{l.get('from')}--{l.get('to')}",
            status="verified", target=",".join(filter(None, tables)),
            influenced=l.get("influenced")))
    for t in prov.get("terms") or []:
        entries.append(_base(
            "terminology", t.get("word"), influenced=t.get("influenced")))
    for r in prov.get("enabled_sql_rules") or []:
        entries.append(_base(
            "rule", r.get("id"), status="enabled",
            influenced=r.get("influenced")))
    for m in prov.get("metrics") or []:
        entries.append(_base(
            "metric", m.get("name"),
            status=m.get("evidence_status"), target=m.get("table"),
            influenced=m.get("influenced")))
    return entries


def _row_get(obj, key, default=None):
    """Read from an ORM row or a plain dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def from_document_proposal(
    proposal, *, org_id: str | None = None, execution_ref: str | None = None,
) -> dict:
    """P4 ``DocumentProposal`` (row or dict) → canonical entry.

    Status passes through verbatim (proposed/approved/rejected/superseded)
    — machine steps can never create approved/verified rows and this
    contract never upgrades them either. DocumentProposal stores its
    lifecycle in ``status``; ProcessEvidence-style rows use
    ``evidence_status`` — both are honored, neither is ever renamed or
    promoted.
    """
    return canonical_entry(
        source_type="document",
        source_id=_row_get(proposal, "id"),
        org_id=org_id or _row_get(proposal, "org_id"),
        resource_type="knowledge_base",
        resource_id=_row_get(proposal, "kb_id"),
        evidence_id=_row_get(proposal, "id"),
        source_ref=_row_get(proposal, "chunk_key"),
        kind=_row_get(proposal, "proposal_kind"),
        retrieval_method=_row_get(proposal, "method"),
        # Phase 7C (E2): omit-when-absent — a source row/dict carrying no
        # lifecycle field must NOT be labeled "proposed" by invention
        # (tri-state honesty; real DocumentProposal rows always carry
        # status, so production entries are unchanged).
        evidence_status=(_row_get(proposal, "evidence_status")
                         or _row_get(proposal, "status")),
        retrieved=True, influenced=False, execution_ref=execution_ref)


def from_process_evidence(
    evidence, *, org_id: str | None = None, execution_ref: str | None = None,
) -> dict:
    """EBPM ``ProcessEvidence`` (row or dict) → canonical entry.

    ``confidence`` passes through verbatim or is omitted — never invented.
    Status passes through (proposed default per the model); retrieved=True
    (the evidence backs a discovered process), influenced=False (discovery
    annotation, not planning influence).
    """
    confidence = _row_get(evidence, "confidence")
    return canonical_entry(
        source_type="process_evidence",
        source_id=_row_get(evidence, "id"),
        org_id=org_id or _row_get(evidence, "org_id"),
        evidence_id=_row_get(evidence, "id"),
        source_ref=_row_get(evidence, "source_ref"),
        process_id=_row_get(evidence, "process_id"),
        activity_seq=_row_get(evidence, "activity_seq"),
        kind=_row_get(evidence, "evidence_kind"),
        # Phase 7C (E2): omit-when-absent (real ProcessEvidence rows carry
        # the model default "proposed" verbatim; foreign dicts without any
        # status field are no longer fabricated as "proposed")
        evidence_status=_row_get(evidence, "evidence_status"),
        confidence=confidence,
        retrieved=True, influenced=False, execution_ref=execution_ref)
