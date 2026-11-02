"""Business Context Service — BCS foundation (Phase 2A, 2026-11-01).

The central runtime bridge between enterprise knowledge and Zhanlu's AI
agents. Assembles a BOUNDED, evidence-grounded, organization-safe context
payload from the authoritative stores:

- Organization / project / department context (department is CONTEXTUAL
  METADATA — never an authorization grant).
- Business processes + activities (EBPM) with verbatim evidence refs.
- Ontology objects + links (verified relationships only prioritized).
- Terminology (enabled glossary entries).
- BusinessRules — ONLY enabled rules are executable guidance; verified
  rules are informational; proposed rules are clearly labeled hypotheses.
  Rejected rules are never included.
- Governed metrics (MetricDefinition — names/semantics, not SQL).
- Authorized resource references (project's own KBs — ids/names only,
  never credentials or backing data).

Security invariants:
- Everything is org-scoped (hard wall).
- Project authorization goes through the EXISTING ``project_access``
  machinery (``authorize_project_reference``); an unauthorized or missing
  project is honestly omitted with a warning, never guessed.
- ``ResourceAccessPolicy`` remains authoritative for resources; BCS only
  references resources the project already owns.
- Missing/insufficient knowledge is represented as explicit gaps
  (``unavailable`` reasons / ``warnings``), never fabricated.

Ranking: simple token-overlap relevance vs. the query (+ project/department
name boost), then a fixed authority order (verified rules → verified
processes → ontology → terminology → evidence → labeled proposals), then
a hard token budget using the existing compaction ``estimate_text_tokens``
utility. The enterprise graph is never dumped wholesale.

NOT in this foundation (Phase 2B+): runtime integration into
``get_system_prompt`` / ``prepare_data_source_runtime``, cross-entity
semantic caching, per-user department visibility rules. No new discovery,
no new ontology engine, no NL2SQL changes.
"""

from __future__ import annotations

import logging

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.services.compaction.token_estimator import estimate_text_tokens

logger = logging.getLogger(__name__)

DEFAULT_BUDGET_TOKENS = 2000
MAX_PROCESSES = 3
MAX_ACTIVITIES_PER_PROCESS = 6
MAX_OBJECTS = 5
MAX_TERMS = 5
MAX_RULES = 6
MAX_PROPOSED_RULES = 2
MAX_METRICS = 3

# Authority order (lower = included first)
_PRIO_RULE_ENABLED = 0
_PRIO_PROCESS = 1
_PRIO_ONTOLOGY = 2
_PRIO_TERM = 3
_PRIO_RULE_INFORMATIONAL = 4
_PRIO_RULE_PROPOSED = 5


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if len(t) > 2}


def _score(text: str | None, q_tokens: set[str], boost: set[str] | None = None) -> int:
    toks = _tokens(text)
    score = len(toks & q_tokens) if q_tokens else 0
    if boost:
        score += 2 * len(toks & boost)
    return score


def _org_unit_chain(db: Session, unit) -> list[str]:
    """Name-based OrgUnit hierarchy (parent_name chain), cycle-guarded."""
    from app.models.enterprise_process import OrgUnit

    chain, seen = [unit.name], {unit.name}
    parent_name = unit.parent_name
    for _ in range(10):
        if not parent_name or parent_name in seen:
            break
        parent = db.query(OrgUnit).filter(
            OrgUnit.org_id == unit.org_id, OrgUnit.name == parent_name,
            OrgUnit.is_deleted == False,  # noqa: E712
        ).first()
        if parent is None:
            break
        chain.append(parent.name)
        seen.add(parent.name)
        parent_name = parent.parent_name
    return chain


def _department_context(db: Session, org_id: str, project) -> dict | None:
    """Department of the project's authoritative OrgUnit link — context
    only. Returns None when unlinked (honest absence, never invented)."""
    unit_id = getattr(project, "department_org_unit_id", None)
    if not unit_id:
        return None
    from app.models.enterprise_process import OrgUnit

    unit = db.query(OrgUnit).filter(
        OrgUnit.id == unit_id, OrgUnit.org_id == org_id,
        OrgUnit.is_deleted == False,  # noqa: E712
    ).first()
    if unit is None:
        return None
    return {"id": unit.id, "name": unit.name,
            "unit_type": unit.unit_type, "hierarchy": _org_unit_chain(db, unit)}


