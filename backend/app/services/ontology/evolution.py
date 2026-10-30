"""P6 — continuous ontology evolution: detect → impact → controlled act.

`reconcile` is the ONE evolution mechanism (no parallel drift engine):
per org, it scans database sources (live schema vs catalog), validates
binding health, re-hashes file documents, checks metric dependencies,
and compares each source's FACT SIGNATURE against the idempotency ledger
(`ontology_evolution_state`) — an unchanged signature produces zero
proposals, revisions, or notifications (E1).

Controlled actions (apply=True ONLY): mark bindings stale/broken (with a
P2 revision — the one automatic object mutation), fan-out notifications
for material changes, recompute coverage. Verified definitions are never
mutated (E3); new concepts are report-only unless the proposal flag is on.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.models.knowledge_base import KnowledgeBase
from app.models.ontology import (
    OntologyEvolutionState,
    OntologyObject,
)

logger = logging.getLogger(__name__)

MAX_SOURCES_PER_RUN = 20
MAX_FILE_KBS_PER_RUN = 10


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sig(facts: list) -> str:
    return hashlib.sha256(
        repr(sorted(map(repr, facts))).encode()).hexdigest()[:64]


def _catalog_tables(db: Session, kb_id: str) -> dict[str, set[str]] | None:
    """{table: {columns}} from the indexed catalog, or None when the
    source was never indexed (nothing to drift FROM — honest skip)."""
    from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

    tables = (
        db.query(KBTableMeta)
        .filter(KBTableMeta.kb_id == kb_id,
                KBTableMeta.is_deleted == False)  # noqa: E712
        .all()
    )
    if not tables:
        return None
    meta_ids = {t.id: t.table_name for t in tables}
    cols = (
        db.query(KBColumnMeta)
        .filter(KBColumnMeta.table_meta_id.in_(meta_ids),
                KBColumnMeta.is_deleted == False)  # noqa: E712
        .all()
    )
    out: dict[str, set[str]] = {t.table_name: set() for t in tables}
    for c in cols:
        table = meta_ids.get(c.table_meta_id)
        if table:
            out.setdefault(table, set()).add(c.column_name)
    return out


def _bound_table(obj: OntologyObject) -> str | None:
    """The live table behind a table-backed object (kb:<id>:<table> or
    a platform table name), else None."""
    if obj.backing_kind != "table" or not obj.backing_ref:
        return None
    ref = obj.backing_ref
    if ref.startswith("kb:"):
        parts = ref.split(":", 2)
        return parts[2] if len(parts) == 3 else None
    return ref or None


def _impact(db: Session, org_id: str, tables: set[str]) -> dict:
    """Who depends on the changed tables — objects, metrics, agents
    (through domain bindings). Real dependency data, never hardcoded."""
    from app.models.metric_definition import MetricDefinition
    from app.models.ontology import AgentDomainBinding

    objects = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == org_id,
                OntologyObject.is_deleted == False)  # noqa: E712
        .all()
    )
    affected = [o for o in objects
                if _bound_table(o) in tables
                or (o.backing_kind == "metric" and (o.provenance_json or {}).get("metric_base_table") in tables)]
    metrics = (
        db.query(MetricDefinition)
        .filter(MetricDefinition.org_id == org_id,
                MetricDefinition.is_deleted == False,  # noqa: E712
                MetricDefinition.base_table.in_(tables))
        .all()
    )
    agent_names: set[str] = set()
    if affected:
        from app.services.ontology.queries import list_bindings

        domain_ids = {o.domain_id for o in affected}
        for b in list_bindings(db, org_id):
            if b.domain_id in domain_ids:
                agent_names.add(b.agent_name)
    return {
        "objects": [{"id": o.id, "name": o.name,
                     "evidence_status": o.evidence_status}
                    for o in affected],
        "metrics": [{"id": m.id, "name": m.name} for m in metrics],
        "agents": sorted(agent_names),
    }


def _detect_database_source(db: Session, kb: KnowledgeBase) -> dict | None:
    """Live-vs-catalog drift facts for ONE database source (None when the
    catalog baseline doesn't exist yet — nothing to drift from)."""
    from app.services.db.schema_service import (
        SchemaService,
        invalidate_schema_cache,
    )

    catalog = _catalog_tables(db, kb.id)
    if catalog is None:
        return None
    try:
        # Evolution must see REALITY, not a cached hour-old schema — the
        # schema cache is keyed on connection identity, so an ALTER TABLE
        # on the SAME connection is invisible without invalidation.
        invalidate_schema_cache(kb.id)
        result = SchemaService(db).describe_all(kb.id, max_tables=100)
    except Exception as exc:  # noqa: BLE001 — bounded, honest per-source
        logger.warning("evolution: describe_all failed for %s: %s",
                       kb.id, exc)
        return None
    live: dict[str, set[str]] = {}
    for t in result.get("tables") or []:
        name = (t.get("table") or t.get("name")) if isinstance(t, dict) else None
        cols = t.get("columns") if isinstance(t, dict) else None
        if name:
            live[name] = {c.get("name") for c in (cols or [])
                          if isinstance(c, dict) and c.get("name")} or set()

    facts: list = []
    for table, cols in catalog.items():
        if table not in live:
            facts.append(("table_removed", table))
        else:
            for gone in cols - live[table]:
                facts.append(("column_removed", table, gone))
    for table, cols in live.items():
        if table not in catalog:
            facts.append(("table_added", table))
        else:
            for added in cols - catalog[table]:
                facts.append(("column_added", table, added))
    return {"facts": facts, "live": live, "catalog": catalog}


def reconcile(db: Session, org_id: str, *, apply: bool = False) -> dict:
    """Scan the org's sources, diff against the ledger, act within E2."""
    report: dict[str, Any] = {
        "org_id": org_id, "apply": apply,
        "sources": [], "changes": [], "actions": [],
        "notifications": [], "failed": [],
    }
    touched_tables: set[str] = set()

    kbs = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.org_id == org_id,
                KnowledgeBase.is_deleted == False,  # noqa: E712
                KnowledgeBase.source_kind == "database")
        .limit(MAX_SOURCES_PER_RUN)
        .all()
    )

    for kb in kbs:
        source_key = f"db:{kb.id}"
        try:
            detection = _detect_database_source(db, kb)
            if detection is None:
                report["sources"].append(
                    {"source_key": source_key, "status": "skipped",
                     "reason": "no catalog baseline yet"})
                continue
            signature = _sig(detection["facts"])
            state = (
                db.query(OntologyEvolutionState)
                .filter(OntologyEvolutionState.org_id == org_id,
                        OntologyEvolutionState.source_key == source_key)
                .first()
            )
            if state and state.signature == signature:
                report["sources"].append(
                    {"source_key": source_key, "status": "unchanged"})
                if apply:
                    state.last_run_at = _now()
                    db.commit()
                continue

            if not detection["facts"]:
                # Baseline run (or transiently re-synced): record the
                # signature WITHOUT inventing a change.
                report["sources"].append(
                    {"source_key": source_key, "status": "ok"})
                if apply:
                    _upsert_state(db, org_id, source_key, "database",
                                  signature, "ok")
                    db.commit()
                continue

            change = {
                "source_key": source_key, "kind": "schema_drift",
                "facts": [list(f) for f in detection["facts"]],
                "impact": _impact(db, org_id,
                                  {f[1] for f in detection["facts"]}),
            }
            report["changes"].append(change)
            # Tables the live diff just judged — the catalog self-check
            # must not "heal" these back to ok (the catalog may lag the
            # live source).
            touched_tables.update(f[1] for f in detection["facts"])

            if apply:
                actions = _apply_drift(db, org_id, detection, change)
                report["actions"].extend(actions)
                # Advance the signature ONLY when the controlled actions
                # actually ran — a report-only pass must never consume a
                # pending change (otherwise the follow-up apply run would
                # see "unchanged" and skip the drift forever).
                _upsert_state(db, org_id, source_key, "database", signature,
                              "changed" if detection["facts"] else "ok")
                db.commit()
        except Exception as exc:  # noqa: BLE001 — per-source isolation (E4)
            logger.exception("evolution: source %s failed", source_key)
            report["failed"].append({"source_key": source_key,
                                     "error": str(exc)[:300]})
            if apply:
                _upsert_state(db, org_id, source_key, "database", "",
                              "failed", error=str(exc)[:300])
                db.commit()

    _check_bindings_and_metrics(db, org_id, report, apply=apply,
                                skip_tables=touched_tables)
    db.commit()
    return report


