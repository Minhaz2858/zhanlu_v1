"""Deterministic candidate retrieval for the table-selection stage.

The Oracle-style split this implements: an *offline* semantic foundation (the
KB catalog: descriptions, aliases, domains, semantic types, enum dictionaries,
join relations, coverage windows) plus a *workload* trace (verified query plans,
shadow query logs) is already built in this lineage. What was missing is the
online consumer — the stage that turns that foundation into a small, ordered,
evidence-carrying candidate set BEFORE the model is asked to choose tables.

Why it has to exist (measured 2026-09-18 on a 143-table warehouse):

  * ``format_toc_text(toc, token_budget=1200)`` rendered 14 of 143 tables, in
    ``build_full_toc``'s alphabetical order, so the model only ever saw
    alphabetically-leading internal tables — "... and 128 more tables". Not
    one business table was in the surface it chose from.
  * Asked about a dated report it therefore picked a price column whose data
    had ended months earlier and a 0-row dimension table → 0 rows → "no data".
  * Asked about contract execution it invented plausible table names that do
    not exist in the source.
  * Holding the model and question fixed and raising only that budget to
    6,000 made it pick the correct customer's contract tables. The model was
    never the problem; the surface it was given was.

Everything here is derived from the catalog rows and the question — no table
name, database type, dialect or customer schema appears in this file, so a
newly connected database gets retrieval with no new code.

Provenance tiers follow ``semantic_store``: VERIFIED (a human or a verified
plan wrote it) > DISCOVERED (the indexer read it) > LEARNED_FROM_WORKLOAD >
RUNTIME_INFERRED (computed for this question, stored nowhere).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.db.lexical import tokens as lexical_tokens

logger = logging.getLogger(__name__)

# Weights are deliberate and ordered: a VERIFIED plan outranks a domain
# assignment, which outranks lexical/semantic name evidence. Popularity alone
# can never create a candidate — that rule lives in
# ``SemanticStore.rank_relations`` and is preserved by only adding to its score.
_W_PLAN = 3.0
_W_DOMAIN = 1.5

# ── Stage 2B-2: classification adjustments (flag-gated by the caller) ──────
# Refinement weights, deliberately SMALLER than the core evidence components:
# classification must never outrank what the question actually says. All
# penalties/boosts scale by provenance — human verdict ×1.0, inferred verdict
# ×its stored confidence — so a 0.6 guess can never dominate (review point 2).
_W_CLASS_SYSTEM = 4.0                # penalty: platform tables sink
_W_HUMAN_BUSINESS = 0.5              # boost: a human business verdict
_W_SCOPE_GLOBAL = 0.4                # boost: global datasets fit any scope
_W_SCOPE_PARTITION_UNIVERSAL = 1.2   # penalty: partition on a universal question
_W_SCOPE_PARTITION_MATCH = 2.0       # boost: the question NAMED this partition


def classification_adjustments(
    facts: dict, question: str
) -> dict[str, tuple[float, str]]:
    """Pure: per-table ``(score delta, why)`` from classification facts.

    ``facts`` is the ``effective_classification`` contract (loader below),
    optionally carrying a structured ``partition_value`` per table. The
    question scope is resolved against OBSERVED partition values only —
    no vocabulary, no customer nouns.

    Ranking precedence (reviewer condition 1):
    - human override → full weight (highest authority);
    - inferred classification → weight × its stored confidence;
    - unknown → zero delta, zero assumption.
    """
    from app.services.db.schema_classification import (
        resolve_question_scope_from_facts,
    )

    scope = resolve_question_scope_from_facts(question, facts)
    out: dict[str, tuple[float, str]] = {}
    for name, f in (facts or {}).items():
        delta = 0.0
        whys: list[str] = []

        def _scale(source_key: str) -> float:
            source = f.get(source_key + "_source")
            if source == "human":
                return 1.0
            if source == "inferred":
                return float(f.get(source_key + "_confidence") or 0.0)
            return 0.0

        sclass = f.get("schema_class")
        if sclass == "system":
            d = _W_CLASS_SYSTEM * _scale("schema_class")
            if d:
                delta -= d
                whys.append(
                    f"classified system ({f.get('schema_class_source')})"
                )
        elif sclass == "business" and f.get("schema_class_source") == "human":
            delta += _W_HUMAN_BUSINESS
            whys.append("human business override")

        dscope = f.get("dataset_scope")
        if dscope == "global":
            d = _W_SCOPE_GLOBAL * _scale("dataset_scope")
            if d:
                delta += d
                whys.append("global dataset")
        elif dscope and (
            dscope.startswith("partition:") or dscope == "partition_family"
        ):
            pv = str(f.get("partition_value") or "").strip().lower()
            if scope and pv and pv == str(scope.get("value", "")).lower():
                d = _W_SCOPE_PARTITION_MATCH * _scale("dataset_scope")
                if d:
                    delta += d
                    whys.append(f"matches question scope {pv!r}")
            else:
                d = _W_SCOPE_PARTITION_UNIVERSAL * _scale("dataset_scope")
                if d:
                    delta -= d
                    whys.append(
                        "partitioned dataset — narrower than the question"
                        if not scope else "different partition than asked"
                    )
        if delta or whys:
            out[name] = (round(delta, 3), "; ".join(whys))
    return out


def _load_classification_facts(
    db: Any, kb_ids: list[str], project_id: str | None = None
) -> dict[str, dict]:
    """Load ``effective_classification`` facts for a KB's tables.

    Overlay overrides win (the contract enforces it); the structured
    ``partition_value`` rides along from the stored signals for the
    question-scope resolver. Degrades to {} on any catalog problem —
    retrieval must never break answering.
    """
    if db is None or not kb_ids:
        return {}
    try:
        from sqlalchemy import or_

        from app.models.knowledge_catalog import (
            KBTableMeta,
            ProjectCatalogOverlay,
        )
        from app.services.db.schema_classification import (
            effective_classification,
        )

        rows = (
            db.query(KBTableMeta)
            .filter(
                KBTableMeta.kb_id.in_(list(kb_ids)),
                KBTableMeta.is_deleted.is_(False),
            )
            .all()
        )
        ov_query = db.query(ProjectCatalogOverlay).filter(
            ProjectCatalogOverlay.is_deleted.is_(False),
        )
        ov_query = ov_query.filter(
            or_(
                ProjectCatalogOverlay.kb_id.in_(list(kb_ids)),
                *(
                    [ProjectCatalogOverlay.project_id == project_id]
                    if project_id
                    else []
                ),
            )
        )
        overlays = ov_query.all() if rows else []
        ov_by_table: dict[str, Any] = {}
        for ov in overlays:
            if ov.table_name:
                # project-scoped overrides are the more specific human record
                prev = ov_by_table.get(ov.table_name)
                if prev is None or (
                    ov.project_id == project_id and prev.project_id != project_id
                ):
                    ov_by_table[ov.table_name] = ov

        facts: dict[str, dict] = {}
        for m in rows:
            fact = effective_classification(m, ov_by_table.get(m.table_name))
            signals = m.dataset_scope_signals_json or {}
            if isinstance(signals, dict):
                fact["partition_value"] = signals.get("partition_value")
            facts[m.table_name] = fact
        return facts
    except Exception as e:  # never let retrieval break answering
        logger.warning("table_retrieval: classification facts unavailable: %s", e)
        return {}


@dataclass
class TableCandidate:
    """One table offered to the table-selection stage, with its evidence."""

    table_name: str
    score: float
    evidence: list[str] = field(default_factory=list)
    # Per-component contribution, so a rank can be argued with rather than
    # trusted: {"text": 3.0, "plan": 3.0, "column": 1.75, "embedding": 0.4}.
    breakdown: dict[str, float] = field(default_factory=dict)
    domain: str = ""
    row_count: int | None = None
    coverage_start: str = ""
    coverage_end: str = ""
    # True/False only when the catalog carries a usable window; None means the
    # catalog has no measurement. A missing measurement is reported as missing,
    # never as "in sync" / "covered".
    covers_window: bool | None = None
    latest_date: str = ""

    def as_dict(self) -> dict:
        return {
            "table_name": self.table_name,
            "score": round(self.score, 3),
            "breakdown": dict(self.breakdown),
            "evidence": list(self.evidence),
            "domain": self.domain,
            "row_count": self.row_count,
            "coverage": {"min": self.coverage_start, "max": self.coverage_end},
            "covers_window": self.covers_window,
        }


@dataclass
class RetrievalResult:
    """The candidate set plus the honest statement of what could not be checked."""

    candidates: list[TableCandidate] = field(default_factory=list)
    window: tuple[date, date] | None = None
    # Set when a window was asked for and NO candidate's catalog window reaches
    # it. The caller must then reroute or disclose — never answer silently.
    uncovered: bool = False
    # Latest window end across the candidates, for the disclosure message.
    latest_available: str = ""
    # Tables whose catalog window is missing entirely.
    unknown_coverage: list[str] = field(default_factory=list)
    method: str = "none"
    # What the ranking could and could not measure (e.g. embeddings skipped
    # because the similarity distribution was flat). Rendered with the ranked
    # block so the model — and the audit trail — sees the same caveats.
    notes: list[str] = field(default_factory=list)
    # 2B-2 review concern 3 (2026-10-30): scope REFUSALS are not audit noise —
    # they mean the answer's scope may differ from what was asked. Each entry
    # is a reader-usable sentence the caller must surface in the disclosure
    # channel (the SQL prompt note alone is not user-facing).
    scope_refusals: list[str] = field(default_factory=list)
    # Which components contributed, largest first: {"text": 34, "column": 12, …}
    component_counts: dict[str, int] = field(default_factory=dict)
    # Phase 3A: provenance of the enterprise-ontology evidence used for this
    # ranking (selected objects/links/terms/hypotheses + enabled rules).
    # None when no ontology context was supplied — the field is additive and
    # every consumer must tolerate its absence.
    ontology_provenance: dict | None = None

    @property
    def names(self) -> list[str]:
        return [c.table_name for c in self.candidates]

    def as_dict(self) -> dict:
        """Serialisable form for telemetry / answer provenance."""
        return {
            "method": self.method,
            "window": [str(self.window[0]), str(self.window[1])] if self.window else None,
            "uncovered": self.uncovered,
            "latest_available": self.latest_available,
            "candidate_count": len(self.candidates),
            "notes": list(self.notes),
            "scope_refusals": list(self.scope_refusals),
            "components": dict(self.component_counts),
            "candidates": [c.as_dict() for c in self.candidates[:12]],
        }


def _parse_day(value: str | None) -> date | None:
    """Parse the ISO-ish dates the catalog stores. Returns None if unusable.

    ``coverage_json`` is written by the indexer from whatever the connector
    returned, so this is tolerant on purpose: ``0000-00-00`` (a real MySQL
    zero-date seen on ``actual_price``) and empty strings must parse to None
    rather than raise.
    """
    if not value:
        return None
    text = str(value).strip()[:10]
    if not text or text.startswith("0000"):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _covers(
    window: tuple[date, date] | None, start: date | None, end: date | None
) -> bool | None:
    """Does the table's catalog window reach the question's window start?

    Overlap, not containment: a table whose data starts after the question
    window must still be offered (the user's period may simply predate it) —
    what must be caught is a table whose data ENDS before the window starts,
    which is a guaranteed 0 rows.

    The end date alone is enough to CONVICT: ``actual_price`` stores a MySQL
    zero-date as ``min_date`` ('0000-00-00') next to a real ``max_date`` of
    2025-07-31, and a table that ends in 2025 cannot hold a 2026 question just
    because its start is unparseable. An unknown END is what yields None — a
    missing measurement, reported as missing.
    """
    if window is None or end is None:
        return None
    q_start, q_end = window
    if end < q_start:
        return False
    if start is None:
        return None
    return start <= q_end


def domain_support(question: str, domain: dict) -> list[str]:
    """The question terms a candidate domain actually answers to.

    Domain assignment comes from an embedding similarity over domain documents,
    which is useful when it is right and actively harmful when it is not: LIVE,
    a Chinese contract question was scoped into "User Authentication and
    Permissions" and eight ``auth_*`` tables were handed to the model with a
    1.5-point "domain match" each — they were the entire top-8.

    So a domain boost must be EARNED by the question's own vocabulary appearing
    in that domain's vocabulary (either language). A domain that matches nothing
    stays visible in the annotation but contributes no score.
    """
    if not question or not domain:
        return []
    text = " ".join(
        str(domain.get(k) or "") for k in ("name_en", "name_zh", "description", "description_en", "description_zh")
    )
    q = lexical_tokens(question)
    if not q:
        return []
    return sorted(q & lexical_tokens(text))


def _embeddings_enabled() -> bool:
    try:
        from app.config import settings

        return bool(getattr(settings, "RETRIEVAL_EMBEDDING_ENABLED", True))
    except Exception:  # pragma: no cover - settings always importable in practice
        return True


def _resolve_embedder() -> tuple[Any | None, str]:
    """The embedding function, or None with the reason it is unavailable.

    Availability is a deployment fact, not an error: the pickled model may not
    be present offline. The caller records the reason instead of pretending the
    component contributed.
    """
    try:
        from app.services.document_ingestion.embedder import get_embedding_function

        return get_embedding_function(), ""
    except Exception as e:
        return None, f"embedding similarity unavailable: {type(e).__name__}"


def retrieve_for_question(
    db,
    kb_ids: list[str],
    question: str,
    toc: list[dict],
    *,
    project_id: str | None = None,
    window: tuple[date, date] | None = None,
    limit: int = 40,
    enabled: bool = True,
    store: Any | None = None,
    embedder: Any | None = None,
    use_embeddings: bool | None = None,
    classification_facts: dict | None = None,
    ontology_context: dict | None = None,
) -> RetrievalResult:
    """Rank the catalog tables for this question. Deterministic, read-only.

    ``toc`` is the already-built table-of-contents (one dict per catalog table
    with ``table_name`` / ``description_zh`` / ``description_en`` /
    ``domain_name`` / ``row_count`` / ``min_date`` / ``max_date`` / ``columns``),
    so this adds no catalog round-trip of its own.

    The score is a SUM of declared components, and every one of them is
    recorded on the candidate (``breakdown``) and in the prompt (``evidence``):

        plan     3.0   a previously VERIFIED plan for this exact question
        text     3.0   BM25 over name/description/domain/columns, IDF from the catalog
        metric   1.5   a VERIFIED metric or terminology definition names this table
        domain   1.5   a candidate domain whose vocabulary the question matches
        column   2.25  the question names the table's columns (cap)
        store    …     name/alias/description hits from the semantic store
        embed    0.75  cosine(question, table document), bounded tie-breaker
    """
    result = RetrievalResult(window=window)
    if not toc or not question:
        return result

    by_name = {t.get("table_name"): t for t in toc if t.get("table_name")}
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}
    breakdown: dict[str, dict[str, float]] = {}

    def _add(name: str, amount: float, why: str, component: str = "") -> bool:
        """Add evidence for a name IN THE CATALOG. Returns whether it was kept."""
        if name not in by_name:
            return False  # never offer a table outside the catalog the user may read
        scores[name] = scores.get(name, 0.0) + amount
        if component and amount:
            slot = breakdown.setdefault(name, {})
            slot[component] = round(slot.get(component, 0.0) + amount, 3)
        evidence.setdefault(name, [])
        if why and why not in evidence[name]:
            evidence[name].append(why)
        return True

    # The semantic store is asked ONCE and reused: its relations carry the
    # overlay aliases, its domains carry the Chinese domain names, and its
    # glossary carries the VERIFIED metric definitions — all of which the fused
    # catalog evidence needs.
    store_obj = store
    if store_obj is None and enabled:
        try:
            from app.services.db.semantic_store import get_semantic_store

            store_obj = get_semantic_store(db, list(kb_ids), project_id=project_id)
        except Exception as e:  # never let retrieval break answering
            logger.warning("table_retrieval: semantic store unavailable: %s", e)

    # ── 1. semantic / lexical evidence (SEMANTIC_STORE_ENABLED) ──────────────
    if enabled and store_obj is not None:
        try:
            hits = store_obj.rank_relations(question, k=limit)
            for h in hits:
                added = _add(h.table_name, float(h.score), "name/alias evidence", "store")
                # Only rewrite the evidence of a table we actually kept: a hit
                # outside the catalog is skipped, and indexing its evidence
                # afterwards raised KeyError and took the WHOLE ranking down
                # (caught by the broad except below, i.e. silently no
                # candidates at all).
                if added and h.evidence:
                    evidence[h.table_name][-1] = "; ".join(h.evidence[:3])
            if hits:
                result.method = "semantic"
        except Exception as e:  # never let retrieval break answering
            logger.warning("table_retrieval: semantic ranking unavailable: %s", e)

    # ── 2. VERIFIED plan reuse: the strongest available evidence ────────────
    try:
        from app.services.db.plan_memory import load_plan

        for kb_id in kb_ids:
            plan = load_plan(db, kb_id, question)
            for name in (plan or {}).get("tables", []) or []:
                _add(name, _W_PLAN, "verified plan for this question", "plan")
    except Exception as e:
        logger.debug("table_retrieval: plan memory unavailable: %s", e)

    # ── 3. business-domain assignment (DISCOVERED, and only when earned) ────
    try:
        from app.services.knowledge_graph.domain_discovery import (
            scoped_tables_for_question,
        )

        scoped, matched = scoped_tables_for_question(db, list(kb_ids), question)
        supported: list[str] = []
        for d in matched or []:
            label = d.get("name_en") or d.get("name_zh") or ""
            terms = domain_support(question, d)
            if not terms:
                logger.info(
                    "table_retrieval: domain %r matched the question no vocabulary "
                    "(%s) — annotation only, no score",
                    label, sorted(lexical_tokens(question))[:6],
                )
                continue
            supported.append(label)
            for name in scoped or []:
                _add(
                    name, _W_DOMAIN,
                    f"domain {label} (matches {'/'.join(terms[:3])})",
                    "domain",
                )
        if not supported and matched:
            result.notes.append(
                "business-domain matches were not scored: the question shares no "
                "vocabulary with any matched domain"
            )
    except Exception as e:
        logger.debug("table_retrieval: domain scoping unavailable: %s", e)

    # ── 3b. workload pair-boost (P0-1): when a mined table_pair has one side
    # already scored as a candidate, boost its partner — analysts actually
    # join these two tables together, so the partner belongs in the DDL slice
    # even if the question text never mentions it.
    if enabled and store_obj is not None:
        try:
            _W_PAIR = 0.75
            scored = {
                n.lower() for n, s in scores.items() if s >= _W_PLAN
            }
            if scored:
                for hint in store_obj.join_hints(min_occurrences=2, k=25):
                    a, b = hint["table_a"].lower(), hint["table_b"].lower()
                    partner = None
                    if a in scored and b not in scored:
                        partner = b
                    elif b in scored and a not in scored:
                        partner = a
                    if partner:
                        _add(
                            partner,
                            _W_PAIR,
                            f"frequently joined with "
                            f"{a if partner == b else b} (workload "
                            f"{hint['occurrences']}×)",
                            "workload-pair",
                        )
        except Exception as e:
            logger.debug("table_retrieval: workload pair-boost unavailable: %s", e)

    # ── 3c. enterprise ontology evidence (Phase 3A, flag-gated upstream) ────
    # ``ontology_context`` comes from build_ontology_planning_context (org-
    # scoped). VERIFIED objects/links are evidence; proposed ones are
    # hypotheses (tiny/nul boost, always labeled). Ontology NEVER invents
    # joins — links only hint that two catalog tables belong together; the
    # actual join conditions still come from the authorized schema catalog.
    if ontology_context:
        try:
            _W_OBJ, _W_HYP, _W_LINK, _W_TERM, _W_METRIC = (
                1.25, 0.25, 0.75, 1.0, 1.5)
            _q = set(lexical_tokens(question))
            prov: dict = {"objects": [], "links": [], "terms": [],
                          "hypotheses": [], "enabled_sql_rules": [],
                          "metrics": []}

            for obj in ontology_context.get("objects_verified") or []:
                kept = False
                if _q & set(lexical_tokens(
                        f"{obj.get('name')} {obj.get('display_name') or ''}")
                ) and obj.get("backing_table"):
                    kept = _add(obj["backing_table"], _W_OBJ,
                                f"ontology object '{obj['name']}' (verified) "
                                f"maps to this table", "ontology")
                prov["objects"].append({"name": obj.get("name"),
                                        "table": obj.get("backing_table"),
                                        "influenced": bool(kept)})
            for obj in ontology_context.get("objects_hypotheses") or []:
                kept = False
                if _q & set(lexical_tokens(obj.get("name") or "")) \
                        and obj.get("backing_table"):
                    kept = _add(obj["backing_table"], _W_HYP,
                                f"ontology object '{obj['name']}' (PROPOSED — "
                                f"hypothesis only)", "ontology-hypothesis")
                prov["hypotheses"].append({"name": obj.get("name"),
                                           "table": obj.get("backing_table"),
                                           "influenced": bool(kept)})

            scored_now = {n.lower() for n, s in scores.items()
                          if s >= _W_LINK}
            scored_any = {n.lower() for n in scores}
            for link in ontology_context.get("links") or []:
                a, b = (link.get("from_table") or "").lower(), \
                    (link.get("to_table") or "").lower()
                if not a or not b:
                    continue
                pair_hint = a if (b in scored_now and a not in scored_now) \
                    else (b if (a in scored_now and b not in scored_now) else None)
                if link.get("evidence_status") == "verified":
                    kept = False
                    if pair_hint:
                        kept = _add(pair_hint, _W_LINK,
                                    f"verified ontology relationship "
                                    f"{link.get('from')} --{link.get('type')}--> "
                                    f"{link.get('to')} (candidate hint, not a join)",
                                    "ontology-link")
                    prov["links"].append({"from": link.get("from"),
                                          "to": link.get("to"),
                                          "tables": [a, b],
                                          "influenced": bool(kept)})
                elif pair_hint is None and (a in scored_any or b in scored_any):
                    # hypothesis links: provenance only, NEVER scored
                    prov["hypotheses"].append({
                        "link": f"{link.get('from')}--{link.get('type')}"
                                f"-->{link.get('to')}"})

            for term in ontology_context.get("terms") or []:
                term_toks = set(lexical_tokens(
                    f"{term.get('word')} {term.get('description') or ''}"))
                if not (_q & set(lexical_tokens(term.get("word") or ""))):
                    continue
                matched = 0
                for name, toc_entry in by_name.items():
                    desc_toks = set(lexical_tokens(" ".join(filter(None, [
                        (toc_entry or {}).get("description_zh"),
                        (toc_entry or {}).get("description_en"),
                    ]))))
                    if len(term_toks & desc_toks) >= 2 and matched < 3:
                        matched += 1
                        _add(name, _W_TERM,
                             f"terminology '{term.get('word')}' matches this "
                             f"table's business description", "terminology")
                prov["terms"].append({"word": term.get("word"),
                                      "influenced": matched > 0})

            # Phase 3B: metric definitions as ranking evidence. A metric
            # contributes ONLY its name/description/synonym relevance and
            # its authorized base_table mapping — never its SQL definition,
            # never an invented calculation. Unmapped or unverified metrics
            # are recorded as provenance WITHOUT influencing ranking.
            for metric in ontology_context.get("metrics") or []:
                entry = {"name": metric.get("name"),
                         "table": metric.get("base_table"),
                         "evidence_status": metric.get("evidence_status")}
                m_tokens = set(lexical_tokens(" ".join(filter(None, [
                    metric.get("name"), metric.get("display_name"),
                    " ".join(metric.get("synonyms") or [])]))))
                kept = False
                if _q & m_tokens:
                    if metric.get("base_table"):
                        if metric.get("evidence_status") == "verified":
                            kept = _add(
                                metric["base_table"], _W_METRIC,
                                f"business metric '{metric.get('name')}' "
                                f"is defined on this table",
                                "ontology-metric")
                            if not kept:
                                # mapped table outside the authorized
                                # catalog — honest "unmapped to anything
                                # queryable"
                                entry["unmapped"] = True
                        # unverified metric: provenance only —
                        # evidence_status in the entry explains why
                    else:
                        # no table mapping — honest degradation
                        entry["unmapped"] = True
                entry["influenced"] = bool(kept)
                prov["metrics"].append(entry)

            for rule in ontology_context.get("enabled_sql_rules") or []:
                prov["enabled_sql_rules"].append({
                    "id": rule.get("id"), "statement": rule.get("statement"),
                    "scope": rule.get("scope"),
                    "datasource_ids": rule.get("datasource_ids") or [],
                    # rules are planning guidance, never a scoring component
                    "influenced": False})
            result.ontology_provenance = prov
        except Exception as e:  # never let retrieval break answering
            logger.warning("table_retrieval: ontology evidence unavailable: %s", e)

    # ── 4. fused catalog evidence: text / columns / metrics / similarity ────
    if enabled:
        try:
            from app.services.db.retrieval_evidence import (
                build_documents,
                cache_documents,
                fuse_evidence,
            )

            _aliases: dict[str, list[str]] = {}
            _domains: list[dict] = []
            if store_obj is not None:
                try:
                    _aliases = {
                        r.table_name.lower(): list(r.aliases or ())
                        for r in (store_obj.relations() or [])
                    }
                except Exception:
                    _aliases = {}
                try:
                    _domains = store_obj.domains() or []
                except Exception:
                    _domains = []

            _embedder = embedder
            _want_embeddings = (
                use_embeddings if use_embeddings is not None else _embeddings_enabled()
            )
            if _embedder is None and _want_embeddings:
                _embedder, why = _resolve_embedder()
                if why:
                    result.notes.append(why)
            if _embedder is not None:
                # Encoding every catalog description costs seconds; the vectors
                # are a pure function of the catalog, so they are cached across
                # questions and invalidated by content digest (a re-index).
                _embedder = cache_documents(
                    _embedder,
                    build_documents(toc, aliases=_aliases, domains=_domains),
                    cache_key="|".join(sorted(n for n in by_name if n)),
                )

            fused = fuse_evidence(
                question, toc, store=store_obj, embedder=_embedder,
                aliases=_aliases, domains=_domains,
            )
            for name, hit in fused.hits.items():
                for component, amount in hit.breakdown.items():
                    _add(name, amount, "", component)
                for why in hit.evidence:
                    if why not in evidence.get(name, []):
                        evidence.setdefault(name, []).append(why)
            result.notes.extend(n for n in fused.notes if n not in result.notes)
            if fused.hits and result.method == "none":
                result.method = "fused"
            elif fused.hits:
                result.method = "semantic+fused"
        except Exception as e:  # never let retrieval break answering
            logger.warning("table_retrieval: fused evidence unavailable: %s", e)

    # ── 4.5 classification refinement (2B-2; caller flag-gated) ─────────────
    # Annotation-first, like coverage: adjustments shift RANK, nothing is
    # filtered, and every delta lands in the candidate's evidence so the
    # ranking stays arguable. Facts absent (flag off / loader degraded) →
    # byte-identical behavior.
    if classification_facts:
        try:
            for name, (delta, why) in classification_adjustments(
                classification_facts, question
            ).items():
                if delta:
                    _add(name, delta, why, "classification")
        except Exception as e:  # never let retrieval break answering
            logger.warning("table_retrieval: classification adjustment failed: %s", e)

    # ── 5. coverage annotation — a constraint, not a filter ─────────────────
    # Tables are never DROPPED for coverage. "This table has no data for your
    # period" is useful information the model must be able to see; silently
    # removing every table would turn an honest "no data in this window" into a
    # fabricated answer from whatever remained.
    for name, score in scores.items():
        t = by_name[name]
        start = _parse_day(t.get("min_date"))
        end = _parse_day(t.get("max_date"))
        covers = _covers(window, start, end)
        row_count = t.get("row_count")
        cand = TableCandidate(
            table_name=name,
            score=score,
            evidence=evidence.get(name, []),
            breakdown=breakdown.get(name, {}),
            domain=t.get("domain_name") or "",
            row_count=row_count if isinstance(row_count, int) else None,
            coverage_start=t.get("min_date") or "",
            coverage_end=t.get("max_date") or "",
            covers_window=covers,
            latest_date=t.get("max_date") or "",
        )
        result.candidates.append(cand)
        if covers is None and window is not None:
            result.unknown_coverage.append(name)

    result.candidates.sort(key=lambda c: (-c.score, c.table_name))
    result.candidates = result.candidates[:limit]

    counts: dict[str, int] = {}
    for c in result.candidates:
        for component in c.breakdown:
            counts[component] = counts.get(component, 0) + 1
    result.component_counts = dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))

    if window is None:
        return result

    known = [c for c in result.candidates if c.covers_window is not None]
    if known and not any(c.covers_window for c in known):
        # Every candidate the catalog can vouch for ends before the period
        # asked about. Say so; do not answer as if the data were simply zero.
        result.uncovered = True
        result.latest_available = max((c.coverage_end for c in known), default="")
    return result


def format_candidates_block(res: RetrievalResult, limit: int = 40) -> str:
    """Render the candidate set as prompt text (names + why + coverage).

    A guard hands back the identifiers it already knows, not a verdict — so
    each line carries the evidence and the catalog window, and tables that
    cannot contain the period are LABELLED rather than hidden.

    Each evidence tag names the component that produced it (``text`` / ``plan``
    / ``column`` / ``metric`` / ``domain`` / ``similarity``), so the ranking is
    auditable from the prompt itself: a wrong top-1 can be traced to the signal
    that put it there instead of being attributed to "the model".
    """
    if not res.candidates:
        return ""
    lines = [
        "## Ranked candidate tables (deterministic retrieval over the catalog; "
        "these carry evidence, in rank order)"
    ]
    if res.component_counts:
        lines.append(
            "   evidence used: "
            + ", ".join(f"{c} on {n}" for c, n in res.component_counts.items())
        )
    for c in res.candidates[:limit]:
        bits = [c.table_name]
        if c.evidence:
            bits.append("; ".join(c.evidence[:3]))
        if c.domain:
            bits.append(f"domain {c.domain}")
        if c.coverage_start and c.coverage_end:
            bits.append(f"data window {c.coverage_start}..{c.coverage_end}")
        elif c.coverage_end:
            bits.append(f"latest {c.coverage_end}")
        if c.covers_window is False:
            bits.append("NO DATA IN REQUESTED WINDOW")
        elif c.covers_window is None:
            bits.append("window unknown")
        if c.row_count == 0:
            bits.append("EMPTY TABLE")
        lines.append("- " + " | ".join(bits))

    if res.uncovered and res.window is not None:
        w_start, w_end = res.window
        lines.append(
            f"## Coverage: NONE of the ranked candidates has data reaching the "
            f"requested period {w_start}..{w_end}. The latest "
            f"available data in them ends {res.latest_available or 'unknown'}. "
            f"Answer by disclosing this — do not report the requested period as "
            f"zero."
        )
    if res.unknown_coverage:
        shown = ", ".join(res.unknown_coverage[:6])
        lines.append(
            f"## Coverage: no catalog data window is stored for {shown}"
            + (" …" if len(res.unknown_coverage) > 6 else "")
            + ". Their coverage is UNKNOWN, not empty."
        )
    for note in res.notes[:4]:
        lines.append(f"## Retrieval note: {note}")
    return "\n".join(lines)


def candidate_names(res: RetrievalResult, k: int = 4) -> list[str]:
    """The deterministic fallback selection: top-k candidates by rank.

    Used when the model's own selection contains no table that exists. That is
    strictly better than the two alternatives it replaced: returning the
    hallucinated names (which produced "table 'contracts' doesn't exist"), or
    dumping the whole catalog into an M-Schema fallback.
    """
    return [c.table_name for c in res.candidates[:k]]


def reroute_uncovered(
    selected: list[str],
    toc: list[dict],
    res: RetrievalResult,
    window: tuple[date, date] | None,
    classification_facts: dict | None = None,
    question_scope: dict | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Swap out a selected table whose catalog window CANNOT hold the period.

    A table whose data ENDS before the question window starts is a guaranteed
    0 rows — that is what ``_covers`` measures, and the candidate list already
    carries the verdict as ``covers_window``. Until now nothing ACTED on it:
    the annotation reached the prompt and the answer, never the selection.

    Measured live on 2026-09-20, a dated Chinese-language reporting question
    (window 2026-09-14..2026-09-20):

      * the freshest sales fact table ranked #1 (score 5.73,
        ``covers_window=True``) — and was NOT selected;
      * the selector chose a different real catalog table whose data ended
        before the window (``covers_window=False``) — the retrieval's
        top-ranked candidate was discarded (retrieval was only consulted
        when the selection matched nothing that exists);
      * the query returned 0 rows and the pipeline wrote an articulate
        "no data, unknown not zero" answer instead of the sales figures.

    So: replace each selected table convicted by a MEASURED ``covers_window is
    False`` with the highest-ranked covering candidate the selection omitted.
    Deliberately narrow, so the invariants hold:

    * an UNKNOWN window (``None``) never convicts — a missing measurement is
      reported as missing, exactly as ``_covers`` documents;
    * tables are never dropped-without-replacement: if no covering candidate
      remains, the original selection stands and the existing coverage
      disclosure carries the message;
    * the returned selection is never empty;
    * this is a first-query REROUTE, not a filter — the full catalog is still
      rendered to the selector, so nothing becomes unreachable.

    Returns ``(selected, [(dropped, added), …])``; the pairs are for logging.
    """
    if not selected or window is None or not res.candidates:
        return selected, []

    q_start, _ = window
    by_name = {t.get("table_name"): t for t in toc if t.get("table_name")}

    def _ends_before_window(name: str) -> bool:
        entry = by_name.get(name)
        if entry is None:  # not in the catalog — leave it alone
            return False
        end = _parse_day(entry.get("max_date"))
        return end is not None and end < q_start

    spare = [c for c in res.candidates if c.covers_window and c.table_name not in selected]
    if not spare:
        return selected, []

    # ── 2B-2: scope compatibility of substitutes ────────────────────────────
    # A regional partition must never answer a company-wide question merely
    # because its date coverage is better. With classification facts present,
    # a substitute must be scope-compatible; an incompatible one is SKIPPED,
    # and a convicted table with no compatible spare keeps the ORIGINAL
    # selection (the never-dropped-without-replacement invariant) — the
    # existing coverage disclosure carries the message, and the scope gate
    # (2B-3) is the enforcement layer. Facts absent → behavior unchanged.
    def _scope_compatible(name: str) -> bool:
        if classification_facts is None:
            return True
        f = classification_facts.get(name) or {}
        ds = f.get("dataset_scope")
        asked_value = str((question_scope or {}).get("value") or "").strip().lower()
        if asked_value:
            if ds in (None, "global"):
                return True
            pv = str(f.get("partition_value") or "").strip().lower()
            return pv == asked_value
        # Universal (company-wide) question: only global or unknown-scope
        # datasets may substitute — a partition is never scope-compatible.
        return ds is None or ds == "global"

    skipped_scope: list[str] = []

    out: list[str] = []
    swaps: list[tuple[str, str]] = []
    for name in selected:
        if spare and _ends_before_window(name):
            replacement = None
            for idx, cand in enumerate(spare):
                if _scope_compatible(cand.table_name):
                    replacement = spare.pop(idx).table_name
                    break
            if replacement is None:
                skipped_scope.append(name)
                logger.warning(
                    "reroute: table %r needs replacement but no "
                    "scope-compatible candidate exists — keeping it and "
                    "disclosing rather than silently swapping scope",
                    name,
                )
                out.append(name)
                continue
            swaps.append((name, replacement))
            out.append(replacement)
        else:
            out.append(name)
    if skipped_scope:
        # Reader-usable sentence (concern 3): the note below reaches the SQL
        # prompt; this one must reach the disclosure channel the caller owns.
        res.scope_refusals.append(
            "For "
            + ", ".join(sorted(set(skipped_scope)))
            + " a fresher replacement covering the requested period exists "
            "only for a different data partition, so no substitution was "
            "made; figures from the original table may not match the scope "
            "the question asked about."
        )
        try:
            res.notes.append(
                "scope: no scope-compatible replacement for "
                + ", ".join(skipped_scope)
                + " — selection unchanged rather than cross-scope swapped"
            )
        except Exception:  # notes list is best-effort
            pass
    return (out or selected), swaps


def scope_disclosure_sentence(
    selected: list[str],
    facts: dict,
    question_scope: dict | None,
) -> str:
    """The user-facing scope sentence for the FINAL selection (concern 3).

    Pure. Built from the same ``effective_classification`` facts the ranking
    used, so what it says is what the pipeline actually knew:

    * a question that names a partition but draws on a DIFFERENT confirmed
      partition gets a mismatch sentence;
    * a universal (company-wide) question drawing on confirmed partition
      datasets gets a "may not represent the whole" sentence;
    * a universal question drawing on a ``partition_family`` dataset (family
      shape detected, key NOT identified) gets an uncertainty sentence —
      per the reviewer's table: probable partition must not silently claim
      completeness, but an unverified guess must never be stated as fact.

    Unknown or absent scope produces nothing: missing evidence must not read
    as either safe or unsafe (concern 1 — unknown ≠ global, in both
    directions), and a VERIFIED-global dataset among the selection makes
    completeness achievable, so no uncertainty is claimed for it. Returns ""
    when there is nothing to disclose.
    """
    if not selected or not facts:
        return ""
    asked = str((question_scope or {}).get("value") or "").strip().lower()
    mismatches: list[str] = []
    partitions: list[str] = []
    families: list[str] = []
    has_global = any(
        str((facts.get(name) or {}).get("dataset_scope") or "") == "global"
        for name in selected
    )
    for name in selected:
        f = facts.get(name) or {}
        ds = str(f.get("dataset_scope") or "")
        if not ds:
            continue
        pv = str(f.get("partition_value") or "").strip()
        if asked:
            if ds.startswith("partition:") and pv and pv.lower() != asked:
                mismatches.append(f"{name} holds {pv or 'a different'} data")
        elif ds.startswith("partition:"):
            if name not in partitions:
                partitions.append(name)
        elif ds == "partition_family" and not has_global:
            if name not in families:
                families.append(name)
    if mismatches:
        return (
            "Note: the question asks about "
            f"{asked!r}, but the answer draws on "
            + " and ".join(sorted(set(mismatches)))
            + "; those figures do not match the scope asked about."
        )
    parts: list[str] = []
    if partitions:
        parts.append(
            "the answer draws on " + ", ".join(sorted(set(partitions)))
            + ", which hold a single partition of the data rather than the "
            "whole dataset"
        )
    if families:
        parts.append(
            "the answer draws on " + ", ".join(sorted(set(families)))
            + ", whose scope could not be fully verified — they may hold "
            "only part of the data"
        )
    if not parts:
        return ""
    return (
        "Note: " + "; ".join(parts) +
        " — figures may not represent the full scope asked about."
    )
