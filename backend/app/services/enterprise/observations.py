"""Observed-execution linkage — evidence-based, deterministic (Phase 3E).

Connects ACTUAL execution records (``Nl2sqlQueryLog``, ``AgentRun``) to
persisted enterprise processes with traceable evidence and honest
confidence. NO LLM anywhere in this module: every match comes from
stored, verifiable signals.

Confidence ladder (max signal wins; ties across DIFFERENT processes are
NEVER forced — they stay unmatched as "ambiguous"):
  1.0  explicit         — the record already carries a process link
  0.8  ontology_object  — VERIFIED ProcessEvidence references the object
  0.6  tool_binding     — executed tool bound to an ontology action whose
                          object has verified process evidence
  0.4  lineage          — executed SQL lineage tables appear in the
                          process's verified table-catalog evidence

Lifecycle honesty: observed ≠ verified ≠ documented — recording an
observation NEVER changes BusinessProcess.evidence_status /
discovery_status; proposed processes stay proposed; historical rows keep
NULL linkage until explicitly observed; an existing linkage is never
overwritten; no sequence edges are fabricated from timestamps.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

CONFIDENCE = {"explicit": 1.0, "ontology_object": 0.8,
              "tool_binding": 0.6, "lineage": 0.4}

_RECORD_TYPES = ("nl2sql_query", "agent_run")


def _unmatched(reason: str) -> dict:
    return {"matched": False, "process_id": None, "activity_seq": None,
            "confidence": None, "method": None, "evidence": [],
            "unmatched_reason": reason}


def _verified_object_processes(db: Session, org_id: str, object_id: str):
    """Processes whose VERIFIED evidence references this ontology object."""
    from app.models.enterprise_process import ProcessEvidence

    if not object_id:
        return []
    rows = db.query(ProcessEvidence.process_id).filter(
        ProcessEvidence.org_id == org_id,
        ProcessEvidence.is_deleted == False,  # noqa: E712
        ProcessEvidence.evidence_status == "verified",
        ProcessEvidence.source_ref == f"ontology:{object_id}",
    ).all()
    return sorted({r[0] for r in rows})


def match_execution(
    db: Session, org_id: str, *, explicit_process_id: str | None = None,
    explicit_activity_seq: int | None = None, object_id: str | None = None,
    tool_names: list[str] | None = None, agent_name: str | None = None,
    datasource_id: str | None = None, tables: list[str] | None = None,
) -> dict:
    """Match one execution's signals to a process. Deterministic; returns
    the matched process/activity with confidence + evidence, or an honest
    unmatched reason."""
    # 1. explicit reference — strongest signal, verified for this org
    if explicit_process_id:
        from app.models.enterprise_process import BusinessProcess

        row = db.query(BusinessProcess).filter(
            BusinessProcess.id == explicit_process_id,
            BusinessProcess.org_id == org_id,
            BusinessProcess.is_deleted == False,  # noqa: E712
        ).first()
        if not row:
            return _unmatched(
                f"explicit process {explicit_process_id!r} not found in "
                "this organization")
        return {"matched": True, "process_id": row.id,
                "activity_seq": explicit_activity_seq,
                "confidence": CONFIDENCE["explicit"], "method": "explicit",
                "evidence": [{"kind": "explicit_reference",
                              "process_id": row.id}],
                "unmatched_reason": None}

    candidates: dict[str, dict] = {}

    def _add(method, process_id, evidence, activity_seq=None):
        prev = candidates.get(process_id)
        if prev is None:
            candidates[process_id] = {"method": method,
                                      "activity_seq": activity_seq,
                                      "evidence": list(evidence)}
        else:
            if CONFIDENCE[method] > CONFIDENCE[prev["method"]]:
                prev["method"] = method
                prev["activity_seq"] = activity_seq
            prev["evidence"].extend(evidence)

    # 2. verified ontology-object relationship
    if object_id:
        for pid in _verified_object_processes(db, org_id, object_id):
            _add("ontology_object", pid,
                 [{"kind": "verified_object_evidence",
                   "object_id": object_id, "process_id": pid}])

    # 3. agent/tool execution metadata — bound ontology action → object →
    #    verified process evidence
    for tool in tool_names or []:
        if not tool:
            continue
        from app.models.ontology import OntologyAction

        act = db.query(OntologyAction).filter(
            OntologyAction.org_id == org_id,
            OntologyAction.tool_name == tool,
            OntologyAction.is_deleted == False,  # noqa: E712
        ).first()
        if act and act.object_id:
            for pid in _verified_object_processes(db, org_id, act.object_id):
                _add("tool_binding", pid,
                     [{"kind": "tool_binding", "tool_name": tool,
                       "action_id": act.id, "object_id": act.object_id,
                       "process_id": pid}])

    # 4. table lineage vs VERIFIED table-catalog evidence
    for table in tables or []:
        if not table:
            continue
        from app.models.enterprise_process import ProcessEvidence

        rows = db.query(ProcessEvidence.process_id).filter(
            ProcessEvidence.org_id == org_id,
            ProcessEvidence.is_deleted == False,  # noqa: E712
            ProcessEvidence.evidence_status == "verified",
            ProcessEvidence.evidence_kind == "table_catalog",
            ProcessEvidence.source_ref.in_([
                f"table://{table}", f"kb_table:{table}", table]),
        ).all()
        for (pid,) in rows:
            _add("lineage", pid,
                 [{"kind": "table_lineage", "table": table,
                   "process_id": pid}])

    if not candidates:
        return _unmatched(
            "no verified process evidence matches the execution signals "
            "(explicit reference, ontology object, bound tool, or lineage)")

    top = max(CONFIDENCE[c["method"]] for c in candidates.values())
    best = [pid for pid, c in candidates.items()
            if CONFIDENCE[c["method"]] == top]
    if len(best) > 1:
        return _unmatched(
            f"ambiguous evidence: {len(best)} processes match with equal "
            f"confidence {top} — refusing to force a match")
    pid = best[0]
    c = candidates[pid]
    return {"matched": True, "process_id": pid,
            "activity_seq": c["activity_seq"],
            "confidence": CONFIDENCE[c["method"]], "method": c["method"],
            "evidence": c["evidence"], "unmatched_reason": None}


# ── record stamping ───────────────────────────────────────────────────────────

def _agent_run_org(run) -> str | None:
    """AgentRun carries NO org_id column — the org lives in
    caller_context JSON. Parse defensively; None = unattributable."""
    try:
        return (json.loads(run.caller_context or "{}") or {}).get("org_id")
    except Exception:  # noqa: BLE001
        return None


def _load_record(db: Session, org_id: str, record_type: str, record_id: str):
    if record_type == "nl2sql_query":
        from app.models.nl2sql_query_log import Nl2sqlQueryLog

        return db.query(Nl2sqlQueryLog).filter(
            Nl2sqlQueryLog.id == record_id,
            Nl2sqlQueryLog.org_id == org_id).first()
    if record_type == "agent_run":
        from app.models.agent_run import AgentRun

        run = db.query(AgentRun).filter(
            AgentRun.run_id == record_id).first()
        if run is not None and _agent_run_org(run) == org_id:
            return run
    return None


def _record_signals(db: Session, org_id: str, record) -> dict:
    """Signals from the stored record itself (absent signals stay absent —
    never guessed)."""
    signals: dict = {}
    if hasattr(record, "lineage_json"):
        tables = ((record.lineage_json or {}).get("tables") or [])
        signals["tables"] = [str(t) for t in tables if t][:20]
        signals["datasource_id"] = record.datasource_id
    if hasattr(record, "tool_calls"):
        try:
            calls = json.loads(record.tool_calls or "[]")
            signals["tool_names"] = [
                c.get("name") for c in calls
                if isinstance(c, dict) and c.get("name")][:20]
        except Exception:  # noqa: BLE001
            signals["tool_names"] = []
        signals["agent_name"] = getattr(record, "agent_name", None)
    return signals


def record_observation(
    db: Session, org_id: str, record_type: str, record_id: str,
    *, signals: dict | None = None,
) -> dict:
    """Match one execution record and, when evidence suffices, stamp its
    linkage columns. IDEMPOTENT: an existing linkage is never overwritten
    (explicit links win forever). Process statuses are NEVER touched.

    ``signals`` may EXTEND the stored-record signals for callers holding
    execution context the row does not persist (e.g. the ontology object
    of the executed action). The row remains the identity source.
    """
    if record_type not in _RECORD_TYPES:
        return _unmatched(f"unknown record type {record_type!r}")
    record = _load_record(db, org_id, record_type, record_id)
    if record is None:
        return _unmatched(f"{record_type} {record_id!r} not found in this "
                          "organization")

    if getattr(record, "process_id", None):
        return {"matched": True, "process_id": record.process_id,
                "activity_seq": getattr(record, "activity_seq", None),
                "confidence": getattr(record, "match_confidence", None),
                "method": getattr(record, "match_method", None),
                "evidence": [{"kind": "existing_linkage"}],
                "unmatched_reason": None}

    merged = dict(_record_signals(db, org_id, record))
    merged.update(signals or {})
    decision = match_execution(
        db, org_id,
        explicit_process_id=merged.get("explicit_process_id"),
        explicit_activity_seq=merged.get("explicit_activity_seq"),
        object_id=merged.get("object_id"),
        tool_names=merged.get("tool_names"),
        agent_name=merged.get("agent_name"),
        datasource_id=merged.get("datasource_id"),
        tables=merged.get("tables"),
    )
    if decision["matched"]:
        record.process_id = decision["process_id"]
        record.activity_seq = decision["activity_seq"]
        record.match_confidence = decision["confidence"]
        record.match_method = decision["method"]
        db.commit()
    return decision


# ── observation summaries ─────────────────────────────────────────────────────

def _confidence_band(c) -> str:
    if c is None:
        return "low"
    return "high" if c >= 0.8 else ("medium" if c >= 0.5 else "low")


def process_observations(
    db: Session, org_id: str, process_id: str, *, limit: int = 50,
    inspect_record: tuple[str, str] | None = None,
) -> dict | None:
    """Org-scoped observation summary for one process. ``None`` when the
    process does not exist in this org (fail closed). Honest empty shape
    when nothing was ever observed. References carry NO execution
    payloads (no question/sql/result). No sequence edges are fabricated.
    """
    from app.models.agent_run import AgentRun
    from app.models.enterprise_process import BusinessProcess
    from app.models.nl2sql_query_log import Nl2sqlQueryLog

    proc = db.query(BusinessProcess).filter(
        BusinessProcess.id == process_id,
        BusinessProcess.org_id == org_id,
        BusinessProcess.is_deleted == False,  # noqa: E712
    ).first()
    if proc is None:
        return None

    references: list[dict] = []
    activity_counts: dict[str, int] = {}
    bands = {"high": 0, "medium": 0, "low": 0}
    times: list = []

    nl_rows = db.query(Nl2sqlQueryLog).filter(
        Nl2sqlQueryLog.org_id == org_id,
        Nl2sqlQueryLog.process_id == process_id,
    ).order_by(Nl2sqlQueryLog.created_date.desc()).limit(limit).all()
    for r in nl_rows:
        references.append({
            "record_type": "nl2sql_query", "record_id": r.id,
            "activity_seq": r.activity_seq,
            "match_confidence": r.match_confidence,
            "match_method": r.match_method,
            "observed_at": r.created_date.isoformat()
            if r.created_date else None})
        if r.activity_seq is not None:
            activity_counts[str(r.activity_seq)] = \
                activity_counts.get(str(r.activity_seq), 0) + 1
        bands[_confidence_band(r.match_confidence)] += 1
        if r.created_date:
            times.append(r.created_date)

    # AgentRun has no org_id column — bound the scan, verify org from
    # caller_context; cross-org runs never count.
    agent_refs = 0
    ar_rows = db.query(AgentRun).filter(
        AgentRun.process_id == process_id,
    ).order_by(AgentRun.created_date.desc()).limit(limit * 3).all()
    for r in ar_rows:
        if _agent_run_org(r) != org_id or agent_refs >= limit:
            continue
        agent_refs += 1
        references.append({
            "record_type": "agent_run", "record_id": r.run_id,
            "activity_seq": r.activity_seq,
            "match_confidence": r.match_confidence,
            "match_method": r.match_method,
            "observed_at": r.created_date.isoformat()
            if r.created_date else None})
        if r.activity_seq is not None:
            activity_counts[str(r.activity_seq)] = \
                activity_counts.get(str(r.activity_seq), 0) + 1
        bands[_confidence_band(r.match_confidence)] += 1
        if r.created_date:
            times.append(r.created_date)

    summary = {
        "process_id": process_id,
        "execution_count": len(references),
        "activity_counts": activity_counts,
        "confidence_distribution": bands,
        "time_range": ({"from": min(times).isoformat(),
                        "to": max(times).isoformat(),
                        "count": len(times)} if times else None),
        "references": references,
        "unmatched_executions": [],
        "note": ("observed ≠ verified ≠ documented — observations record "
                 "that executions MATCHED this process; they never change "
                 "its evidence_status or discovery_status; unmatched "
                 "executions are not enumerated (bounded query)"),
    }
    if inspect_record:
        decision = record_observation(db, org_id, inspect_record[0],
                                      inspect_record[1])
        summary["inspected_record"] = decision
        if not decision["matched"]:
            summary["unmatched_executions"] = [{
                "record_type": inspect_record[0],
                "record_id": inspect_record[1],
                "unmatched_reason": decision["unmatched_reason"]}]
    return summary