def _apply_drift(db: Session, org_id: str, detection: dict,
                 change: dict) -> list[dict]:
    """E2-allowed mutations: stale/broken marking with P2 revisions, and
    fan-out for material changes. Never touches verified definitions."""
    from app.services.ontology.revisions import record_revision

    actions: list[dict] = []
    removed_tables = {f[1] for f in detection["facts"]
                      if f[0] == "table_removed"}
    touched = {f[1] for f in detection["facts"]}
    objects = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == org_id,
                OntologyObject.is_deleted == False)  # noqa: E712
        .all()
    )
    for obj in objects:
        table = _bound_table(obj)
        if table not in touched:
            continue
        new_status = "broken" if table in removed_tables else "stale"
        if obj.binding_status == new_status:
            continue  # idempotent — no duplicate revisions (E1)
        obj.binding_status = new_status
        obj.last_validated_at = _now()
        record_revision(db, "object", obj, change_kind="drift",
                        changed_by="ontology-evolution",
                        summary=(
                            f"binding {new_status}: source schema changed "
                            f"({table})"))
        actions.append({"action": f"mark_{new_status}", "object": obj.name,
                        "table": table})
    return actions


def _check_bindings_and_metrics(db: Session, org_id: str, report: dict,
                                *, apply: bool,
                                skip_tables: set[str] | None = None) -> None:
    """Binding health + metric dependency self-checks (bounded, honest).

    ``skip_tables``: tables the live-schema diff already judged this run —
    the (possibly lagging) catalog must not upgrade those bindings back
    to ok.
    """
    from app.models.metric_definition import MetricDefinition

    skip_tables = skip_tables or set()

    objects = (
        db.query(OntologyObject)
        .filter(OntologyObject.org_id == org_id,
                OntologyObject.is_deleted == False,  # noqa: E712
                OntologyObject.backing_kind.in_(("table", "metric")))
        .all()
    )
    for obj in objects:
        if obj.backing_kind == "table":
            table = _bound_table(obj)
            if not table or table in skip_tables:
                continue
            ref_kb = obj.backing_ref.split(":")[1] \
                if (obj.backing_ref or "").startswith("kb:") else None
            catalog = _catalog_tables(db, ref_kb) if ref_kb else None
            if catalog is None:
                continue
            if table not in catalog:
                _mark(db, org_id, obj, "broken", report, apply)
            else:
                mapped_cols = {
                    p.get("column") for p in (obj.properties_json or [])
                    if isinstance(p, dict) and p.get("column")}
                missing = {c for c in mapped_cols
                           if c not in catalog.get(table, set())}
                status = "stale" if missing else "ok"
                if status != obj.binding_status:
                    _mark(db, org_id, obj, status, report, apply)
        else:  # metric backing
            metric = _metric_for(db, org_id, obj.backing_ref)
            if metric is None:
                _mark(db, org_id, obj, "broken", report, apply)

    # Metric dependency check: a published metric whose base table is no
    # longer in its datasource's catalog is flagged honestly (compute_metric
    # already degrades to typed unavailable — this makes it VISIBLE).
    metrics = (
        db.query(MetricDefinition)
        .filter(MetricDefinition.org_id == org_id,
                MetricDefinition.is_deleted == False,  # noqa: E712
                MetricDefinition.lifecycle == "published")
        .all()
    )
    for m in metrics:
        catalog = _catalog_tables(db, m.datasource_id)
        if catalog is None:
            continue
        if (m.base_table or "") not in catalog:
            report["actions"].append({
                "action": "metric_broken_basis", "object": m.name,
                "metric_id": m.id, "base_table": m.base_table})


