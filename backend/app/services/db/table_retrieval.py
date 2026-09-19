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
    ``_ref_material_mapping``, ``access_model_config``, ``actual_price``,
    ``auth_*``… and then "... and 128 more tables". Not one business table was
    in the surface it chose from.
  * Asked about a sales report it therefore picked ``actual_price`` (data ends
    2025-07-31) and ``chain_product_kind`` (0 rows) → 0 rows → "no data".
  * Asked about contract execution it returned ``contract_execution``,
    ``contract``, ``contract_execution_detail`` — names that do not exist.
  * Holding the model and question fixed and raising only that budget to 6,000
    made it pick ``erp_t_crm_contract``, ``erp_t_crm_contractentry``,
    ``erp_t_crm_contractfin``, ``erp_v_contract_execution``. The model was
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

logger = logging.getLogger(__name__)

# Weights are deliberate and ordered: a VERIFIED plan outranks a domain
# assignment, which outranks lexical/semantic name evidence. Popularity alone
# can never create a candidate — that rule lives in
# ``SemanticStore.rank_relations`` and is preserved by only adding to its score.
_W_PLAN = 3.0
_W_DOMAIN = 1.5


@dataclass
class TableCandidate:
    """One table offered to the table-selection stage, with its evidence."""

    table_name: str
    score: float
    evidence: list[str] = field(default_factory=list)
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
) -> RetrievalResult:
    """Rank the catalog tables for this question. Deterministic, read-only.

    ``toc`` is the already-built table-of-contents (one dict per catalog table
    with ``table_name`` / ``domain_name`` / ``row_count`` / ``min_date`` /
    ``max_date``), so this adds no catalog round-trip of its own.
    """
    result = RetrievalResult(window=window)
    if not toc or not question:
        return result

    by_name = {t.get("table_name"): t for t in toc if t.get("table_name")}
    scores: dict[str, float] = {}
    evidence: dict[str, list[str]] = {}

    def _add(name: str, amount: float, why: str) -> bool:
        """Add evidence for a name IN THE CATALOG. Returns whether it was kept."""
        if name not in by_name:
            return False  # never offer a table outside the catalog the user may read
        scores[name] = scores.get(name, 0.0) + amount
        evidence.setdefault(name, []).append(why)
        return True

    # ── 1. semantic / lexical evidence (SEMANTIC_STORE_ENABLED) ──────────────
    if enabled:
        try:
            from app.services.db.semantic_store import get_semantic_store

            hits = get_semantic_store(
                db, list(kb_ids), project_id=project_id
            ).rank_relations(question, k=limit)
            for h in hits:
                added = _add(h.table_name, float(h.score), "name/alias evidence")
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
                _add(name, _W_PLAN, "verified plan for this question")
    except Exception as e:
        logger.debug("table_retrieval: plan memory unavailable: %s", e)

    # ── 3. business-domain assignment (DISCOVERED) ──────────────────────────
    try:
        from app.services.knowledge_graph.domain_discovery import (
            scoped_tables_for_question,
        )

        scoped, matched = scoped_tables_for_question(db, list(kb_ids), question)
        label = ", ".join(
            d.get("name_en") or d.get("name_zh") or "" for d in (matched or [])
        ).strip(", ")
        for name in scoped or []:
            _add(name, _W_DOMAIN, f"domain {label}" if label else "domain match")
    except Exception as e:
        logger.debug("table_retrieval: domain scoping unavailable: %s", e)

    # ── 4. coverage annotation — a constraint, not a filter ─────────────────
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
    """
    if not res.candidates:
        return ""
    lines = [
        "## Ranked candidate tables (deterministic retrieval over the catalog; "
        "these carry evidence, in rank order)"
    ]
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
    return "\n".join(lines)


def candidate_names(res: RetrievalResult, k: int = 4) -> list[str]:
    """The deterministic fallback selection: top-k candidates by rank.

    Used when the model's own selection contains no table that exists. That is
    strictly better than the two alternatives it replaced: returning the
    hallucinated names (which produced "table 'contracts' doesn't exist"), or
    dumping the whole catalog into an M-Schema fallback.
    """
    return [c.table_name for c in res.candidates[:k]]
