"""Integration tests for ``collect_enterprise_data`` tool wrapper.

Design spec: §5 data flow.

The wrapper composes four orchestrator phases:
    profile → execute → synthesize → verify

These tests verify the wrapper's contracts in isolation by mocking the
orchestrator functions (no DB / no LLM required). The wrapper imports
the orchestrator functions lazily at call time, so the mocks are
applied at the source module level
(``app.services.enterprise_orchestrator.*``).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.tool_handlers.enterprise_data_tools import (
    COLLECT_ENTERPRISE_DATA_SCHEMA,
    _collect_enterprise_data,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_intent():
    """A minimal valid EnterpriseIntent dict."""
    return {
        "domain": "sales",
        "primary_metric": "revenue",
        "period": {"label": "Q3 2026"},
        "facets": [
            {"name": "primary", "type": "ad_hoc", "args": {"sql": "SELECT 1"}},
        ],
    }


@pytest.fixture
def fake_facets():
    """Facets result with one facet carrying rows."""
    return {
        "primary": {
            "rows": [{"month": "Jul", "revenue": 1000.0}],
            "sql": "SELECT 1",
            "available": True,
        },
    }


@pytest.fixture
def fake_payload():
    """A minimal enterprise report payload (6 sections, claims, etc.)."""
    return {
        "enterprise_report_kind": "executive",
        "title": "Q3 2026 Sales Report",
        "period_label": "Q3 2026",
        "source_label": "erp_sales",
        "executive_summary": "Revenue grew 12% in Q3 driven by Product A.",
        "primary_metric_breakdown": {"available": True, "kpis": []},
        "segment_breakdown": {"available": True, "table": {"headers": [], "rows": []}},
        "drivers": {"narrative": "Key drivers..."},
        "risks": {"narrative": "Key risks..."},
        "actions": {"items": []},
        "claims": [
            {
                "claim_id": "c1",
                "text": "Revenue grew 12%.",
                "source_facet": "primary",
                "source_row_ids": ["r1"],
                "source_sql": "SELECT 1",
                "verified": True,
            }
        ],
    }


def _patch_orchestrator(monkeypatch, *, intent=None, facets=None, payload=None):
    """Patch the orchestrator functions at their source module so the
    wrapper's lazy import resolves to the mocks.

    The wrapper does
    ``from app.services.enterprise_orchestrator import (profile_enterprise_intent, ...)``
    inside the function, so the names are resolved from the
    ``enterprise_orchestrator`` module's namespace at call time. Patching
    that namespace intercepts the call.
    """
    import app.services.enterprise_orchestrator as eo

    if intent is not None:
        monkeypatch.setattr(eo, "profile_enterprise_intent", lambda *a, **kw: intent)
    if facets is not None:
        async def _facets_coro(*a, **kw):
            return facets
        monkeypatch.setattr(eo, "execute_facets", _facets_coro)
    if payload is not None:
        monkeypatch.setattr(
            eo, "synthesize_enterprise_report",
            lambda intent, facets, prior_period_facets=None: payload,
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def test_schema_is_well_formed():
    """The tool schema must declare ``query`` as a required string."""
    assert COLLECT_ENTERPRISE_DATA_SCHEMA["name"] == "collect_enterprise_data"
    params = COLLECT_ENTERPRISE_DATA_SCHEMA["parameters"]
    assert "query" in params["properties"]
    assert params["properties"]["query"]["type"] == "string"
    assert "query" in params["required"]


# ---------------------------------------------------------------------------
# Fail-open: empty query
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_query_returns_error():
    result = await _collect_enterprise_data({"query": ""}, db=None, user_id="u1")
    assert result["success"] is False
    assert "required" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_missing_query_returns_error():
    result = await _collect_enterprise_data({}, db=None, user_id="u1")
    assert result["success"] is False


# ---------------------------------------------------------------------------
# Fail-open: profiler returns None
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_profiler_none_returns_not_business_query(monkeypatch):
    """When the profiler can't build an intent, the wrapper falls through."""
    import app.services.enterprise_orchestrator as eo

    monkeypatch.setattr(eo, "profile_enterprise_intent", lambda *a, **kw: None)

    result = await _collect_enterprise_data(
        {"query": "what is the meaning of life?"}, db=None, user_id="u1",
    )
    assert result["success"] is False
    assert result["reason"] == "not_business_query"