def _project_section(db: Session, user, org_id: str,
                     project_id: str | None) -> dict:
    """Authorized project context via the EXISTING project_access wall."""
    section: dict = {"project_id": project_id, "authorized": False,
                     "department": None, "resources": []}
    if not project_id:
        return section
    from fastapi import HTTPException

    from app.services.project_access import authorize_project_reference

    try:
        # Existing project_access wall: raises 404 on unauthorized
        # existing projects, returns None on missing/deleted.
        project = authorize_project_reference(db, user, project_id)
    except HTTPException as exc:
        section["unavailable_reason"] = (
            f"project not authorized ({exc.status_code})")
        return section
    if project is None:
        section["unavailable_reason"] = (
            "project not found, deleted, or not authorized for this user")
        return section
    section.update({
        "authorized": True,
        "name": project.name,
        "description": project.description,
        "department": _department_context(db, org_id, project),
    })
    # Authorized resource references — the project's OWN knowledge bases
    # (org-scoped, home-pointer). Ids/names only; never credentials,
    # connection strings or backing rows. ResourceAccessPolicy remains
    # authoritative for any actual resource access.
    try:
        from app.models.knowledge_base import KnowledgeBase

        kbs = db.query(KnowledgeBase).filter(
            KnowledgeBase.org_id == org_id,
            KnowledgeBase.project_id == project.id,
            KnowledgeBase.is_deleted == False,  # noqa: E712
        ).all()
        section["resources"] = [
            {"kind": "knowledge_base", "id": kb.id, "name": kb.name,
             "source_kind": kb.source_kind} for kb in kbs
        ]
    except Exception as exc:  # pragma: no cover - fail-open metadata
        logger.debug("bcs: resource refs unavailable: %s", exc)
        section["resources_unavailable_reason"] = str(exc)[:200]
    return section


def _process_candidates(db: Session, org_id: str, q_tokens: set[str],
                        boost: set[str]) -> list[dict]:
    from app.models.enterprise_process import (
        BusinessProcess, ProcessActivity, ProcessEvidence)

    rows = db.query(BusinessProcess).filter(
        BusinessProcess.org_id == org_id,
        BusinessProcess.is_deleted == False,  # noqa: E712
        # Phase 7C (T19): a human REJECTED this process — it must never
        # enter AI execution context (invariant 8), labeled or not.
        or_(BusinessProcess.evidence_status.is_(None),
            BusinessProcess.evidence_status != "rejected"),
    ).all()
    scored = []
    for p in rows:
        s = _score(" ".join(filter(None, [p.name, p.purpose, p.objective,
                                          p.trigger_event])), q_tokens, boost)
        verified = p.evidence_status == "verified"
        scored.append(((-int(verified), -s), p))
    scored.sort(key=lambda t: t[0])
    out = []
    for _, p in scored[:MAX_PROCESSES]:
        activities = db.query(ProcessActivity).filter(
            ProcessActivity.org_id == org_id,
            ProcessActivity.process_id == p.id,
            ProcessActivity.is_deleted == False,  # noqa: E712
        ).order_by(ProcessActivity.seq.asc()).limit(MAX_ACTIVITIES_PER_PROCESS).all()
        evidence = db.query(ProcessEvidence).filter(
            ProcessEvidence.org_id == org_id,
            ProcessEvidence.process_id == p.id,
            ProcessEvidence.is_deleted == False,  # noqa: E712
        ).limit(5).all()
        out.append({
            "id": p.id, "name": p.name, "purpose": p.purpose,
            "family": p.process_family,
            "evidence_status": p.evidence_status,
            "discovery_status": p.discovery_status,
            "activities": [{"seq": a.seq, "name": a.name,
                            "type": a.activity_type} for a in activities],
            "evidence": [{"kind": e.evidence_kind, "ref": e.source_ref}
                         for e in evidence],
        })
    return out


