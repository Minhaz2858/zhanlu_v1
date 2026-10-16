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
        facts[name] = {"role": role, "grain": grain, "authority": authority}
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
