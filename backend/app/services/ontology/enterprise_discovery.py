"""Enterprise discovery — inventory the org's authorized resources.

Deterministic first: the inventory is assembled ONLY from rows the org
already owns (KBs, catalogued schemas, ontology elements, automations,
agents, actions). No LLM, no guessing, no cross-org reads. LLM-assisted
process proposal lives in `propose_processes` and is explicitly marked
inferred/proposed with evidence quotes.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def inventory_resources(db: Session, org_id: str) -> dict:
    """Deterministic inventory of the org's authorized enterprise resources.

    Assembles what the org ALREADY has — never assumes a source, never
    queries outside the org. Every section can be honestly empty.
    """
    from app.models.agent_run import AgentRun
    from app.models.automation_task import AutomationTask
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta, KBTableRelation
    from app.models.ontology import (
        OntologyAction, OntologyDomain, OntologyLink, OntologyObject,
    )
    from app.models.metric_definition import MetricDefinition

    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.org_id == org_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .all()
    )
    datasources, documents = [], []
    for kb in kbs:
        entry = {
            "id": kb.id, "name": kb.name,
            "source_kind": kb.source_kind or "database",
            "db_type": getattr(kb, "db_type", None),
            "status": kb.status,
            "catalog_status": getattr(kb, "catalog_status", None),
            "indexing_status": getattr(kb, "indexing_status", None),
        }
        if (kb.source_kind or "") == "database":
            tmeta = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb.id, KBTableMeta.is_deleted == False)  # noqa: E712
                .count()
            )
            table_ids = [
                t.id for t in db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == kb.id,
                        KBTableMeta.is_deleted == False)  # noqa: E712
                .all()
            ]
            cmeta = (
                db.query(KBColumnMeta)
                .filter(KBColumnMeta.table_meta_id.in_(table_ids or [""]),
                        KBColumnMeta.is_deleted == False)  # noqa: E712
                .count()
            ) if table_ids else 0
            rels = (
                db.query(KBTableRelation)
                .filter(KBTableRelation.kb_id == kb.id, KBTableRelation.is_deleted == False)  # noqa: E712
                .count()
            )
            entry.update({"tables": tmeta, "columns": cmeta, "relations": rels})
            datasources.append(entry)
        else:
            entry.update({
                "file_type": kb.file_type,
                "chunk_count": kb.chunk_count or 0,
                "indexing_status": kb.indexing_status,
            })
            documents.append(entry)

    domains = db.query(OntologyDomain).filter(
        OntologyDomain.org_id == org_id, OntologyDomain.is_deleted == False).count()  # noqa: E712
    objects = db.query(OntologyObject).filter(
        OntologyObject.org_id == org_id, OntologyObject.is_deleted == False).count()  # noqa: E712
    links = db.query(OntologyLink).filter(
        OntologyLink.org_id == org_id, OntologyLink.is_deleted == False).count()  # noqa: E712
    actions = db.query(OntologyAction).filter(
        OntologyAction.org_id == org_id, OntologyAction.is_deleted == False).all()
    metrics = db.query(MetricDefinition).filter(
        MetricDefinition.org_id == org_id, MetricDefinition.is_deleted == False).count()  # noqa: E712
    automations = db.query(AutomationTask).filter(
        AutomationTask.org_id == org_id, AutomationTask.is_deleted == False).all()
    agents = db.query(AgentRun).filter(
        AgentRun.org_id == org_id).distinct(AgentRun.agent_name).count() \
        if hasattr(AgentRun, "org_id") else 0

    return {
        "org_id": org_id,
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "datasources": datasources,
        "documents": documents,
        "ontology": {
            "domains": domains, "objects": objects, "links": links,
            "metrics": metrics,
            "actions": [
                {"id": a.id, "name": a.name, "tool_name": a.tool_name,
                 "risk_level": a.risk_level}
                for a in actions
            ],
        },
        "automations": [
            {"id": a.id, "name": getattr(a, "name", None) or a.id,
             "schedule_kind": getattr(a, "schedule_kind", None),
             "status": getattr(a, "status", None)}
            for a in automations
        ],
        "agents_active": agents,
        "summary": {
            "datasources": len(datasources),
            "documents": len(documents),
            "ontology_objects": objects,
            "actions": len(actions),
            "automations": len(automations),
        },
    }


def refresh_inventory(db: Session, org_id: str) -> dict:
    """Persist the inventory as EnterpriseResource rows (upsert by ref)."""
    from app.models.enterprise_process import EnterpriseResource

    inv = inventory_resources(db, org_id)
    now = datetime.now(timezone.utc).isoformat()

    rows: list[tuple[str, str, str, dict, str]] = []
    for ds in inv["datasources"]:
        rows.append(("datasource", ds["id"], ds["name"], ds, "verified"))
    for doc in inv["documents"]:
        rows.append(("document", doc["id"], doc["name"], doc, "verified"))
    for act in inv["ontology"]["actions"]:
        rows.append(("action_interface", act["id"], act["name"], act, "verified"))
    for auto in inv["automations"]:
        rows.append(("automation", auto["id"], auto["name"], auto, "verified"))

    existing = {
        (r.resource_kind, r.ref_id): r
        for r in db.query(EnterpriseResource)
        .filter(EnterpriseResource.org_id == org_id,
                EnterpriseResource.is_deleted == False)  # noqa: E712
        .all()
    }
    for kind, ref, name, status, ev in rows:
        row = existing.get((kind, ref))
        if row:
            row.name = name
            row.status_json = status
            row.discovered_at = now
        else:
            db.add(EnterpriseResource(
                org_id=org_id, resource_kind=kind, ref_id=ref,
                name=name, status_json=status,
                evidence_status=ev, discovered_at=now))
    db.commit()
    return inv


# ---------------------------------------------------------------------------
# Increment 2 — evidence extraction + AI-assisted operating-model proposal
# ---------------------------------------------------------------------------

_MAX_TABLE_EVIDENCE = 14
_MAX_RELATION_EVIDENCE = 20
_MAX_OBJECT_EVIDENCE = 30
_MAX_CHUNKS_PER_DOC = 8

_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "org_units": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "unit_type": {"type": "string",
                                  "enum": ["department", "team", "role"]},
                    "responsibilities": {"type": "string"},
                    "evidence_ids": {"type": "array",
                                     "items": {"type": "string"}},
                },
                "required": ["name", "unit_type"],
            },
        },
        "capabilities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "evidence_ids": {"type": "array",
                                     "items": {"type": "string"}},
                },
                "required": ["name"],
            },
        },
        "processes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "purpose": {"type": "string"},
                    "trigger_event": {"type": "string"},
                    "objective": {"type": "string"},
                    "start_condition": {"type": "string"},
                    "completion_condition": {"type": "string"},
                    "process_family": {"type": "string"},
                    "discovery_status": {"type": "string",
                                         "enum": ["documented",
                                                  "configured",
                                                  "observed"]},
                    "evidence_ids": {"type": "array",
                                     "items": {"type": "string"}},
                    "activities": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "seq": {"type": "integer"},
                                "name": {"type": "string"},
                                "activity_type": {"type": "string"},
                                "performer": {"type": "string"},
                                "business_rules": {"type": "string"},
                                "exception_paths": {"type": "string"},
                                "evidence_ids": {"type": "array",
                                                 "items": {"type": "string"}},
                            },
                            "required": ["seq", "name"],
                        },
                    },
                },
                "required": ["name"],
            },
        },
    },
    "required": ["org_units", "capabilities", "processes"],
}

_DISCOVERY_PROMPT = """You are reconstructing how an enterprise operates from
AUTHORIZED evidence only. Below is evidence extracted from the organization's
own systems: database catalog metadata, table relations, ontology objects and
links, document excerpts, and workflow/automation configurations.