def _ontology_candidates(db: Session, org_id: str, q_tokens: set[str],
                         boost: set[str]) -> tuple[list[dict], list[dict]]:
    from app.models.ontology import OntologyLink, OntologyObject

    rows = db.query(OntologyObject).filter(
        OntologyObject.org_id == org_id,
        OntologyObject.is_deleted == False,  # noqa: E712
    ).all()
    scored = []
    for o in rows:
        s = _score(" ".join(filter(None, [o.name, o.display_name])),
                   q_tokens, boost)
        verified = o.evidence_status == "verified" if hasattr(
            o, "evidence_status") else False
        scored.append(((-int(verified), -s), o))
    scored.sort(key=lambda t: t[0])
    selected = scored[:MAX_OBJECTS]
    objects = [{
        "id": o.id, "name": o.name, "display_name": o.display_name,
        "backing_kind": o.backing_kind,
        "evidence_status": getattr(o, "evidence_status", None),
        # backing_ref is intentionally NOT exposed (never leak table/entity
        # identity into prompts; runtime resolvers own that mapping)
    } for _, o in selected]
    ids = {o["id"] for o in objects}
    by_id = {o["id"]: o for o in objects}
    links = db.query(OntologyLink).filter(
        OntologyLink.org_id == org_id,
        OntologyLink.is_deleted == False,  # noqa: E712
    ).all()
    rel = []
    for l in links:
        if l.from_object_id not in ids or l.to_object_id not in ids:
            continue
        status = l.evidence_status
        if status == "rejected":
            # Phase 7B: rejected relationships are NEVER included — a human
            # declined this edge and rediscovery does not resurrect it.
            continue
        entry = {"from": by_id[l.from_object_id]["name"],
                 "to": by_id[l.to_object_id]["name"],
                 "type": l.link_type, "evidence_status": status}
        if status != "verified":
            # verified relationships are facts; everything else is an
            # explicitly labeled hypothesis
            entry["hypothesis"] = True
        rel.append(entry)
    return objects, rel


def _terminology_candidates(db: Session, org_id: str,
                            q_tokens: set[str]) -> list[dict]:
    from app.models.terminology import Terminology

    rows = db.query(Terminology).filter(
        Terminology.org_id == org_id,
        Terminology.is_deleted == False,  # noqa: E712
        Terminology.enabled.is_(True),
    ).all()
    scored = sorted(
        rows,
        key=lambda t: -_score(f"{t.word} {t.description or ''}", q_tokens))
    return [{"id": t.id, "word": t.word, "description": t.description}
            for t in scored[:MAX_TERMS]]


def _rule_candidates(db: Session, org_id: str, q_tokens: set[str],
                     boost: set[str],
                     agent_bound_kb_ids: set[str] | None = None) -> list[dict]:
    from app.models.business_rule import BusinessRule

    rows = db.query(BusinessRule).filter(
        BusinessRule.org_id == org_id,
        BusinessRule.is_deleted == False,  # noqa: E712
        BusinessRule.status.in_(("enabled", "verified", "proposed")),
    ).all()
    scored = []
    for r in rows:
        # Phase 7C (F2): datasource-scoped rules narrow, never widen — when
        # the calling agent's bound KBs are known, a rule scoped to other
        # datasources does not enter this agent's context. Org-wide rules
        # (NULL/empty datasource_ids) always pass; with NO agent scoping
        # available, previous behavior is preserved (never widen by
        # removing guidance either).
        if agent_bound_kb_ids is not None:
            rule_ds = {str(d) for d in (r.datasource_ids or [])}
            if rule_ds and not (rule_ds & agent_bound_kb_ids):
                continue
        s = _score(f"{r.statement} {r.scope}", q_tokens, boost)
        prio = {"enabled": _PRIO_RULE_ENABLED, "verified": _PRIO_RULE_INFORMATIONAL,
                "proposed": _PRIO_RULE_PROPOSED}[r.status]
        scored.append(((prio, -s), r))
    scored.sort(key=lambda t: t[0])
    out, proposed = [], 0
    for (prio, _), r in scored:
        if r.status == "proposed":
            if proposed >= MAX_PROPOSED_RULES:
                continue
            proposed += 1
        out.append({
            "id": r.id, "statement": r.statement, "status": r.status,
            "scope": r.scope, "scope_ref_id": r.scope_ref_id,
            "applies_to": r.applies_to,
            "source_evidence_ids": list(r.source_evidence_ids or []),
        })
        if len(out) >= MAX_RULES:
            break
    return out


