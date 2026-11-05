"""Source Verification Gate (P-SV) — the blocking check BETWEEN table
selection and SQL generation.

Closes three audited NL2SQL gaps with one component:

1. **Grain metadata** — every catalog table carries a ``grain`` ("one row =
   what", auto-inferred at index time, human-overridable per project). A
   question that needs row-level detail must not be answered from a
   pre-aggregated rollup, and a measure summed over a wide fact table must
   be warned about fan-out.
2. **Authority** — tables carry an effective authority: an explicit
   ``authority_override`` wins, then an approved metric binding (the metric
   system's ground truth), then the table role (fact / entity_master beat
   forecast / archive). When a live table and its archive twin are BOTH
   selected, the archive is dropped, never averaged in.
3. **Blocking gate** — refuse / clarify / auto-repair BEFORE SQL is written.
   "LLM proposes, the semantic system verifies."

Honesty rules:
- An unknown grain is passed through as unknown — never invented.
- A refusal says exactly which sources and which grain facts conflict.
- Everything the gate does is returned in a structured result the caller
  logs, so a repaired or refused turn is auditable.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Roles the gate treats as non-authoritative for raw business measures.
SOFT_ROLES = {"aggregate", "reporting_view", "forecast", "archive"}
# Roles that hold row-level business events.
HARD_ROLES = {"fact", "entity_master"}
# Suffixes that mark a table as a stale copy of a live one (archive twin).
_TWIN_SUFFIX_RE = re.compile(
    r"_(archive|backup|bak|hist|history|old)$", re.IGNORECASE
)

# ── question intent detection (structural keywords, EN + zh) ────────────────
_DETAIL_RE = re.compile(
    r"(明细|逐笔|逐单|detail|records|list all|which order|哪些单|列出|line items|"
    r"each row|每一[条笔个])",
    re.IGNORECASE,
)
_FORECAST_RE = re.compile(
    r"(预测|预估|forecast|predict|projection|预计)", re.IGNORECASE
)


@dataclass
class SourceVerificationResult:
    """Outcome of the gate. Exactly one of refuse/clarify fires; repair
    (added/removed tables) can co-occur with a pass."""

    selected_tables: list[str] = field(default_factory=list)
    refuse: str | None = None
    clarify: str | None = None
    added_tables: list[str] = field(default_factory=list)
    removed_tables: list[str] = field(default_factory=list)
    guidance: str = ""
    disclosures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.refuse is None and self.clarify is None


def _effective_grain_role(meta, overlay_grain: str | None, overlay_role: str | None):
    """Overlay (human) wins over auto-inference; None stays None."""
    role = overlay_role or (meta.table_role if meta else "unknown")
    grain = overlay_grain or (meta.grain if meta else None)
    return role or "unknown", grain


def _load_catalog_facts(
    db: Session, kb_id: str, names: list[str], project_id: str | None
) -> dict[str, dict]:
    """table_name -> {role, grain, authority} for the selected tables."""
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import KBTableMeta, ProjectCatalogOverlay

    facts: dict[str, dict] = {}
    if not names:
        return facts
    org_id = (
        getattr(
            db.query(KnowledgeBase).filter(KnowledgeBase.id == kb_id).first(),
            "org_id",
            None,
        )
        if kb_id
        else None
    )
    metas = (
        db.query(KBTableMeta)
        .filter(
            KBTableMeta.kb_id == kb_id,
            KBTableMeta.table_name.in_(names),
            *([KBTableMeta.org_id == org_id] if org_id else []),
        )
        .all()
    )
    meta_by_name = {m.table_name: m for m in metas}

    overlays: dict[str, ProjectCatalogOverlay] = {}
    if project_id:
        rows = (
            db.query(ProjectCatalogOverlay)
            .filter(
                ProjectCatalogOverlay.project_id == project_id,
                ProjectCatalogOverlay.table_name.in_(names),
                *([ProjectCatalogOverlay.org_id == org_id] if org_id else []),
            )
            .all()
        )
        for ov in rows:
            # Prefer an overlay row that names this KB explicitly.
            if ov.table_name not in overlays or ov.kb_id == kb_id:
                overlays[ov.table_name] = ov

    for name in names:
        m = meta_by_name.get(name)
        ov = overlays.get(name)
        role, grain = _effective_grain_role(
            m,
            getattr(ov, "grain", None),
            getattr(ov, "table_role", None),
        )
        authority = getattr(m, "authority_override", None) if m else None
        fact = {"role": role, "grain": grain, "authority": authority}
        # 2B-3: scope rides the same read contract as retrieval — human
        # override > inferred×confidence > unknown (effective_classification
        # enforces the precedence; degrades to absent keys on any problem).
        try:
            from app.services.db.schema_classification import (
                effective_classification as _effective_cls,
            )

            fact.update(_effective_cls(m, ov))
        except Exception as e:  # pragma: no cover — degrade, never gate-break
            logger.debug("source verification: scope facts unavailable: %s", e)
        # The observed partition value rides the stored signals (the same
        # provenance _load_classification_facts uses) — without it the
        # wrong-partition check cannot compare values.
        signals = getattr(m, "dataset_scope_signals_json", None) if m else None
        if isinstance(signals, dict) and signals.get("partition_value"):
            fact["partition_value"] = signals.get("partition_value")
        facts[name] = fact
    return facts


def _approved_metric_binding_tables(
    db: Session, project_id: str | None, question: str, kb_id: str | None = None
) -> list[tuple[str, str]]:
    """Approved metrics the question actually names → (metric, base_table).

    Only metrics whose name or alias appears in the question are checked —
    an unmentioned metric must not inject tables into the plan.
    """
    if not project_id or not question:
        return []
    from app.models.knowledge_base import KnowledgeBase
    from app.models.knowledge_catalog import ProjectMetric

    org_id = (
        getattr(
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == kb_id)
            .first(),
            "org_id",
            None,
        )
        if kb_id
        else None
    )
    ql = question.lower()
    hits: list[tuple[str, str]] = []
    for metric in (
        db.query(ProjectMetric)
        .filter(
            ProjectMetric.project_id == project_id,
            ProjectMetric.status == "approved",
            *([ProjectMetric.org_id == org_id] if org_id else []),
        )
        .all()
    ):
        labels = [metric.name or ""] + list(metric.aliases or [])
        if not any(lbl and lbl.lower() in ql for lbl in labels):
            continue
        for binding in metric.bindings or []:
            table = (binding or {}).get("table") or ""
            if table:
                hits.append((metric.name, table))
    return hits


def _drop_archive_twins(names: list[str]) -> tuple[list[str], list[str]]:
    """If both ``x`` and ``x_archive`` are selected, the archive is noise.

    Returns (kept, removed). A twin is only dropped when its LIVE base table
    is also selected — a lone archive table may be all the source has.
    """
    name_set = set(names)
    kept, removed = [], []
    for n in names:
        m = _TWIN_SUFFIX_RE.search(n)
        base = n[: m.start()] if m else None
        if base and base in name_set:
            removed.append(n)
        else:
            kept.append(n)
    return kept, removed


def verify_sources(
    db: Session,
    kb_id: str,
    question: str,
    selected_tables: list[str],
    project_id: str | None = None,
) -> SourceVerificationResult:
    """The gate. Pure read + classification; the CALLER applies the result."""
    names = [t for t in (selected_tables or []) if t]
    res = SourceVerificationResult(selected_tables=list(names))
    if not names:
        return res

    facts = _load_catalog_facts(db, kb_id, names, project_id)

    # ── 1. Metric-binding auto-repair ──────────────────────────────────────
    # An approved metric the question names is the project's ground truth for
    # that number; its bound base table MUST be in the plan.
    bound = _approved_metric_binding_tables(db, project_id, question, kb_id=kb_id)
    selected_set = set(names)
    for metric_name, table in bound:
        if table not in selected_set and table not in res.added_tables:
            res.added_tables.append(table)
            res.disclosures.append(
                f"approved metric '{metric_name}' is bound to table "
                f"'{table}' — included in the plan"
            )
    if res.added_tables:
        names = names + [t for t in res.added_tables if t not in selected_set]
        facts.update(
            _load_catalog_facts(db, kb_id, res.added_tables, project_id)
        )

    # ── 2. Authority conflicts: drop archive twins of selected live tables ─
    kept, removed_twins = _drop_archive_twins(names)
    if removed_twins:
        res.removed_tables.extend(removed_twins)
        for t in removed_twins:
            res.disclosures.append(
                f"table '{t}' is an archived copy; the live table also in the "
                f"plan is authoritative — archive excluded"
            )
    names = kept

    # ── 3. Grain compatibility ─────────────────────────────────────────────
    soft = [n for n in names if facts.get(n, {}).get("role") in SOFT_ROLES]
    hard = [n for n in names if facts.get(n, {}).get("role") in HARD_ROLES]
    detail_intent = bool(_DETAIL_RE.search(question or ""))
    forecast_intent = bool(_FORECAST_RE.search(question or ""))

    if detail_intent and hard:
        # The detail tables exist in the plan — soft sources stay but must
        # not be the ones the measure is read from.
        pass

    if detail_intent and not hard and soft:
        soft_list = ", ".join(sorted(soft))
        res.clarify = (
            f"Your question asks for row-level detail, but the selected "
            f"source(s) {soft_list} are pre-aggregated / reporting / archived "
            f"tables — their rows are not individual business records. "
            f"Please confirm, or point me at the detail table."
        )
        return res

    if soft and not forecast_intent:
        # A forecast/rollup table was selected for a question that is not
        # about forecasting. Keep it out of the measure path unless it is
        # the ONLY thing selected.
        non_forecast = [
            n for n in names
            if facts.get(n, {}).get("role") != "forecast"
        ]
        if len(non_forecast) < len(names) and non_forecast:
            for n in names:
                if n not in non_forecast:
                    res.removed_tables.append(n)
                    res.disclosures.append(
                        f"table '{n}' holds forecast/predicted values, not "
                        f"actuals — excluded from a non-forecast question"
                    )
            names = non_forecast

    # ── 3.5 Scope authorization (2B-3, flag-gated, ships OFF) ──────────────
    # Plan-level counterpart of the execution gate's scope_authorization:
    # a company-wide question whose ENTIRE selection is confirmed partition
    # data is clarified (not answered from one partition silently) when the
    # catalog also holds a global dataset, and disclosed when it does not.
    # Evidence-positive only: unknown scope and ``partition_family`` guesses
    # are never convicted; human overrides win via effective_classification;
    # any catalog problem degrades to no scope check.
    try:
        from app.config import settings as _sv_settings

        if getattr(_sv_settings, "SCHEMA_SCOPE_AUTHORIZATION_ENABLED", False):
            from app.services.db.schema_classification import (
                resolve_question_scope_from_facts,
            )
            from app.services.db.table_retrieval import (
                _load_classification_facts as _sv_load_cls_facts,
            )

            _kb_facts = _sv_load_cls_facts(db, [kb_id], project_id=project_id)
            _q_scope = resolve_question_scope_from_facts(question or "", _kb_facts)
            _asked = str((_q_scope or {}).get("value") or "").strip().lower()
            _confirmed = {
                n for n in names
                if str((facts.get(n) or {}).get("dataset_scope") or "").startswith(
                    "partition:"
                )
            }
            if _asked:
                # The question named a partition; a DIFFERENT confirmed
                # partition in the plan would report the wrong region's
                # numbers. Remove it with a disclosure when others remain.
                _wrong = {
                    n for n in _confirmed
                    if str((facts.get(n) or {}).get("partition_value") or "")
                    .strip().lower() not in ("", _asked)
                }
                if _wrong and len(names) > len(_wrong):
                    for n in sorted(_wrong):
                        res.removed_tables.append(n)
                        res.disclosures.append(
                            "scope: table '" + n + "' holds a different "
                            "partition than the question asks about ("
                            + _asked + ") — excluded"
                        )
                    names = [n for n in names if n not in _wrong]
                    for n in _wrong:
                        facts.pop(n, None)
                elif _wrong:
                    res.clarify = (
                        "The question asks about "
                        f"{_asked!r}, but the selected data covers a "
                        "different partition. Please confirm the data you "
                        "want, or point me at the right one."
                    )
                    return res
            elif _confirmed and len(_confirmed) == len(names):
                # Company-wide question, every selected table a confirmed
                # partition. Clarify when a global dataset exists (it is the
                # right answer and must not be silently skipped); otherwise
                # disclose honestly — nothing better exists to answer with.
                _globals = sorted(
                    n for n, f in (_kb_facts or {}).items()
                    if str((f or {}).get("dataset_scope") or "") == "global"
                )
                if _globals:
                    res.clarify = (
                        "This question spans the whole organization, but the "
                        "selected data covers only a single partition. The "
                        "company-wide dataset(s) "
                        + ", ".join(f"{g!r}" for g in _globals)
                        + " also exist — should I answer from those, or from "
                        "one specific partition?"
                    )
                    return res
                res.disclosures.append(
                    "scope: company-wide question answered from partition "
                    "data only (" + ", ".join(sorted(_confirmed)) + "); no "
                    "company-wide dataset exists in this catalog, so figures "
                    "cover those partitions only"
                )
            elif (
                # Reviewer table, row 3: probable partitions with the key
                # unidentified. Disclose uncertainty; do not refuse on an
                # unverified inference, and do not silently claim completeness.
                (family_names := {
                    n for n in names
                    if str((facts.get(n) or {}).get("dataset_scope") or "")
                    == "partition_family"
                })
                and (len(_confirmed) + len(family_names)) == len(names)
            ):
                _globals = sorted(
                    n for n, f in (_kb_facts or {}).items()
                    if str((f or {}).get("dataset_scope") or "") == "global"
                )
                res.disclosures.append(
                    "scope: the selected data's scope could not be fully "
                    "verified (" + ", ".join(sorted(family_names | _confirmed))
                    + " — probable partitions); a company-wide dataset"
                    + (" (" + ", ".join(f"{g!r}" for g in _globals) + ") also "
                       "exists" if _globals else " does not exist in this "
                       "catalog")
                    + " — figures may not be complete"
                )
    except Exception as _scope_err:  # degrade: a catalog problem never gates
        logger.debug("source verification: scope check skipped: %s", _scope_err)

    # ── 4. Build guidance: grain facts + authority, for the SQL model ──────
    lines: list[str] = ["[Source verification]"]
    for n in names:
        f = facts.get(n) or {}
        role, grain = f.get("role", "unknown"), f.get("grain")
        if grain:
            lines.append(f"- {n}: grain = {grain} (role: {role})")
        else:
            lines.append(
                f"- {n}: grain unknown (role: {role}) — check the primary "
                f"key before aggregating"
            )
        # 2B-3: scope the SQL model must respect when aggregating.
        ds = str(f.get("dataset_scope") or "")
        if ds.startswith("partition:"):
            pv = str(f.get("partition_value") or "").strip()
            lines.append(
                f"- {n}: scope = {ds}"
                + (f" (value {pv!r})" if pv else "")
                + " — a single partition; company-wide totals must not come "
                "from it alone"
            )
        elif ds == "global":
            lines.append(f"- {n}: scope = global (company-wide dataset)")
    if res.removed_tables or res.added_tables:
        if res.added_tables:
            lines.append(
                "- REQUIRED source(s) added by metric bindings: "
                + ", ".join(res.added_tables)
            )
        if res.removed_tables:
            lines.append(
                "- EXCLUDED source(s): " + ", ".join(sorted(set(res.removed_tables)))
                + " — do NOT query them"
            )
    lines.append(
        "- Aggregate only over the grain the metric needs; if two sources "
        "report the same measure, use the authoritative one and say which."
    )
    res.guidance = "\n".join(lines)
    return res