# ---------------------------------------------------------------------------
# Happy path: full pipeline
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_pipeline_returns_executive_payload(
    monkeypatch, fake_intent, fake_facets, fake_payload,
):
    """When all four phases succeed, the wrapper returns a full
    EnterpriseReport payload with ``enterprise_report_kind ==
    "executive"``."""
    _patch_orchestrator(
        monkeypatch, intent=fake_intent, facets=fake_facets, payload=fake_payload,
    )

    result = await _collect_enterprise_data(
        {"query": "Give me Q3 2026 sales report"},
        db=None,
        user_id="u1",
        context={"bound_kb_ids": []},
    )
    assert result["success"] is True
    assert result["enterprise_report_kind"] == "executive"
    assert result["title"] == "Q3 2026 Sales Report"
    # Answer must be a non-empty markdown string from the html renderer.
    assert isinstance(result["answer"], str)
    assert result["answer"].strip()
    # Rows must be flattened from the primary facet.
    assert result["rows"] == fake_facets["primary"]["rows"]
    # Full payload must be returned for downstream docx rendering.
    assert result["payload"]["enterprise_report_kind"] == "executive"


# ---------------------------------------------------------------------------
# Rendered answer contains the exec summary
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rendered_answer_contains_summary(
    monkeypatch, fake_intent, fake_facets, fake_payload,
):
    """The chat-rendered markdown must include the executive summary."""
    _patch_orchestrator(
        monkeypatch, intent=fake_intent, facets=fake_facets, payload=fake_payload,
    )

    result = await _collect_enterprise_data(
        {"query": "Q3 sales"},
        db=None,
        user_id="u1",
        context={},
    )
    assert "Executive Summary" in result["answer"] or "Revenue grew" in result["answer"]


# ---------------------------------------------------------------------------
# Fail-open: facet execution error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_facets_failure_returns_error(monkeypatch, fake_intent):
    import app.services.enterprise_orchestrator as eo

    monkeypatch.setattr(eo, "profile_enterprise_intent", lambda *a, **kw: fake_intent)

    async def _boom(*a, **kw):
        raise RuntimeError("facet timeout")

    monkeypatch.setattr(eo, "execute_facets", _boom)

    result = await _collect_enterprise_data(
        {"query": "Q3 sales"}, db=None, user_id="u1",
    )
    assert result["success"] is False
    assert result["reason"] == "facet_execution_failed"


# ---------------------------------------------------------------------------
# Fail-open: synthesis error
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_synthesize_failure_returns_error(
    monkeypatch, fake_intent, fake_facets,
):
    import app.services.enterprise_orchestrator as eo

    monkeypatch.setattr(eo, "profile_enterprise_intent", lambda *a, **kw: fake_intent)

    async def _facets_coro(*a, **kw):
        return fake_facets
    monkeypatch.setattr(eo, "execute_facets", _facets_coro)

    def _boom(intent, facets, prior_period_facets=None):
        raise RuntimeError("synthesis failed")

    monkeypatch.setattr(eo, "synthesize_enterprise_report", _boom)

    result = await _collect_enterprise_data(
        {"query": "Q3 sales"}, db=None, user_id="u1",
    )
    assert result["success"] is False
    assert result["reason"] == "synthesis_failed"


# ---------------------------------------------------------------------------
# Claim verification with no db_executor keeps claims unverified
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_claims_get_caveat_when_no_db_executor(
    monkeypatch, fake_intent, fake_facets,
):
    """When no bound KB is available, the wrapper passes db_executor=None
    and ``rewrite_unverified`` replaces claim text with the standard
    caveat — no fabricated numbers."""
    from app.services.enterprise_orchestrator.claim_tracker import UNVERIFIED_TEXT

    payload_with_unverified = {
        "enterprise_report_kind": "executive",
        "title": "Q3 2026 Sales Report",
        "executive_summary": "Revenue grew 12%.",
        "primary_metric_breakdown": {"available": True},
        "segment_breakdown": {"available": True},
        "drivers": {"narrative": "..."},
        "risks": {"narrative": "..."},
        "actions": {"items": []},
        "claims": [
            {
                "claim_id": "c1",
                "text": "Revenue grew 12%.",
                "source_facet": "primary",
                "source_row_ids": ["r1"],
                "source_sql": "SELECT 1",
                "verified": False,
            }
        ],
    }

    _patch_orchestrator(
        monkeypatch, intent=fake_intent, facets=fake_facets,
        payload=payload_with_unverified,
    )

    # No bound_kb_ids → no db_executor → claim stays unverified → rewrite
    result = await _collect_enterprise_data(
        {"query": "Q3 sales"}, db=None, user_id="u1",
        context={"bound_kb_ids": []},
    )
    assert result["success"] is True
    # The claim text must have been rewritten to the caveat.
    assert result["payload"]["claims"][0]["text"] == UNVERIFIED_TEXT

