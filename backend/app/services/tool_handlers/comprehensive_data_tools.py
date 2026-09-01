"""``comprehensive_data`` — profile-driven multi-facet data tool.

Replaces the legacy ``collect_enterprise_data`` (kept as a back-compat
shim in ``enterprise_data_tools.py``). Supports pluggable profiles:

    * ``enterprise``  (default) — original 6-section executive report.
    * ``market``      — institutional-grade market overview / weekly
                        digest / trend report (8 dimensions).

The wrapper runs the same four-phase pipeline regardless of profile:

    1. profile    → ``profiler.profile_enterprise_intent(query, schema_slice, profile_name=...)``
    2. execute    → ``executor.execute_facets(intent, db, kb, context, user_id)``
    3. synthesize → ``synthesizer.synthesize_enterprise_report`` or
                    ``synthesizer.synthesize_market_report``
    4. verify     → ``verify_claims`` + ``rewrite_unverified``

Profiles are registered in ``app/services/enterprise_orchestrator/profiles/``;
adding a new profile is one file there plus a flag gate in ``config.py``.

Fail-open contract:
    * Profiler returns ``None`` → wrapper emits
      ``{"success": False, "reason": "not_business_query"}`` so the
      caller falls through to the existing single-query path.
    * Claim verification with no ``db_executor`` → claims stay
      unverified and ``rewrite_unverified`` replaces their text with
      the standard caveat (no fabrication).

Backwards compatibility:
    The legacy ``collect_enterprise_data(query)`` tool remains
    available — it now delegates to ``_comprehensive_data`` with
    ``profile="enterprise"`` so existing callers + 80+ tests stay
    untouched.

Design spec: ``docs/superpowers/specs/2026-08-25-comprehensive-data-market-profile.md``
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

COMPREHENSIVE_DATA_SCHEMA: dict[str, Any] = {
    "name": "comprehensive_data",
    "description": (
        "Profile-driven multi-facet data collection + synthesis. Choose "
        "``profile=\"enterprise\"`` for executive business reports "
        "(financial / supply chain / sales ops / risk / HR / procurement) "
        "or ``profile=\"market\"`` for institutional-grade market "
        "overviews, weekly digests, and trend reports (8 mandatory "
        "dimensions: core_metrics, historical_trends, cost_structure, "
        "supply_side, demand_side, macro_context, forward_indicators, "
        "cross_segment_relationships). Always call this BEFORE "
        "``create_artifact(type=\"pptx\", ...)`` for market / weekly "
        "digest / trend-report requests — the resulting payload carries "
        "``coverage_dimensions`` which the artifact-coverage gate "
        "inspects."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The user's natural-language question (e.g. 'weekly "
                    "crude oil market overview', 'Q3 sales by region'). "
                    "The profiler derives intent, period, primary metric, "
                    "and facet plan from it."
                ),
            },
            "profile": {
                "type": "string",
                "enum": ["enterprise", "market"],
                "default": "enterprise",
                "description": (
                    "Profile id. ``enterprise`` = legacy 6-section "
                    "executive report. ``market`` = institutional-grade "
                    "4-section market overview with 8 mandatory "
                    "dimensions. Defaults to ``enterprise`` so legacy "
                    "callers (no explicit profile) keep their behavior."
                ),
            },
        },
        "required": ["query"],
    },
}


# ---------------------------------------------------------------------------
# LLM caller (profiler is JSON-only, sync)
# ---------------------------------------------------------------------------

def _fast_llm_call(prompt: str) -> dict:
    """Lightweight sync JSON completion used by the profiler.

    Returns ``{}`` on any failure so the profiler can fall through to
    its fail-open branch (returns ``None`` → wrapper emits the
    ``not_business_query`` marker).
    """
    try:
        from app.services.llm_service import chat_completion_json_sync
        return chat_completion_json_sync(prompt, schema=None, temperature=0.0) or {}
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("comprehensive_data_tools: llm call failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# SQL executor (for claim verification)
# ---------------------------------------------------------------------------

def _build_db_executor(db, kb_id: str | None):
    """Return a ``Callable[[str], list[dict]]`` that re-executes SQL against
    the bound KB's warehouse, or ``None`` when no bound KB is available.

    The returned executor is used by ``verify_claims`` to re-run each
    claim's ``source_sql`` and confirm the numbers still match. When
    ``kb_id`` is ``None`` (no bound data source), the wrapper passes
    ``None`` to ``verify_claims`` so all claims stay unverified and
    ``rewrite_unverified`` replaces their text with the standard
    caveat — never fabricated numbers.
    """
    if db is None or not kb_id:
        return None

    def _executor(sql: str) -> list[dict]:
        try:
            from app.services.db import QueryService
            result = QueryService(db).execute(kb_id, sql, max_rows=100, timeout_s=20)
            return result.get("rows") or []
        except Exception as exc:
            logger.info("comprehensive_data_tools: db_executor failed: %s", exc)
            return []

    return _executor


# ---------------------------------------------------------------------------
# Profile flag gating
# ---------------------------------------------------------------------------

def _profile_enabled(profile_name: str) -> bool:
    """Check flag gating for a profile (default ON for ``enterprise``)."""
    from app.config import settings

    if profile_name == "enterprise":
        # The enterprise profile is on by default (matches existing
        # ENTERPRISE_PIPELINE_ENABLED contract).
        return bool(getattr(settings, "ENTERPRISE_PIPELINE_ENABLED", False))
    if profile_name == "market":
        return bool(getattr(settings, "COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED", False))
    return False


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

async def _comprehensive_data(
    args: dict,
    db,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Tool handler — runs the full profile-driven pipeline.

    Args:
        args:     ``{"query": str, "profile": "enterprise"|"market"}``
        db:       SQLAlchemy session (for schema slice + KB lookup).
        user_id:  Acting user.
        context:  Bound-KB ids + flags passed in by the agent runtime.

    Returns: ``{"success": bool, ...}`` — on success, includes the full
             payload under ``"payload"`` (artifact-coverage gate
             inspects ``payload["coverage_dimensions"]``).
    """
    query = (args.get("query") or "").strip()
    profile_name = (args.get("profile") or "enterprise").strip() or "enterprise"
    if not query:
        return {"success": False, "error": "query is required"}

    # Fail-open / opt-in: a disabled profile returns a structured
    # ``profile_disabled`` marker so the caller can fall through to
    # the existing path or surface a clear error.
    if not _profile_enabled(profile_name):
        logger.info(
            "comprehensive_data: profile='%s' is gated OFF; returning profile_disabled",
            profile_name,
        )
        return {
            "success": False,
            "reason": "profile_disabled",
            "profile": profile_name,
        }

    # Unknown profile — surface the failure explicitly so the agent
    # doesn't silently get enterprise behavior.
    try:
        from app.services.enterprise_orchestrator.profiles import get_profile
        profile = get_profile(profile_name)
    except Exception as exc:
        logger.info("comprehensive_data: unknown profile='%s': %s", profile_name, exc)
        return {
            "success": False,
            "reason": "unknown_profile",
            "profile": profile_name,
        }

    context = context or {}
    bound_kb_ids: list[str] = list(context.get("bound_kb_ids") or [])

    # ── Phase 1: Profile intent ──────────────────────────────────────────
    # Build the schema slice (cached per-bound-KB) so the profiler can
    # pick the right domain / primary metric / facets.
    schema_slice_text = ""
    try:
        from app.services.data_source_runtime.data_source_runtime import _build_schema_slice
        slices = _build_schema_slice(db, bound_kb_ids) if bound_kb_ids else {}
        # `_build_schema_slice` returns ``dict[kb_id, schema_text]`` —
        # the profiler takes a single concatenated string.
        schema_slice_text = "\n\n---\n\n".join(
            f"[{kb_id}]\n{s}" for kb_id, s in (slices or {}).items()
        )
    except Exception as exc:
        logger.info("comprehensive_data_tools: schema slice unavailable: %s", exc)

    try:
        from app.services.enterprise_orchestrator import profile_enterprise_intent
        from app.services.enterprise_orchestrator.profiler import (
            MARKET_MIN_FACETS,
            MARKET_MAX_FACETS,
            MIN_FACETS,
            MAX_FACETS,
        )
        if profile_name == "market":
            min_f, max_f = MARKET_MIN_FACETS, MARKET_MAX_FACETS
        else:
            min_f, max_f = MIN_FACETS, MAX_FACETS
        intent = profile_enterprise_intent(
            query,
            schema_slice=schema_slice_text,
            llm_caller=_fast_llm_call,
            profile_name=profile_name,
            min_facets=min_f,
            max_facets=max_f,
        )
    except Exception as exc:
        logger.warning("comprehensive_data_tools: profiler raised: %s", exc)
        intent = None

    if intent is None:
        # Fail-open: caller falls through to the existing single-query path.
        return {
            "success": False,
            "reason": "not_business_query",
            "profile": profile_name,
            "enterprise_report_kind": (
                "market_overview" if profile_name == "market" else "executive"
            ),
        }

    # ── Phase 2: Execute facets in parallel ──────────────────────────────
    kb = None
    try:
        from app.models.knowledge_base import KnowledgeBase
        if bound_kb_ids:
            kb = (
                db.query(KnowledgeBase)
                .filter(
                    KnowledgeBase.id == bound_kb_ids[0],
                    KnowledgeBase.is_deleted == False,  # noqa: E712
                )
                .first()
            )
    except Exception as exc:
        logger.info("comprehensive_data_tools: kb lookup failed: %s", exc)

    from app.services.enterprise_orchestrator import execute_facets
    try:
        facets = await execute_facets(
            intent,
            db=db,
            kb=kb,
            context=context,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning("comprehensive_data_tools: execute_facets failed: %s", exc)
        return {
            "success": False,
            "error": f"facet execution failed: {exc}",
            "reason": "facet_execution_failed",
            "profile": profile_name,
            "enterprise_report_kind": (
                "market_overview" if profile_name == "market" else "executive"
            ),
        }

    # ── Phase 3: Synthesize the report (profile-aware) ──────────────────
    try:
        if profile_name == "market":
            from app.services.enterprise_orchestrator import (
                synthesize_market_report,
            )
            payload = synthesize_market_report(intent, facets)
        else:
            from app.services.enterprise_orchestrator import (
                synthesize_enterprise_report,
            )
            payload = synthesize_enterprise_report(intent, facets)
    except Exception as exc:
        logger.warning("comprehensive_data_tools: synthesize failed: %s", exc)
        return {
            "success": False,
            "error": f"synthesis failed: {exc}",
            "reason": "synthesis_failed",
            "profile": profile_name,
            "enterprise_report_kind": (
                "market_overview" if profile_name == "market" else "executive"
            ),
        }

    # ── Phase 4: Verify claims (best-effort) ─────────────────────────────
    # When no db_executor is available (no bound KB or DB layer disabled),
    # all claims stay unverified and rewrite_unverified replaces their
    # text with the standard caveat — the LLM never sees unverified
    # numbers in the executive document.
    try:
        from app.services.enterprise_orchestrator.claim_tracker import (
            ClaimTracker,
            verify_claims,
            rewrite_unverified,
        )
        tracker = ClaimTracker()
        tracker.extend(payload.get("claims") or [])
        db_executor = _build_db_executor(db, bound_kb_ids[0] if bound_kb_ids else None)
        verify_claims(tracker, db_executor=db_executor)
        rewrite_unverified(payload.get("claims") or [])
    except Exception as exc:
        logger.info("comprehensive_data_tools: claim verification skipped: %s", exc)

    # ── Render the chat narrative ────────────────────────────────────────
    # HTML rendering for the chat bubble. DOCX / PPTX exports
    # downstream consume the SAME payload, so the file mirrors the chat.
    try:
        if profile_name == "market":
            # Market profile reuses the enterprise HTML renderer for now
            # (the renderer reads ``executive_summary`` / ``key_findings``
            # which the market payload also carries).
            from app.services.enterprise_orchestrator.renderers import (
                render_enterprise_html,
            )
            answer_markdown = render_enterprise_html(payload)
        else:
            from app.services.enterprise_orchestrator.renderers import (
                render_enterprise_html,
            )
            answer_markdown = render_enterprise_html(payload)
    except Exception as exc:
        logger.warning("comprehensive_data_tools: html render failed: %s", exc)
        answer_markdown = (
            (payload.get("executive_summary") or {}).get("summary")
            if isinstance(payload.get("executive_summary"), dict)
            else payload.get("executive_summary") or ""
        )

    # Flatten the primary-facet rows for the data-mining compatibility
    # path (MessageBubble's DataTableCard) — market/enterprise reports
    # render the full narrative, but downstream consumers (history reuse,
    # the ``ask_data_agent`` compatibility shim) still expect ``rows``.
    primary = facets.get("primary") or {}
    primary_rows = primary.get("rows") or []
    if not primary_rows:
        # Fall back to the richest facet that has rows.
        for _fname, fdata in (facets or {}).items():
            if isinstance(fdata, dict) and fdata.get("rows"):
                primary_rows = fdata.get("rows") or []
                break

    coverage_dimensions = payload.get("coverage_dimensions") or []

    return {
        "success": True,
        "profile": profile_name,
        "enterprise_report_kind": (
            "market_overview" if profile_name == "market" else "executive"
        ),
        "title": payload.get("title") or "Executive Report",
        "answer": answer_markdown,
        "rows": primary_rows,
        "payload": payload,
        "coverage_dimensions": coverage_dimensions,
        "source_id": bound_kb_ids[0] if bound_kb_ids else None,
        "source_name": payload.get("source_label") or "",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="comprehensive_data",
    schema=COMPREHENSIVE_DATA_SCHEMA,
    handler=_comprehensive_data,
    category="delegation",
    enabled_by_default=False,  # gated by per-profile flag (see _profile_enabled)
    description=COMPREHENSIVE_DATA_SCHEMA["description"],
    is_async=True,
)