def _metric_candidates(db: Session, org_id: str, q_tokens: set[str]) -> list[dict]:
    from app.models.metric_definition import MetricDefinition

    rows = db.query(MetricDefinition).filter(
        MetricDefinition.org_id == org_id,
        MetricDefinition.is_deleted == False,  # noqa: E712
        # Phase 7C (T19): parity with the NL2SQL planning path — only
        # enabled, published metrics inform context (verified ≠ enabled;
        # a draft metric's name/semantics are not yet governed).
        MetricDefinition.is_enabled.is_(True),
        MetricDefinition.lifecycle == "published",
    ).all()
    scored = sorted(
        rows,
        key=lambda m: -_score(" ".join(filter(None, [
            m.name, m.display_name, m.description])), q_tokens))
    # Names/semantics only — the SQL definition stays in NL2SQL's store.
    return [{"id": m.id, "name": m.name, "display_name": m.display_name,
             "description": m.description, "unit": m.unit,
             "category": m.category} for m in scored[:MAX_METRICS]]


def build_business_context(
    db: Session, user, org_id: str, *, project_id: str | None = None,
    agent_id: str | None = None, query: str | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> dict:
    """Assemble bounded, evidence-grounded, org-safe business context.

    Returns the structured payload described in the target architecture
    §BCS contract. Every section fails open individually (warning +
    ``unavailable`` reason) — a broken section never fabricates content
    nor blocks the other sections.
    """
    warnings: list[str] = []
    q_tokens = _tokens(query)
    org_name = None
    try:
        from app.models.organization import Organization

        org_name = db.query(Organization).filter(
            Organization.id == org_id).first().name
    except Exception:
        org_name = None

    project = _project_section(db, user, org_id, project_id)
    boost: set[str] = set()
    if project.get("authorized"):
        boost = _tokens(project.get("name") or "") | _tokens(
            (project.get("department") or {}).get("name") or "")

    # Phase 7C (F2): resolve the calling agent's bound KBs so datasource-
    # scoped rules narrow to this agent's actual reach. Narrow-only: any
    # resolution failure falls back to the previous unscoped behavior.
    agent_bound_kb_ids: set[str] | None = None
    if agent_id:
        try:
            from app.models.agent_app import AgentApp

            agent_row = (
                db.query(AgentApp)
                .filter(AgentApp.id == agent_id,
                        AgentApp.org_id == org_id,
                        AgentApp.is_deleted == False)  # noqa: E712
                .first()
            )
            if agent_row and isinstance(agent_row.knowledge_bases, list):
                agent_bound_kb_ids = {str(k) for k in agent_row.knowledge_bases}
        except Exception as exc:  # honest, narrow-only fallback
            logger.warning("bcs: agent binding resolution failed: %s", exc)
            agent_bound_kb_ids = None

    processes, objects, links, terms, rules, metrics = [], [], [], [], [], []
    for name, fn in (
        ("processes", lambda: _process_candidates(db, org_id, q_tokens, boost)),
        ("ontology", lambda: _ontology_candidates(db, org_id, q_tokens, boost)),
        ("terminology", lambda: _terminology_candidates(db, org_id, q_tokens)),
        ("business_rules", lambda: _rule_candidates(
            db, org_id, q_tokens, boost,
            agent_bound_kb_ids=agent_bound_kb_ids)),
        ("metrics", lambda: _metric_candidates(db, org_id, q_tokens)),
    ):
        try:
            result = fn()
        except Exception as exc:  # honest per-section failure
            logger.warning("bcs: section %s unavailable: %s", name, exc)
            warnings.append(f"section_unavailable:{name}")
            continue
        if name == "ontology":
            objects, links = result
        elif name == "business_rules":
            rules = result
        elif name == "processes":
            processes = result
        elif name == "terminology":
            terms = result
        else:
            metrics = result

    # --- bounded selection over a uniform item stream ----------------------
    # (priority, -score, section, id, label, text, evidence)
    items: list[tuple[int, int, str, str, str, str, str]] = []
    for r in rules:
        prio = {"enabled": _PRIO_RULE_ENABLED,
                "verified": _PRIO_RULE_INFORMATIONAL,
                "proposed": _PRIO_RULE_PROPOSED}[r["status"]]
        evidence = "evidence" if r["source_evidence_ids"] else ""
        items.append((prio, -_score(r["statement"], q_tokens, boost),
                      "business_rules", r["id"], r["status"],
                      r["statement"], evidence))
    for p in processes:
        evidence = "evidence" if p["evidence"] else ""
        text = p["name"] + (f" — {p['purpose']}" if p["purpose"] else "")
        items.append((_PRIO_PROCESS, -_score(text, q_tokens, boost),
                      "processes", p["id"], p["evidence_status"], text,
                      evidence))
    for o in objects:
        items.append((_PRIO_ONTOLOGY,
                      -_score(" ".join(filter(None, [o["name"], o["display_name"]])),
                              q_tokens, boost),
                      "ontology", o["id"], "object", o["name"], ""))
    for t in terms:
        items.append((_PRIO_TERM, -_score(f"{t['word']} {t['description'] or ''}",
                                          q_tokens),
                      "terminology", t["id"], "term",
                      f"{t['word']}: {t['description'] or ''}", ""))
    for m in metrics:
        items.append((_PRIO_TERM, -_score(" ".join(filter(None, [
            m["name"], m["description"]])), q_tokens),
            "metrics", m["id"], "metric", m["name"], ""))

    items.sort(key=lambda it: (it[0], it[1]))
    included: list[dict] = []
    kept_ids: dict[str, set[str]] = {}
    omitted = 0
    estimated = 0
    for prio, _s, section, item_id, label, text, evidence in items:
        cost = estimate_text_tokens(text)
        if estimated + cost > budget_tokens and included:
            omitted += 1
            continue
        estimated += cost
        included.append({"section": section, "id": item_id, "label": label,
                         "text": text, "evidence": evidence})
        kept_ids.setdefault(section, set()).add(item_id)

    # Payload sections carry ONLY what survived the budget (plus ontology
    # links whose both ends survived).
    selected_processes = [p for p in processes
                          if p["id"] in kept_ids.get("processes", set())]
    selected_objects = [o for o in objects
                        if o["id"] in kept_ids.get("ontology", set())]
    obj_ids = {o["id"] for o in selected_objects}
    selected_links = [l for l in links
                      if l and l["from"] in {o["name"] for o in selected_objects}
                      and l["to"] in {o["name"] for o in selected_objects}]
    selected_terms = [t for t in terms
                      if t["id"] in kept_ids.get("terminology", set())]
    selected_rules = [r for r in rules
                      if r["id"] in kept_ids.get("business_rules", set())]
    selected_metrics = [m for m in metrics
                        if m["id"] in kept_ids.get("metrics", set())]

    omitted_by_section: dict[str, int] = {}
    total_by_section: dict[str, int] = {}
    for _p, _s, section, _i, _l, _t, _e in items:
        total_by_section[section] = total_by_section.get(section, 0) + 1
    for section, total in total_by_section.items():
        omitted_by_section[section] = total - len(kept_ids.get(section, set()))

    evidence_backed = [it for it in included if it["evidence"]]
    coverage = (len(evidence_backed)
                / max(1, sum(1 for it in included
                             if it["section"] in ("business_rules", "processes"))))
    if not included and not project.get("authorized"):
        status = "unavailable"
    elif omitted or warnings:
        status = "partial"
    else:
        status = "complete"

    return {
        "organization": {"id": org_id, "name": org_name},
        "project": project,
        "department": project.get("department"),
        "processes": selected_processes,
        "ontology": {"objects": selected_objects, "links": selected_links},
        "terminology": selected_terms,
        "business_rules": selected_rules,
        "metrics": selected_metrics,
        "authorization_summary": {
            "org_isolated": True,
            "project_authorized": project.get("authorized", False),
            "department_is_context_only": True,
            "resource_policy": "ResourceAccessPolicy remains authoritative",
        },
        "token_budget": {
            "estimated_tokens": estimated,
            "budget_tokens": budget_tokens,
            "included_items": len(included),
            "omitted_items": omitted,
            "omitted_by_section": omitted_by_section,
        },
        "evidence_coverage": round(min(1.0, coverage), 2),
        "status": status,
        "warnings": warnings,
    }


def build_ontology_planning_context(
    db: Session, org_id: str, *, query: str | None = None,
    max_objects: int = 12, datasource_id: str | None = None,
) -> dict:
    """Phase 3A — ontology-aware NL2SQL PLANNING context (internal data,
    NOT a prompt block). Org-scoped, read-only:

    - objects: verified ontology objects first (name + backing TABLE when
      ``backing_kind='table'``), then proposed ones explicitly labeled
      ``hypothesis``; rejected/orphaned rows are never included.
    - links: relationships between the selected objects — VERIFIED links
      are facts; proposed/inferred ones are labeled hypotheses and must
      never become executable joins.
    - terms: enabled terminology ranked by query relevance (aliases for
      candidate retrieval).
    - enabled_sql_rules: ONLY ``status='enabled'`` AND
      ``applies_to='sql'`` rules (proposed/verified-not-enabled/rejected
      are excluded — they cannot guide SQL). Phase 3B: further scoped to
      ``datasource_id`` when supplied — rules listing other datasources
      do not apply; org-wide rules always do.
    - metrics: published MetricDefinitions (names/display/description/
      synonyms + base_table ONLY — never base_sql), ranked by query
      relevance. Missing table mappings degrade honestly (no boost).

    Ranking uses the same token-overlap approach as build_business_context.
    The caller (table retrieval) decides how to use this; nothing here
    invents joins or fabricates citations.
    """
    q_tokens = _tokens(query)
    from app.models.business_rule import BusinessRule
    from app.models.ontology import OntologyLink, OntologyObject
    from app.models.terminology import Terminology

    def _ranked(rows, text_fn):
        return sorted(
            rows, key=lambda r: -_score(text_fn(r), q_tokens)) if q_tokens \
            else list(rows)

    objects: list[dict] = []
    obj_rows = db.query(OntologyObject).filter(
        OntologyObject.org_id == org_id,
        OntologyObject.is_deleted == False,  # noqa: E712
    ).all()
    verified, hypo = [], []
    for o in obj_rows:
        status = getattr(o, "evidence_status", None) or "proposed"
        backing_table = None
        if getattr(o, "backing_kind", None) == "table" and getattr(
                o, "backing_ref", None):
            # backing_ref format: kb:<kb_id>:<table> — split(":", 2) so the
            # table name (which may contain colons... it may not) survives
            parts = str(o.backing_ref).split(":", 2)
            if len(parts) == 3:
                backing_table = parts[2]
        entry = {"id": o.id, "name": o.name,
                 "display_name": o.display_name, "domain_id": o.domain_id,
                 "evidence_status": status, "backing_table": backing_table}
        if status == "verified":
            verified.append(entry)
        elif status in ("proposed", "inferred"):
            entry["hypothesis"] = True
            hypo.append(entry)
        # anything else (rejected/orphaned) is excluded entirely
    text_of = lambda e: f"{e['name']} {e.get('display_name') or ''}"  # noqa: E731
    objects = [_ranked(verified, text_of)[:max_objects],
               _ranked(hypo, text_of)[:max(2, max_objects // 4)]]

    sel = objects[0] + objects[1]
    by_id = {e["id"]: e for e in sel}
    links: list[dict] = []
    link_rows = db.query(OntologyLink).filter(
        OntologyLink.org_id == org_id,
        OntologyLink.is_deleted == False,  # noqa: E712
    ).all()
    for l in link_rows:
        a, b = by_id.get(getattr(l, "from_object_id")), \
            by_id.get(getattr(l, "to_object_id"))
        if not a or not b:
            continue
        status = getattr(l, "evidence_status", None) or "proposed"
        if status == "rejected":
            # Phase 7B: rejected relationships are NEVER included — a human
            # declined this edge and rediscovery does not resurrect it.
            continue
        entry = {
            "from": a["name"], "to": b["name"], "type": l.link_type,
            "from_table": a.get("backing_table"),
            "to_table": b.get("backing_table"),
            "evidence_status": status,
        }
        if status == "verified":
            links.append(entry)
        else:
            entry["hypothesis"] = True
            links.append(entry)

    terms = [
        {"id": t.id, "word": t.word, "description": t.description}
        for t in _ranked(
            db.query(Terminology).filter(
                Terminology.org_id == org_id,
                Terminology.is_deleted == False,  # noqa: E712
                Terminology.enabled.is_(True),
            ).all(),
            lambda t: f"{t.word} {t.description or ''}")[:8]
    ]
    # Phase 3B: rules are additionally scoped to the datasource being
    # queried. ``datasource_ids`` NULL/empty = org-wide (existing scope);
    # a scoped rule applies ONLY to its listed datasources. Scoping can
    # only NARROW — it never widens access beyond the org filter above,
    # and the caller passes the id of the datasource it already owns.
    rules = []
    for r in db.query(BusinessRule).filter(
            BusinessRule.org_id == org_id,
            BusinessRule.is_deleted == False,  # noqa: E712
            BusinessRule.status == "enabled",
            BusinessRule.applies_to == "sql",
    ).all():
        ds_ids = list(r.datasource_ids or [])
        if ds_ids and datasource_id and \
                str(datasource_id) not in {str(d) for d in ds_ids}:
            continue
        rules.append({"id": r.id, "statement": r.statement, "scope": r.scope,
                      "scope_ref_id": r.scope_ref_id,
                      "datasource_ids": ds_ids})
    # Phase 3B: metric definitions as ranking evidence. NAMES/SEMANTICS
    # only — ``base_sql``/``filters_json`` stay out of the planning
    # context so deterministic metric computation is never bypassed and
    # the LLM can never execute a metric itself.
    metrics = []
    try:
        from app.models.metric_definition import MetricDefinition

        def _m_text(m):
            return " ".join(filter(None, [
                m.name, m.display_name, m.description,
                " ".join(m.synonyms or [])]))

        for m in _ranked(
                db.query(MetricDefinition).filter(
                    MetricDefinition.org_id == org_id,
                    MetricDefinition.is_deleted == False,  # noqa: E712
                    MetricDefinition.is_enabled.is_(True),
                    MetricDefinition.lifecycle == "published",
                ).all(),
                _m_text)[:6]:
            metrics.append({
                "id": m.id, "name": m.name,
                "display_name": m.display_name,
                "description": m.description,
                "synonyms": list(m.synonyms or []),
                "base_table": m.base_table,
                "evidence_status": getattr(m, "evidence_status",
                                           None) or "verified"})
    except Exception as e:  # per-section fail-open
        logger.warning("ontology planning: metric section unavailable: %s", e)
        metrics = []
    return {
        "org_id": org_id,
        "objects_verified": objects[0],
        "objects_hypotheses": objects[1],
        "links": links,
        "terms": terms,
        "enabled_sql_rules": rules,
        "metrics": metrics,
        "provenance": {
            "sources": ["ontology_objects", "ontology_links",
                        "terminologies", "business_rules",
                        "metric_definitions"],
            "hypotheses_labeled": True,
        },
    }


def render_prompt_block(context: dict) -> str:
    """Render the context as a labeled prompt block. Tiers are EXPLICIT:
    verified facts, executable rules, informational rules, proposed
    hypotheses and unavailable gaps are never mixed — hypotheses are
    never converted into facts."""
    lines: list[str] = ["# Business Context"]
    org = context.get("organization") or {}
    if org.get("name"):
        lines.append(f"Organization: {org['name']}")
    project = context.get("project") or {}
    if project.get("authorized"):
        lines.append(f"Project: {project.get('name')}")
        dept = project.get("department")
        if dept:
            lines.append(f"Department (context only, not an access grant): "
                         f"{' > '.join(dept['hierarchy'])}")
        for res in project.get("resources") or []:
            lines.append(f"Authorized resource: {res['kind']} '{res['name']}'")
    elif project.get("project_id"):
        lines.append(f"Project: unavailable — "
                     f"{project.get('unavailable_reason', 'not authorized')}")

    verified, enabled, informational, proposed = [], [], [], []
    for r in context.get("business_rules") or []:
        if r["status"] == "enabled":
            enabled.append(f"- {r['statement']} (applies_to: {r['applies_to']})")
        elif r["status"] == "verified":
            informational.append(f"- {r['statement']}")
        else:
            proposed.append(f"- {r['statement']}")

    processes = context.get("processes") or []
    verified_processes = [p for p in processes
                          if p.get("evidence_status") == "verified"]
    other_processes = [p for p in processes
                       if p.get("evidence_status") != "verified"]

    if verified_processes or (context.get("ontology") or {}).get("links") \
            or context.get("metrics") or context.get("terminology"):
        lines.append("\n## Verified facts")
        for p in verified_processes:
            acts = ", ".join(a["name"] for a in p.get("activities") or [])
            lines.append(f"- Process: {p['name']}"
                         + (f" (steps: {acts})" if acts else ""))
        links = (context.get("ontology") or {}).get("links") or []
        for l in links:
            lines.append(f"- Relationship: {l['from']} --{l['type']}--> "
                         f"{l['to']}")
        for t in context.get("terminology") or []:
            lines.append(f"- Term: {t['word']}: {t['description'] or ''}")
        for m in context.get("metrics") or []:
            lines.append(f"- Metric: {m['name']}"
                         + (f" ({m['unit']})" if m.get("unit") else ""))
    if other_processes:
        lines.append("\n## Discovered processes (not yet human-verified — "
                     "proposals, not facts)")
        for p in other_processes:
            lines.append(f"- {p['name']} [{p['evidence_status']}]")
    if enabled:
        lines.append("\n## Enabled business rules (executable guidance)")
        lines.extend(enabled)
    if informational:
        lines.append("\n## Verified but NOT enabled rules (informational "
                     "only — do not act on them)")
        lines.extend(informational)
    if proposed:
        lines.append("\n## Proposed (UNVERIFIED — hypotheses, never facts)")
        lines.extend(proposed)
    objects = (context.get("ontology") or {}).get("objects") or []
    if objects and not (context.get("ontology") or {}).get("links"):
        lines.append("\n## Known business objects (no verified relationships "
                     "available)")
        lines.extend(f"- {o['name']}" for o in objects)

    warnings = context.get("warnings") or []
    tb = context.get("token_budget") or {}
    if warnings or tb.get("omitted_items") or context.get("status") != "complete":
        lines.append("\n## Unavailable / truncated (honest gaps)")
        for w in warnings:
            lines.append(f"- {w}")
        if tb.get("omitted_items"):
            lines.append(f"- {tb['omitted_items']} lower-relevance items "
                         f"omitted (token budget "
                         f"{tb.get('estimated_tokens')}/{tb.get('budget_tokens')})")
        if project.get("authorized") is False and project.get("project_id"):
            lines.append("- Project context unavailable (unauthorized)")
    return "\n".join(lines)
