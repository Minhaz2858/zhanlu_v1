"""Tests for the profiler (LLM-driven facet planner).

Design spec reference: §6 Dynamic Resource & Intent Profiler.

The profiler's public contract is the ``profile_enterprise_intent``
function, which is fail-open (returns ``None`` on any error). Tests use
an injectable ``llm_caller`` to avoid hitting any LLM service.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.enterprise_orchestrator.profiler import (
    build_profiler_prompt,
    profile_enterprise_intent,
    _normalize_intent,
    _repair_json,
    _sanitize_facet,
    _parse_period,
    SERVICE_WHITELIST,
    VALID_DOMAINS,
    MAX_FACETS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _llm(data):
    """Build an LLM stub that returns ``data`` directly."""
    def _caller(prompt: str) -> dict:
        return data
    return _caller


def _make_intent(domain="supply_chain", facets=None, primary="gross_margin_pct", period=None):
    return {
        "domain": domain,
        "period": period or {"start": "2026-07-25", "end": "2026-08-23"},
        "primary_metric": primary,
        "segments": [],
        "facets": facets or [],
    }


# ---------------------------------------------------------------------------
# prompt + JSON repair
# ---------------------------------------------------------------------------
class TestProfilerPrompt:
    def test_includes_user_message(self):
        prompt = build_profiler_prompt("Why is margin dropping?", "")
        assert "Why is margin dropping?" in prompt

    def test_truncates_long_user_message(self):
        prompt = build_profiler_prompt("x" * 5000, "")
        # user_message is sliced to 2000 chars; the whole prompt will
        # be larger than the slice, so we just check a substring fits.
        assert "x" * 2000 in prompt
        assert "x" * 5000 not in prompt

    def test_includes_schema_slice(self):
        prompt = build_profiler_prompt("q", "erp_v_sale_orderentry")
        assert "erp_v_sale_orderentry" in prompt

    def test_handles_empty_schema_slice(self):
        prompt = build_profiler_prompt("q", "")
        assert "no schema slice available" in prompt.lower()

    def test_output_schema_present(self):
        prompt = build_profiler_prompt("q", "sc")
        assert "primary_metric" in prompt and "facets" in prompt


class TestRepairJson:
    def test_parses_clean_json(self):
        repaired = _repair_json('{"a": 1, "b": [1, 2]}')
        assert repaired == {"a": 1, "b": [1, 2]}

    def test_strips_markdown_fences(self):
        repaired = _repair_json('```json\n{"a": 1}\n```')
        assert repaired == {"a": 1}

    def test_finds_first_braced_block(self):
        repaired = _repair_json('preamble {"a": 2} trailing')
        assert repaired == {"a": 2}

    def test_returns_none_on_garbage(self):
        assert _repair_json("not json at all") is None

    def test_returns_none_on_empty(self):
        assert _repair_json("") is None
        assert _repair_json("   ") is None
        assert _repair_json(None) is None  # type: ignore


class TestParsePeriod:
    def test_valid_period(self):
        assert _parse_period({"start": "2026-01-01", "end": "2026-01-31"}) == (
            "2026-01-01",
            "2026-01-31",
        )

    def test_rejects_non_dict(self):
        assert _parse_period("2026-01-01") is None
        assert _parse_period(None) is None

    def test_rejects_wrong_format(self):
        assert _parse_period({"start": "Jan 2026", "end": "Feb 2026"}) is None


# ---------------------------------------------------------------------------
# _sanitize_facet
# ---------------------------------------------------------------------------
class TestSanitizeFacet:
    def test_service_call_with_args(self):
        raw = {
            "facet_id": "x",
            "kind": "service_call",
            "service": "ErpKpiService.sales_summary_for_period",
            "args": {"days": 30},
            "purpose": "primary",
        }
        out = _sanitize_facet(raw)
        assert out is not None
        assert out["purpose"] == "primary"

    def test_ad_hoc_query_with_nl(self):
        raw = {
            "facet_id": "y",
            "kind": "ad_hoc_query",
            "natural_language": "COGS by region",
            "suggested_tables": ["erp_t_sal_outstockentry"],
        }
        out = _sanitize_facet(raw)
        assert out is not None
        assert out["suggested_tables"] == ["erp_t_sal_outstockentry"]

    def test_rejects_unknown_kind(self):
        assert _sanitize_facet({"facet_id": "z", "kind": "wtf"}) is None

    def test_rejects_missing_id(self):
        assert _sanitize_facet({"kind": "service_call", "service": "X"}) is None

    def test_default_purpose_is_auxiliary(self):
        raw = {
            "facet_id": "a",
            "kind": "ad_hoc_query",
            "natural_language": "q",
        }
        out = _sanitize_facet(raw)
        assert out["purpose"] == "auxiliary"

    def test_clamps_suggested_tables(self):
        raw = {
            "facet_id": "a",
            "kind": "ad_hoc_query",
            "natural_language": "q",
            "suggested_tables": [f"t{i}" for i in range(50)],
        }
        out = _sanitize_facet(raw)
        assert len(out["suggested_tables"]) <= 10


# ---------------------------------------------------------------------------
# _normalize_intent — domain validation, facet clamping, primary guarantee
# ---------------------------------------------------------------------------
class TestNormalizeIntent:
    def test_unknown_domain_falls_back_to_generic(self):
        intent = _make_intent(domain="hack_me")
        intent["facets"] = [{"facet_id": "x", "kind": "ad_hoc_query", "natural_language": "q"}]
        out = _normalize_intent(intent)
        assert out["domain"] == "generic"

    def test_clamps_to_max_facets(self):
        facets = [
            {"facet_id": f"x{i}", "kind": "ad_hoc_query", "natural_language": "q"}
            for i in range(MAX_FACETS + 5)
        ]
        intent = _make_intent(domain="supply_chain", facets=facets)
        out = _normalize_intent(intent)
        assert len(out["facets"]) == MAX_FACETS

    def test_no_primary_assigned(self):
        # If LLM didn't mark any as primary, first one becomes primary.
        facets = [
            {"facet_id": "a", "kind": "ad_hoc_query", "natural_language": "q", "purpose": "auxiliary"},
            {"facet_id": "b", "kind": "ad_hoc_query", "natural_language": "q", "purpose": "auxiliary"},
        ]
        intent = _make_intent(domain="supply_chain", facets=facets)
        out = _normalize_intent(intent)
        assert any(f["purpose"] == "primary" for f in out["facets"])

    def test_empty_facets_returns_none(self):
        intent = _make_intent(domain="supply_chain", facets=[])
        assert _normalize_intent(intent) is None

    def test_all_invalid_facets_returns_none(self):
        facets = [
            {"kind": "nope"},
            {"facet_id": "", "kind": "ad_hoc_query"},
        ]
        intent = _make_intent(facets=facets)
        assert _normalize_intent(intent) is None

    def test_normalizes_period_tuple(self):
        intent = _make_intent()
        intent["facets"] = [{"facet_id": "x", "kind": "ad_hoc_query", "natural_language": "q"}]
        out = _normalize_intent(intent)
        assert out["period"] == ("2026-07-25", "2026-08-23")


# ---------------------------------------------------------------------------
# profile_enterprise_intent — fail-open contract
# ---------------------------------------------------------------------------
class TestProfileEnterpriseIntentFailOpen:
    def test_empty_message_returns_none(self):
        assert profile_enterprise_intent("") is None
        assert profile_enterprise_intent("   ") is None

    def test_llm_raises_returns_none(self):
        def bad(_):
            raise RuntimeError("kaboom")
        assert profile_enterprise_intent("q", llm_caller=bad) is None

    def test_llm_returns_empty_returns_none(self):
        assert profile_enterprise_intent("q", llm_caller=_llm({})) is None
        assert profile_enterprise_intent("q", llm_caller=_llm(None)) is None  # type: ignore

    def test_malformed_json_falls_open(self):
        # Embedded text that can't be repaired: no braces at all.
        assert profile_enterprise_intent("q", llm_caller=_llm({"response": "no json"})) is None

    def test_successful_faceting_intent(self):
        intent = _make_intent(
            domain="financial_performance",
            facets=[
                {"facet_id": "sales", "kind": "service_call",
                 "service": "ErpKpiService.sales_summary_for_period",
                 "args": {"days": 30}, "purpose": "primary"},
            ],
        )
        out = profile_enterprise_intent("q", llm_caller=_llm(intent))
        assert out is not None
        assert out["domain"] == "financial_performance"
        assert len(out["facets"]) == 1

    def test_repairs_json_inside_response_field(self):
        raw_text = json.dumps(_make_intent(
            domain="supply_chain",
            facets=[{"facet_id": "x", "kind": "ad_hoc_query", "natural_language": "q"}],
        ))
        out = profile_enterprise_intent(
            "q", llm_caller=_llm({"response": raw_text})
        )
        assert out is not None
        assert out["domain"] == "supply_chain"


# ---------------------------------------------------------------------------
# Whitelist / domain table sanity
# ---------------------------------------------------------------------------
class TestWhitelistAndDomains:
    def test_whitelist_has_required_services(self):
        for required in {
            "ErpKpiService.sales_summary_for_period",
            "ErpKpiService.inventory_position",
            "ErpKpiService.top_customers",
            "ErpKpiService.top_orders",
            "PerceptionService.build_snapshot",
        }:
            assert required in SERVICE_WHITELIST

    def test_valid_domains_match_spec(self):
        expected = {
            "supply_chain", "financial_performance", "logistics",
            "risk_management", "sales_operations", "hr",
            "procurement", "generic",
        }
        assert expected.issubset(set(VALID_DOMAINS))