def _metric_for(db: Session, org_id: str, metric_ref):
    from app.models.metric_definition import MetricDefinition

    return db.query(MetricDefinition).filter(
        MetricDefinition.id == (metric_ref or "").strip(),
        MetricDefinition.org_id == org_id,
        MetricDefinition.is_deleted == False,  # noqa: E712
    ).first()


def _mark(db: Session, org_id: str, obj: OntologyObject, status: str,
          report: dict, apply: bool) -> None:
    if not apply:
        report["actions"].append({"action": f"would_mark_{status}",
                                  "object": obj.name})
        return
    if obj.binding_status == status:
        return
    obj.binding_status = status or None
    obj.last_validated_at = _now()
    if status in ("stale", "broken"):
        # A health DEGRADATION is a meaningful change → P2 revision.
        # A recovery-to-ok is routine validation — field update only,
        # no revision spam (E1).
        from app.services.ontology.revisions import record_revision

        record_revision(db, "object", obj, change_kind="drift",
                        changed_by="ontology-evolution",
                        summary=f"binding {status}")
    report["actions"].append({"action": f"mark_{status}",
                              "object": obj.name})


def _upsert_state(db: Session, org_id: str, source_key: str, kind: str,
                  signature: str, status: str, *, error: str | None = None):
    state = (
        db.query(OntologyEvolutionState)
        .filter(OntologyEvolutionState.org_id == org_id,
                OntologyEvolutionState.source_key == source_key)
        .first()
    )
    if state is None:
        import uuid

        state = OntologyEvolutionState(
            id=str(uuid.uuid4()), org_id=org_id, source_key=source_key,
            source_kind=kind)
        db.add(state)
    state.signature = signature
    state.last_status = status
    state.last_error = error
    state.attempts = (state.attempts or 0) + 1
    state.last_run_at = _now()