Rules:
- Propose ONLY what the evidence supports. Never invent departments,
  activities, systems, or execution history.
- Each proposal MUST reference the evidence ids that support it (evidence_ids).
- discovery_status: "documented" only when document/policy evidence says so;
  "configured" only when workflow/automation configuration shows it;
  "observed" ONLY when timestamped execution records were provided (they are
  NOT part of this evidence set — do not use it).
- If evidence is insufficient for a concept, omit it. An empty proposal is
  better than an invented one.
- activity_type one of: start_event, end_event, human_task, system_task,
  decision, approval, subprocess.

Return JSON matching the schema. Evidence:
{evidence}
"""


def extract_evidence(db: Session, org_id: str, inv: dict) -> tuple[list, list]:
    """Phase B — deterministic evidence extraction from authorized rows.

    Every item: {id, kind, source_ref, quote}. Quotes are VERBATIM metadata
    or chunk text — never synthesized. Bounded; per-resource error isolation.
    """
    from app.models.enterprise_process import EnterpriseResource  # noqa: F401
    from app.models.knowledge_catalog import (
        KBColumnMeta, KBTableMeta, KBTableRelation,
    )
    from app.models.ontology import OntologyLink, OntologyObject

    evidence: list[dict] = []
    skipped: list[dict] = []

    def _add(kind, source_ref, quote):
        evidence.append({
            "id": f"ev-{len(evidence) + 1}", "kind": kind,
            "source_ref": source_ref, "quote": (quote or "")[:1200],
        })

    # 1) Database catalog metadata (per authorized datasource KB).
    for ds in inv["datasources"]:
        try:
            tmetas = (
                db.query(KBTableMeta)
                .filter(KBTableMeta.kb_id == ds["id"],
                        KBTableMeta.is_deleted == False)  # noqa: E712
                .limit(_MAX_TABLE_EVIDENCE).all()
            )
            for tm in tmetas:
                cols = [
                    c.column_name for c in db.query(KBColumnMeta)
                    .filter(KBColumnMeta.table_meta_id == tm.id,
                            KBColumnMeta.is_deleted == False)  # noqa: E712
                    .limit(12).all()
                ]
                desc = tm.description_en or tm.description_zh or tm.source_comment or ""
                _add("table_catalog", f"{ds['id']}:{tm.table_name}",
                     f"table {tm.table_name} ({tm.table_role or 'table'})"
                     f" — {desc} — columns: {', '.join(cols)}")
            rels = (
                db.query(KBTableRelation)
                .filter(KBTableRelation.kb_id == ds["id"],
                        KBTableRelation.is_deleted == False)  # noqa: E712
                .limit(_MAX_RELATION_EVIDENCE).all()
            )
            for rel in rels:
                src = db.query(KBTableMeta).filter(
                    KBTableMeta.id == rel.source_table_meta_id).first()
                dst = db.query(KBTableMeta).filter(
                    KBTableMeta.id == rel.target_table_meta_id).first()
                if src and dst:
                    _add("relation", f"{ds['id']}:{rel.id}",
                         f"{src.table_name} -> {dst.table_name} "
                         f"({rel.relation_type})")
        except Exception as exc:  # noqa: BLE001 — error isolation per resource
            skipped.append({"resource": ds["name"],
                            "reason": f"catalog read failed: {exc}"})

    # 2) Ontology objects + links (evidence for entity handoffs).
    try:
        objs = {
            o.id: o for o in db.query(OntologyObject)
            .filter(OntologyObject.org_id == org_id,
                    OntologyObject.is_deleted == False)  # noqa: E712
            .limit(_MAX_OBJECT_EVIDENCE).all()
        }
        for o in objs.values():
            _add("ontology_object", f"ontology:{o.id}",
                 f"business object {o.name}"
                 f" — {getattr(o, 'description', None) or ''}")
        links = (
            db.query(OntologyLink)
            .filter(OntologyLink.org_id == org_id,
                    OntologyLink.is_deleted == False)  # noqa: E712
            .limit(_MAX_OBJECT_EVIDENCE).all()
        )
        for l in links:
            a = objs.get(l.from_object_id) or db.query(OntologyObject).filter(
                OntologyObject.id == l.from_object_id).first()
            b = objs.get(l.to_object_id) or db.query(OntologyObject).filter(
                OntologyObject.id == l.to_object_id).first()
            if a and b:
                _add("ontology_link", f"ontology:{l.id}",
                     f"{a.name} --{l.link_type}--> {b.name}")
    except Exception as exc:  # noqa: BLE001
        skipped.append({"resource": "ontology", "reason": str(exc)})

    # 3) Document evidence: catalog rows + best-effort verbatim chunk sample.
    for doc in inv["documents"]:
        _add("document_catalog", f"kb:{doc['id']}",
             f"document '{doc['name']}' ({doc.get('file_type') or 'file'}, "
             f"{doc.get('chunk_count') or 0} indexed chunks)")
        try:
            from app.services.document_ingestion.store import _get_collection
            coll = _get_collection(org_id)
            res = coll.get(where={"kb_id": doc["id"]},
                           limit=_MAX_CHUNKS_PER_DOC,
                           include=["documents", "metadatas"])
            for text in (res.get("documents") or [])[:_MAX_CHUNKS_PER_DOC]:
                if text:
                    _add("document_chunk", f"kb:{doc['id']}", text)
        except Exception as exc:  # noqa: BLE001 — store unavailable is honest
            skipped.append({
                "resource": doc["name"],
                "reason": f"chunk store unavailable: {type(exc).__name__}"})

    # 4) Configured workflows: automations + governed actions.
    for auto in inv["automations"]:
        _add("automation_config", f"automation:{auto['id']}",
             f"scheduled automation '{auto['name']}' "
             f"(status: {auto.get('status')})")
    for act in inv["ontology"]["actions"]:
        _add("action_interface", f"action:{act['id']}",
             f"governed action '{act['name']}' tool={act.get('tool_name')} "
             f"risk={act.get('risk_level')}")

    return evidence, skipped


def _clean_evidence_ids(raw, valid_ids):
    return [e for e in (raw or []) if e in valid_ids]


def _persist_proposals(db: Session, org_id: str, payload: dict,
                       evidence: list) -> dict:
    """Phase C persistence — upsert with dedup + verified-preservation.

    - dedup by (org, name) — repeated runs UPDATE, never duplicate;
    - evidence rows upsert by (org, process, source_ref);
    - rows already VERIFIED by a human are never weakened by proposals.
    """
    from app.models.enterprise_process import (
        BusinessCapability, BusinessProcess, OrgUnit, ProcessActivity,
        ProcessEvidence,
    )

    valid = {e["id"]: e for e in evidence}
    counts = {"proposals_created": 0, "proposals_updated": 0,
              "evidence_created": 0, "needs_review": 0}

    def _prov(ids):
        return {"discovery_method": "ai_proposal",
                "evidence_ids": _clean_evidence_ids(ids, valid)}

    for u in payload.get("org_units") or []:
        name = (u.get("name") or "").strip()[:255]
        if not name:
            continue
        row = db.query(OrgUnit).filter(
            OrgUnit.org_id == org_id, OrgUnit.name == name,
            OrgUnit.is_deleted == False).first()  # noqa: E712
        prov = _prov(u.get("evidence_ids"))
        if row is None:
            db.add(OrgUnit(
                org_id=org_id, name=name,
                unit_type=u.get("unit_type") or "department",
                responsibilities=u.get("responsibilities"),
                evidence_status="proposed", provenance_json=prov))
            counts["proposals_created"] += 1
        elif row.evidence_status != "verified":
            row.responsibilities = u.get("responsibilities") or row.responsibilities
            row.provenance_json = prov
            counts["proposals_updated"] += 1

    for cp in payload.get("capabilities") or []:
        name = (cp.get("name") or "").strip()[:255]
        if not name:
            continue
        row = db.query(BusinessCapability).filter(
            BusinessCapability.org_id == org_id, BusinessCapability.name == name,
            BusinessCapability.is_deleted == False).first()  # noqa: E712
        prov = _prov(cp.get("evidence_ids"))
        if row is None:
            db.add(BusinessCapability(
                org_id=org_id, name=name, description=cp.get("description"),
                evidence_status="proposed", provenance_json=prov))
            counts["proposals_created"] += 1
        elif row.evidence_status != "verified":
            row.description = cp.get("description") or row.description
            row.provenance_json = prov
            counts["proposals_updated"] += 1

    for pr in payload.get("processes") or []:
        name = (pr.get("name") or "").strip()[:255]
        if not name:
            continue
        prov = _prov(pr.get("evidence_ids"))
        row = db.query(BusinessProcess).filter(
            BusinessProcess.org_id == org_id, BusinessProcess.name == name,
            BusinessProcess.is_deleted == False).first()  # noqa: E712
        if row is None:
            row = BusinessProcess(
                org_id=org_id, name=name,
                purpose=pr.get("purpose"), trigger_event=pr.get("trigger_event"),
                objective=pr.get("objective"),
                start_condition=pr.get("start_condition"),
                completion_condition=pr.get("completion_condition"),
                process_family=(pr.get("process_family") or "")[:120] or None,
                discovery_status=pr.get("discovery_status") or "documented",
                evidence_status="proposed", confidence=None,
                provenance_json=prov)
            db.add(row)
            db.flush()
            counts["proposals_created"] += 1
        elif row.evidence_status not in ("verified", "rejected"):
            # Human decisions (verified/rejected) survive rediscovery.
            for f in ("purpose", "trigger_event", "objective",
                      "start_condition", "completion_condition"):
                if pr.get(f):
                    setattr(row, f, pr[f])
            row.process_family = ((pr.get("process_family") or "")[:120]
                                  or row.process_family)
            row.provenance_json = prov
            counts["proposals_updated"] += 1
        counts["needs_review"] += 1  # every proposal awaits human review

        # Activities — replace-by-upsert per (seq, name).
        existing_acts = {
            (a.seq, a.name): a for a in db.query(ProcessActivity)
            .filter(ProcessActivity.org_id == org_id,
                    ProcessActivity.process_id == row.id,
                    ProcessActivity.is_deleted == False).all()  # noqa: E712
        }
        allowed_types = {"start_event", "end_event", "human_task",
                         "system_task", "decision", "approval", "subprocess"}
        for act in pr.get("activities") or []:
            try:
                seq = int(act.get("seq"))
            except (TypeError, ValueError):
                continue
            aname = (act.get("name") or "").strip()[:255]
            if not aname:
                continue
            prov = _prov(act.get("evidence_ids"))
            atype = act.get("activity_type") or "human_task"
            if atype not in allowed_types:
                atype = "human_task"
            key = (seq, aname)
            if key in existing_acts:
                a = existing_acts.pop(key)
                if a.evidence_status != "verified":
                    a.activity_type = atype
                    a.performer = act.get("performer") or a.performer
                    a.business_rules = act.get("business_rules") or a.business_rules
                    a.exception_paths = (act.get("exception_paths")
                                         or a.exception_paths)
                    a.provenance_json = prov
            else:
                db.add(ProcessActivity(
                    org_id=org_id, process_id=row.id, seq=seq, name=aname,
                    activity_type=atype, performer=act.get("performer"),
                    business_rules=act.get("business_rules"),
                    exception_paths=act.get("exception_paths"),
                    evidence_status="proposed", provenance_json=prov))
        # activities removed from the latest proposal stay (evidence history).

        # Evidence rows — upsert by source_ref, never duplicate.
        seen_refs = set()
        for eid in _clean_evidence_ids(pr.get("evidence_ids"), valid):
            ev = valid[eid]
            if ev["source_ref"] in seen_refs:
                continue
            seen_refs.add(ev["source_ref"])
            erow = db.query(ProcessEvidence).filter(
                ProcessEvidence.org_id == org_id,
                ProcessEvidence.process_id == row.id,
                ProcessEvidence.source_ref == ev["source_ref"],
                ProcessEvidence.is_deleted == False).first()  # noqa: E712
            if erow is None:
                db.add(ProcessEvidence(
                    org_id=org_id, process_id=row.id,
                    evidence_kind=ev["kind"], source_ref=ev["source_ref"],
                    quote=ev["quote"]))
                counts["evidence_created"] += 1

    db.commit()
    return counts


_MAX_PROMPT_ITEMS = 40
_MAX_PROMPT_CHARS = 24_000


def _bounded_prompt_evidence(evidence: list) -> list:
    """Bounded LLM input (directive §7): cap items and total chars, keeping
    kind diversity — document chunks and relations carry the process signal."""
    by_kind: dict[str, list] = {}
    for e in evidence:
        by_kind.setdefault(e["kind"], []).append(e)
    quota = {"document_chunk": 10, "relation": 8, "ontology_link": 6,
             "table_catalog": 8, "document_catalog": 4, "ontology_object": 8,
             "automation_config": 4, "action_interface": 6}
    picked: list[dict] = []
    for kind, items in by_kind.items():
        picked.extend(items[:quota.get(kind, 3)])
    out, total = [], 0
    for e in picked:
        if len(out) >= _MAX_PROMPT_ITEMS or total + len(e["quote"]) > _MAX_PROMPT_CHARS:
            break
        out.append(e)
        total += len(e["quote"])
    return out


def propose_operating_model(db: Session, org_id: str,
                            llm_fn=None) -> dict:
    """Full discovery pass: refresh inventory → extract evidence → one
    bounded structured LLM call → validate → persist proposals.

    Honest degradation: LLM unavailable/malformed ⇒ inventory-only result
    with ``llm_ok: false``; insufficient evidence ⇒ empty proposals.
    """
    inv = refresh_inventory(db, org_id)
    evidence, skipped = extract_evidence(db, org_id, inv)

    result = {
        "org_id": org_id,
        "resources_inspected": inv["summary"]["datasources"] + inv["summary"]["documents"],
        "documents_inspected": len(inv["documents"]),
        "catalog_entries_inspected": sum(d.get("tables", 0) for d in inv["datasources"]),
        "evidence_extracted": len(evidence),
        "skipped": skipped,
        "llm_ok": False,
    }
    if not evidence:
        result.update({
            "status": "insufficient_evidence",
            "note": ("no authorized resource evidence available — nothing "
                     "proposed (honest empty result)"),
            **{k: 0 for k in ("proposals_created", "proposals_updated",
                              "evidence_created", "needs_review")},
        })
        return result

    if llm_fn is None:
        from app.services.llm_service import chat_completion_json_sync as llm_fn
    # Reuse the existing hierarchical LLM resolution (org/workspace endpoint
    # config) — the discovery engine never hardcodes providers or keys.
    endpoint = None
    try:
        from app.services.llm_router import resolve_effective_llm
        eff = resolve_effective_llm(db, org_id=org_id, user_is_admin=True)
        endpoint = getattr(eff, "endpoint", None)
    except Exception:  # noqa: BLE001 — fall back to the legacy env provider
        endpoint = None
    prompt_evidence = _bounded_prompt_evidence(evidence)
    prompt = _DISCOVERY_PROMPT.format(
        evidence="\n".join(
            f"[{e['id']}] {e['kind']} {e['source_ref']}: {e['quote']}"
            for e in prompt_evidence))
    try:
        payload = llm_fn(prompt, schema=_PROPOSAL_SCHEMA, endpoint=endpoint)
        if not payload:
            # One bounded retry without the JSON-schema wrapper — some
            # compatible-mode providers reject response_json_schema.
            payload = llm_fn(prompt, endpoint=endpoint)
            if isinstance(payload, dict) and "response" in payload and len(payload) == 1:
                import json as _json
                try:
                    payload = _json.loads(payload["response"])
                except (ValueError, TypeError):
                    pass
    except Exception as exc:  # noqa: BLE001 — degrade honestly
        result.update({"status": "llm_error", "reason": str(exc),
                       **{k: 0 for k in ("proposals_created",
                                         "proposals_updated",
                                         "evidence_created",
                                         "needs_review")}})
        return result
    if not payload or not isinstance(payload, dict):
        result.update({"status": "llm_malformed",
                       "reason": "empty or non-object model response",
                       **{k: 0 for k in ("proposals_created",
                                         "proposals_updated",
                                         "evidence_created",
                                         "needs_review")}})
        return result

    counts = _persist_proposals(db, org_id, payload, evidence)
    result.update({"status": "ok", "llm_ok": True, **counts})
    return result


# ---------------------------------------------------------------------------
# Increment 3 — Enterprise Process Management (catalogue / detail / review)
# ---------------------------------------------------------------------------

def _process_evidence_counts(db: Session, org_id: str, process_ids: list) -> dict:
    from sqlalchemy import func

    from app.models.enterprise_process import ProcessEvidence
    if not process_ids:
        return {}
    rows = (
        db.query(ProcessEvidence.process_id, func.count(ProcessEvidence.id))
        .filter(ProcessEvidence.org_id == org_id,
                ProcessEvidence.process_id.in_(process_ids),
                ProcessEvidence.is_deleted == False)  # noqa: E712
        .group_by(ProcessEvidence.process_id)
        .all()
    )
    return {pid: cnt for pid, cnt in rows}


def list_processes(db: Session, org_id: str, *, q: str | None = None,
                   family: str | None = None, status: str | None = None,
                   page: int = 1, page_size: int = 20) -> dict:
    """Process catalogue — search / filter / paginate with honest counts.

    Every process carries its discovery_status (documented/configured/
    observed — never conflated), evidence_status and evidence/activity
    counts. ``needs_review`` is true for AI proposals awaiting a human
    decision. Responsible units come from ACTIVITY performers (observed
    in evidence), never from a hardcoded department mapping.
    """
    from app.models.enterprise_process import BusinessProcess, ProcessActivity

    page = max(1, int(page or 1))
    page_size = min(100, max(1, int(page_size or 20)))

    base = db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id,
        BusinessProcess.is_deleted == False)  # noqa: E712
    if q:
        like = f"%{q}%"
        base = base.filter(
            or_(BusinessProcess.name.ilike(like),
                BusinessProcess.purpose.ilike(like)))
    if family:
        base = base.filter(BusinessProcess.process_family == family)
    if status:
        base = base.filter(BusinessProcess.evidence_status == status)

    total = base.count()
    rows = (
        base.order_by(BusinessProcess.process_family.asc().nullslast(),
                      BusinessProcess.name.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    ids = [r.id for r in rows]
    ev_counts = _process_evidence_counts(db, org_id, ids)
    act_counts: dict = {}
    performers: dict = {}
    if ids:
        acts = (
            db.query(ProcessActivity)
            .filter(ProcessActivity.org_id == org_id,
                    ProcessActivity.process_id.in_(ids),
                    ProcessActivity.is_deleted == False)  # noqa: E712
            .all()
        )
        for a in acts:
            act_counts[a.process_id] = act_counts.get(a.process_id, 0) + 1
            if a.performer:
                performers.setdefault(a.process_id, set()).add(a.performer)

    items = [
        {
            "id": r.id, "name": r.name,
            "process_family": r.process_family,
            "discovery_status": r.discovery_status,
            "evidence_status": r.evidence_status,
            "needs_review": r.evidence_status == "proposed",
            "purpose": r.purpose,
            "evidence_count": ev_counts.get(r.id, 0),
            "activities_count": act_counts.get(r.id, 0),
            "responsible_units": sorted(performers.get(r.id, set())),
            "updated_date": r.updated_date.isoformat() if r.updated_date else None,
        }
        for r in rows
    ]
    from sqlalchemy import func

    families = [
        {"family": f or None, "count": c}
        for f, c in (
            db.query(BusinessProcess.process_family,
                     func.count(BusinessProcess.id))
            .filter(BusinessProcess.org_id == org_id,
                    BusinessProcess.is_deleted == False)  # noqa: E712
            .group_by(BusinessProcess.process_family)
            .order_by(func.count(BusinessProcess.id).desc())
            .all()
        )
    ]
    return {"total": total, "page": page, "page_size": page_size,
            "families": families, "items": items}


def get_process(db: Session, org_id: str, process_id: str) -> dict | None:
    """Full process detail — ordered activities, verbatim evidence,
    related processes (both link directions), ontology connections from
    evidence source_refs, and the human review history. Cross-org or
    missing ids return None (fail closed)."""
    from app.models.enterprise_process import (
        BusinessProcess, ProcessActivity, ProcessEvidence, ProcessLink,
    )
    from app.models.ontology import OntologyLink, OntologyObject

    row = db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id, BusinessProcess.id == process_id,
        BusinessProcess.is_deleted == False).first()  # noqa: E712
    if row is None:
        return None

    activities = [
        {
            "seq": a.seq, "name": a.name, "activity_type": a.activity_type,
            "performer": a.performer, "business_rules": a.business_rules,
            "exception_paths": a.exception_paths,
            "evidence_status": a.evidence_status,
            "provenance": a.provenance_json,
        }
        for a in db.query(ProcessActivity)
        .filter(ProcessActivity.org_id == org_id,
                ProcessActivity.process_id == process_id,
                ProcessActivity.is_deleted == False)  # noqa: E712
        .order_by(ProcessActivity.seq.asc())
        .all()
    ]
    evidence = [
        {"id": e.id, "evidence_kind": e.evidence_kind,
         "source_ref": e.source_ref, "quote": e.quote,
         "confidence": e.confidence}
        for e in db.query(ProcessEvidence)
        .filter(ProcessEvidence.org_id == org_id,
                ProcessEvidence.process_id == process_id,
                ProcessEvidence.is_deleted == False)  # noqa: E712
        .order_by(ProcessEvidence.evidence_kind, ProcessEvidence.source_ref)
        .all()
    ]

    peer_ids = set()
    links = db.query(ProcessLink).filter(
        ProcessLink.org_id == org_id,
        or_(ProcessLink.predecessor_id == process_id,
            ProcessLink.successor_id == process_id),
        ProcessLink.is_deleted == False).all()  # noqa: E712
    peer_ids = {l.predecessor_id for l in links} | {l.successor_id for l in links}
    peer_ids.add(process_id)
    peers = {p.id: p for p in db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id,
        BusinessProcess.is_deleted == False,  # noqa: E712
        BusinessProcess.id.in_(list(peer_ids))).all()}
    related = [
        {"direction": "successor" if l.predecessor_id == process_id
         else "predecessor",
         "peer_id": (l.successor_id if l.predecessor_id == process_id
                     else l.predecessor_id),
         "peer_name": getattr(peers.get(
             l.successor_id if l.predecessor_id == process_id
             else l.predecessor_id), "name", None),
         "handoff_note": l.handoff_note,
         "evidence_status": l.evidence_status}
        for l in links
    ]

    # Ontology connections referenced by this process's evidence.
    ontology_refs = []
    for e in evidence:
        if e["source_ref"].startswith("ontology:"):
            ref_id = e["source_ref"].split(":", 1)[1]
            obj = db.query(OntologyObject).filter(
                OntologyObject.org_id == org_id,
                OntologyObject.id == ref_id).first()
            if obj:
                ontology_refs.append({"kind": "object", "id": obj.id,
                                      "name": obj.name,
                                      "source_ref": e["source_ref"]})
                continue
            link = db.query(OntologyLink).filter(
                OntologyLink.org_id == org_id,
                OntologyLink.id == ref_id).first()
            if link:
                ontology_refs.append({"kind": "link", "id": link.id,
                                      "link_type": link.link_type,
                                      "source_ref": e["source_ref"]})

    prov = row.provenance_json or {}
    return {
        "id": row.id, "name": row.name, "purpose": row.purpose,
        "trigger_event": row.trigger_event, "objective": row.objective,
        "start_condition": row.start_condition,
        "completion_condition": row.completion_condition,
        "process_family": row.process_family,
        "discovery_status": row.discovery_status,
        "evidence_status": row.evidence_status,
        "confidence": row.confidence,
        "provenance": prov,
        "review_history": prov.get("review_history") or [],
        "activities": activities,
        "evidence": evidence,
        "related_processes": related,
        "ontology_connections": ontology_refs,
        "updated_date": row.updated_date.isoformat() if row.updated_date else None,
    }


def _record_process_review(db: Session, org_id: str, process,
                           decision: str, reviewer, reason: str | None):
    """Shared review bookkeeping: provenance review_history + AuditEvent.

    The AI provenance (discovery_method, evidence_ids) is PRESERVED — the
    review decision is appended, never overwritten.
    """
    from app.models.audit_event import AuditEvent

    prov = dict(process.provenance_json or {})
    history = list(prov.get("review_history") or [])
    history.append({
        "decision": decision,
        "reviewer_id": getattr(reviewer, "id", None),
        "reviewer_name": getattr(reviewer, "full_name", None)
        or getattr(reviewer, "email", None),
        "reason": reason,
        "at": datetime.now(timezone.utc).isoformat(),
    })
    prov["review_history"] = history
    process.provenance_json = prov
    db.add(AuditEvent(
        org_id=org_id,
        event_type=f"process_{decision}",
        event_source="ontology",
        actor_type="user",
        actor_id=getattr(reviewer, "id", None),
        actor_name=getattr(reviewer, "full_name", None)
        or getattr(reviewer, "email", None),
        resource_type="business_process",
        resource_id=process.id,
        outcome="success",
        detail_json={"process_name": process.name, "reason": reason},
        occurred_at=datetime.now(timezone.utc),
    ))


def confirm_process(db: Session, org_id: str, process_id: str,
                    reviewer) -> dict | None:
    """Human confirm (org-admin): proposed/inferred → verified. The row
    then survives rediscovery unchanged."""
    from app.models.enterprise_process import BusinessProcess

    row = db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id, BusinessProcess.id == process_id,
        BusinessProcess.is_deleted == False).first()  # noqa: E712
    if row is None:
        return None
    _record_process_review(db, org_id, row, "confirmed", reviewer, None)
    row.evidence_status = "verified"
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name,
            "evidence_status": row.evidence_status,
            "review_history": (row.provenance_json or {}).get("review_history")}


def reject_process(db: Session, org_id: str, process_id: str,
                   reviewer, reason: str | None = None) -> dict | None:
    """Human reject (org-admin): proposed/inferred → rejected. The row AND
    its rejection decision survive rediscovery — the proposal is never
    silently re-created."""
    from app.models.enterprise_process import BusinessProcess

    row = db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id, BusinessProcess.id == process_id,
        BusinessProcess.is_deleted == False).first()  # noqa: E712
    if row is None:
        return None
    _record_process_review(db, org_id, row, "rejected", reviewer, reason)
    row.evidence_status = "rejected"
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name,
            "evidence_status": row.evidence_status,
            "reason": reason,
            "review_history": (row.provenance_json or {}).get("review_history")}


def enterprise_map(db: Session, org_id: str) -> dict:
    """Enterprise Map aggregation — the persisted business structure in one
    org-scoped payload: org units, capabilities, process families +
    processes, authorized systems/resources, and ONLY persisted
    relationships (process links + performer participation). Every section
    can be honestly empty; nothing is inferred beyond what discovery stored.
    """
    from app.models.enterprise_process import (
        BusinessCapability, BusinessProcess, EnterpriseResource, OrgUnit,
        ProcessActivity, ProcessLink,
    )
    from sqlalchemy import func

    units = [
        {"id": u.id, "name": u.name, "unit_type": u.unit_type,
         "evidence_status": u.evidence_status,
         "responsibilities": u.responsibilities}
        for u in db.query(OrgUnit)
        .filter(OrgUnit.org_id == org_id, OrgUnit.is_deleted == False)  # noqa: E712
        .order_by(OrgUnit.name).all()
    ]
    capabilities = [
        {"id": c.id, "name": c.name, "description": c.description,
         "evidence_status": c.evidence_status}
        for c in db.query(BusinessCapability)
        .filter(BusinessCapability.org_id == org_id,
                BusinessCapability.is_deleted == False)  # noqa: E712
        .order_by(BusinessCapability.name).all()
    ]
    procs = (
        db.query(BusinessProcess)
        .filter(BusinessProcess.org_id == org_id,
                BusinessProcess.is_deleted == False)  # noqa: E712
        .order_by(BusinessProcess.process_family.asc().nullslast(),
                  BusinessProcess.name.asc())
        .all()
    )
    ids = [p.id for p in procs]
    act_counts: dict = {}
    performers: dict = {}
    if ids:
        for a in db.query(ProcessActivity).filter(
                ProcessActivity.org_id == org_id,
                ProcessActivity.process_id.in_(ids),
                ProcessActivity.is_deleted == False).all():  # noqa: E712
            act_counts[a.process_id] = act_counts.get(a.process_id, 0) + 1
            if a.performer:
                performers.setdefault(a.process_id, set()).add(a.performer)
    name_by_id = {p.id: p.name for p in procs}
    links = []
    if ids:
        for l in db.query(ProcessLink).filter(
                ProcessLink.org_id == org_id,
                ProcessLink.predecessor_id.in_(ids),
                ProcessLink.is_deleted == False).all():  # noqa: E712
            if l.successor_id not in name_by_id:
                continue  # cross-org or deleted peer — never fabricate
            links.append({
                "predecessor_id": l.predecessor_id,
                "successor_id": l.successor_id,
                "predecessor_name": name_by_id.get(l.predecessor_id),
                "successor_name": name_by_id.get(l.successor_id),
                "handoff_note": l.handoff_note,
                "evidence_status": l.evidence_status,
            })
    processes = [
        {"id": p.id, "name": p.name, "process_family": p.process_family,
         "discovery_status": p.discovery_status,
         "evidence_status": p.evidence_status,
         "activities_count": act_counts.get(p.id, 0),
         "responsible_units": sorted(performers.get(p.id, set()))}
        for p in procs
    ]
    fam_counts = [
        {"family": f or None, "count": c}
        for f, c in db.query(BusinessProcess.process_family,
                             func.count(BusinessProcess.id))
        .filter(BusinessProcess.org_id == org_id,
                BusinessProcess.is_deleted == False)  # noqa: E712
        .group_by(BusinessProcess.process_family)
        .order_by(func.count(BusinessProcess.id).desc()).all()
    ]
    systems = [
        {"id": r.id, "name": r.name, "resource_kind": r.resource_kind,
         "ref_id": r.ref_id, "evidence_status": r.evidence_status}
        for r in db.query(EnterpriseResource)
        .filter(EnterpriseResource.org_id == org_id,
                EnterpriseResource.is_deleted == False)  # noqa: E712
        .order_by(EnterpriseResource.resource_kind, EnterpriseResource.name)
        .limit(60).all()
    ]
    return {
        "org_units": units,
        "capabilities": capabilities,
        "processes": processes,
        "families": fam_counts,
        "systems": systems,
        "process_links": links,
    }
