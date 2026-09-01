"""``collect_enterprise_data`` — enterprise business-data tool wrapper.

This is the single tool the agent calls when the user asks for a
multi-facet business report. The wrapper composes the four phases of the
enterprise pipeline:

    1. profile    → ``profile_enterprise_intent(user_message, schema_slice)``
    2. execute    → ``execute_facets(intent, db, kb, context, user_id)``
    3. synthesize → ``synthesize_enterprise_report(intent, facets)``
    4. verify     → ``verify_claims`` + ``rewrite_unverified`` (claims)

The wrapper returns a payload dict carrying the full ``EnterpriseReport``
payload (``payload`` key) plus a chat-rendered markdown narrative
(``answer`` key). The orchestrator routes the same payload into the
DOCX exporter via ``render_enterprise_docx`` when the user requests a
downloadable file.

Fail-open contract:
    * Profiler returns ``None`` (non-business query / LLM failure) →
      wrapper returns ``{"success": False, "reason": "not_business_query"}``
      so the caller falls through to the existing single-query path.
    * Claim verification with no ``db_executor`` → claims stay unverified
      and ``rewrite_unverified`` replaces their text with the standard
      "Data unavailable" placeholder (safe default, no fabrication).

Design spec: ``docs/superpowers/specs/2026-08-24-business-data-executive-pipeline-design.md``
"""

from __future__ import annotations

import logging
from typing import Any

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------

COLLECT_ENTERPRISE_DATA_SCHEMA: dict[str, Any] = {
    "name": "collect_enterprise_data",
    "description": (
        "Collect, verify, and synthesize a 6-section executive business-data "
        "report (financial performance, supply chain, sales ops, risk, HR, "
        "procurement). Profiled intent → multi-facet parallel data collection "
        "→ grounded narrative with claim verification → rendered as inline "
        "markdown in chat and a downloadable DOCX on request."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The user's business question in natural language, e.g. "
                    "'Q3 2026 sales by region and product line'. This is the "
                    "raw user message — the profiler derives intent, period, "
                    "domain, and primary metric from it."
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
        logger.warning("enterprise_data_tools: llm call failed: %s", exc)
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
            logger.info("enterprise_data_tools: db_executor failed: %s", exc)
            return []

    return _executor


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

async def _collect_enterprise_data(
    args: dict,
    db,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Tool handler — see module docstring for the full contract."""
    query = (args.get("query") or "").strip()
    if not query:
        return {"success": False, "error": "query is required"}

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
        logger.info("enterprise_data_tools: schema slice unavailable: %s", exc)

    try:
        from app.services.enterprise_orchestrator import (
            profile_enterprise_intent,
            execute_facets,
            synthesize_enterprise_report,
        )
        from app.services.enterprise_orchestrator.claim_tracker import (
            ClaimTracker,
            verify_claims,
            rewrite_unverified,
        )

        intent = profile_enterprise_intent(
            query,
            schema_slice=schema_slice_text,
            llm_caller=_fast_llm_call,
        )
    except Exception as exc:
        logger.warning("enterprise_data_tools: profiler raised: %s", exc)
        intent = None

    if intent is None:
        # Fail-open: caller falls through to the existing single-query path.
        return {
            "success": False,
            "reason": "not_business_query",
            "enterprise_report_kind": "executive",  # marker for consistency
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
        logger.info("enterprise_data_tools: kb lookup failed: %s", exc)

    try:
        facets = await execute_facets(
            intent,
            db=db,
            kb=kb,
            context=context,
            user_id=user_id,
        )
    except Exception as exc:
        logger.warning("enterprise_data_tools: execute_facets failed: %s", exc)
        return {
            "success": False,
            "error": f"facet execution failed: {exc}",
            "reason": "facet_execution_failed",
            "enterprise_report_kind": "executive",
        }

    # ── Phase 3: Synthesize the 6-section report ──────────────────────────
    try:
        payload = synthesize_enterprise_report(intent, facets)
    except Exception as exc:
        logger.warning("enterprise_data_tools: synthesize failed: %s", exc)
        return {
            "success": False,
            "error": f"synthesis failed: {exc}",
            "reason": "synthesis_failed",
            "enterprise_report_kind": "executive",
        }

    # ── Phase 4: Verify claims (best-effort) ─────────────────────────────
    # When no db_executor is available (no bound KB or DB layer disabled),
    # all claims stay unverified and rewrite_unverified replaces their
    # text with the standard caveat — the LLM never sees unverified
    # numbers in the executive document.
    try:
        tracker = ClaimTracker()
        tracker.extend(payload.get("claims") or [])
        db_executor = _build_db_executor(db, bound_kb_ids[0] if bound_kb_ids else None)
        verify_claims(tracker, db_executor=db_executor)
        rewrite_unverified(payload.get("claims") or [])
    except Exception as exc:
        logger.info("enterprise_data_tools: claim verification skipped: %s", exc)

    # ── Render the chat narrative ────────────────────────────────────────
    # The HTML renderer produces inline-markdown for the chat bubble; the
    # DOCX exporter (generation_orchestrator → docx_export.render →
    # render_enterprise_docx) uses the SAME payload, so the file
    # mirrors the chat.
    try:
        from app.services.enterprise_orchestrator.renderers import (
            render_enterprise_html,
        )
        answer_markdown = render_enterprise_html(payload)
    except Exception as exc:
        logger.warning("enterprise_data_tools: html render failed: %s", exc)
        answer_markdown = payload.get("executive_summary") or ""

    # Flatten the primary-facet rows for the data-mining compatibility
    # path (MessageBubble's DataTableCard) — enterprise reports render
    # the full narrative, but downstream consumers (history reuse, the
    # `ask_data_agent` compatibility shim) still expect `rows`.
    primary = facets.get("primary") or {}
    primary_rows = primary.get("rows") or []
    if not primary_rows:
        # Fall back to the richest facet that has rows.
        for _fname, fdata in (facets or {}).items():
            if isinstance(fdata, dict) and fdata.get("rows"):
                primary_rows = fdata.get("rows") or []
                break

    return {
        "success": True,
        "enterprise_report_kind": "executive",
        "title": payload.get("title") or "Executive Report",
        "answer": answer_markdown,
        "rows": primary_rows,
        "payload": payload,
        "source_id": bound_kb_ids[0] if bound_kb_ids else None,
        "source_name": payload.get("source_label") or "",
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

registry.register(
    name="collect_enterprise_data",
    schema=COLLECT_ENTERPRISE_DATA_SCHEMA,
    handler=_collect_enterprise_data,
    category="delegation",
    enabled_by_default=False,  # only when ENTERPRISE_PIPELINE_ENABLED + KB bound
    description=(
        "Run the full enterprise business-data pipeline (profile → "
        "multi-facet execute → synthesize → verify) and return a "
        "6-section executive report as inline markdown + a payload "
        "ready for DOCX rendering."
    ),
    is_async=True,
)
