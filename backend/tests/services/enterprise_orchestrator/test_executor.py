"""Tests for the executor (multi-facet parallel fan-out).

Design spec reference: §8 Facet Executor — Hybrid Execution.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "app"))

import pytest

from app.services.enterprise_orchestrator.executor import (
    execute_facets,
    _check_degeneracy,
    _is_pivoted_aggregate_row,
    _normalize_service_result,
    _normalize_ad_hoc_result,
    _extract_service_payload,
    PER_FACET_TIMEOUT_S,
)
from app.services.enterprise_orchestrator.profiler import FacetSpec


def _svc_facet(
    facet_id="sales_summary",
    service="ErpKpiService.sales_summary_for_period",
    args=None,
    purpose="primary",
):
    return FacetSpec(
        facet_id=facet_id,
        kind="service_call",
        service=service,
        args=args or {"days": 30},
        natural_language="",
        suggested_tables=[],
        purpose=purpose,
    )


def _ad_facet(
    facet_id="adhoc",
    nl="How many customers in Southern region?",
    tables=None,
    purpose="auxiliary",
):
    return FacetSpec(
        facet_id=facet_id,
        kind="ad_hoc_query",
        service="",
        args={},
        natural_language=nl,
        suggested_tables=tables or ["erp_t_crm_contractentry"],
        purpose=purpose,
    )


def _intent(facets):
    return {
        "domain": "financial_performance",
        "period": ("2026-07-25", "2026-08-23"),
        "primary_metric": "gross_margin_pct",
        "segments": [],
        "facets": facets,
    }


# ---------------------------------------------------------------------------
# Degenerate-row detection (the exact bug pattern)
# ---------------------------------------------------------------------------
class TestDegeneracyGate:
    def test_one_row_total_max_min_equal(self):
        # The exact bug: 1 row with Total/Max/Min all = 51601.685.
        rows = [{"Total": 51601.685, "Max volume": 51601.685,
                 "Min volume": 51601.685, "Avg volume": 51601.685}]
        avail, why = _check_degeneracy(rows)
        assert avail is False
        assert "degenerate" in why

    def test_one_row_real_data_passes(self):
        rows = [{"product": "isoprene", "volume": 123.45, "revenue": 6789.0}]
        avail, why = _check_degeneracy(rows)
        assert avail is True
        assert why == ""

    def test_multi_row_real_data_passes(self):
        rows = [
            {"product": "isoprene", "volume": 123.45},
            {"product": "piperylene", "volume": 67.89},
        ]
        avail, why = _check_degeneracy(rows)
        assert avail is True

    def test_empty_rows_unavailable(self):
        avail, why = _check_degeneracy([])
        assert avail is False
        assert "no rows" in why

    def test_two_rows_same_value_aggregate_columns(self):
        rows = [
            {"Total": 100.0, "Max": 100.0, "Min": 100.0},
            {"Total": 100.0, "Max": 100.0, "Min": 100.0},
        ]
        avail, why = _check_degeneracy(rows)
        assert avail is False

    def test_pivoted_aggregate_row_helper(self):
        assert _is_pivoted_aggregate_row(
            {"Total": 51601.685, "Max volume": 51601.685, "Min volume": 51601.685}
        )
        assert not _is_pivoted_aggregate_row(
            {"product": "isoprene", "volume": 123.45}
        )


# ---------------------------------------------------------------------------
# Payload normalization
# ---------------------------------------------------------------------------
class TestServicePayloadExtraction:
    def test_rows_shape(self):
        rows, label, sql, summary = _extract_service_payload({
            "rows": [{"a": 1}, {"b": 2}], "source": "erp_v", "summary": "ok",
        })
        assert rows == [{"a": 1}, {"b": 2}]
        assert label == "erp_v"
        assert summary == "ok"
        assert sql == ""

    def test_kpi_block_flattened(self):
        rows, *_ = _extract_service_payload({
            "kpi": {"sales_30d_tons": 123.45, "inventory_tons": 999.0},
            "source": "ErpKpiService",
        })
        assert rows and all(isinstance(r, dict) for r in rows)

    def test_none_input(self):
        assert _extract_service_payload(None) == ([], "", "", "")

    def test_unexpected_type(self):
        rows, label, *_ = _extract_service_payload(12345)
        assert rows == []
        assert "unexpected" in label or "unexpected" in _extract_service_payload(12345)[3]


class TestServiceResultNormalization:
    def test_marks_degenerate_as_unavailable(self):
        facet = _svc_facet()
        raw = {"rows": [{"Total": 51601.685, "Max": 51601.685}], "source": "erp_v"}
        out = _normalize_service_result(facet, raw)
        assert out["available"] is False
        assert out["unavailable_reason"]
        assert out["rows"] == []  # cleared

    def test_normal_rows_available(self):
        facet = _svc_facet()
        raw = {"rows": [{"product": "isoprene", "volume": 1234.56}], "source": "erp_v"}
        out = _normalize_service_result(facet, raw)
        assert out["available"] is True
        assert out["row_count"] == 1


class TestAdHocResultNormalization:
    def test_normalizes_ask_data_agent_shape(self):
        facet = _ad_facet()
        raw = {
            "success": True,
            "rows": [{"customer": "A", "ytd": 1.5}],
            "sql": "SELECT * FROM x",
            "source_name": "erp_v_sale_orderentry",
            "answer": "found 1 row",
        }
        out = _normalize_ad_hoc_result(facet, raw)
        assert out["available"] is True
        assert out["source_sql"] == "SELECT * FROM x"
        assert out["source_label"] == "erp_v_sale_orderentry"

    def test_degenerate_rows_become_unavailable_and_cleared(self):
        facet = _ad_facet()
        raw = {
            "success": True,
            "rows": [{"Total": 51601.685, "Max": 51601.685}],
            "sql": "SELECT SUM(...)",
        }
        out = _normalize_ad_hoc_result(facet, raw)
        assert out["available"] is False
        assert out["rows"] == []

    def test_unexpected_input_type_becomes_warning(self):
        facet = _ad_facet()
        out = _normalize_ad_hoc_result(facet, 12345)
        assert out["available"] is False
        assert any("unexpected" in w for w in out["warnings"])

    def test_success_false_is_warning_but_not_failure(self):
        facet = _ad_facet()
        raw = {"success": False, "rows": [{"a": 1}]}
        out = _normalize_ad_hoc_result(facet, raw)
        assert "ad-hoc tool returned success=False" in out["warnings"]


# ---------------------------------------------------------------------------
# End-to-end execute_facets — partial failure, isolation, parallelism
# ---------------------------------------------------------------------------
class TestParallelExecution:
    def test_parallel_partial_failure_isolation(self):
        # Mix: 1 OK service facet, 1 non-whitelisted service facet,
        # 1 OK ad-hoc facet, 1 ad-hoc with empty nl.
        facets = [
            _svc_facet(),
            _svc_facet(facet_id="bad", service="SomeService.evil_method"),
            _ad_facet(facet_id="adhoc1"),
            _ad_facet(facet_id="adhoc2", nl=""),
        ]
        svc_results = {
            "ErpKpiService.sales_summary_for_period": {
                "rows": [{"product": "isoprene", "volume": 100.0}],
                "source": "erp_v",
            }
        }
        ad_results = {
            "How many customers in Southern region?": {
                "success": True,
                "rows": [{"n": 42}],
                "sql": "SELECT COUNT(*)",
                "source_name": "erp_t_crm",
            }
        }
        async def svc_inv(service, args):
            return svc_results.get(service) or {"rows": [], "source": "x"}

        async def ad_inv(args):
            return ad_results.get(args["question"]) or {
                "success": True, "rows": [], "sql": ""
            }

        intent = _intent(facets)
        results = asyncio.run(execute_facets(
            intent, service_invoker=svc_inv, ad_hoc_invoker=ad_inv,
        ))
        assert set(results.keys()) == {"sales_summary", "bad", "adhoc1", "adhoc2"}
        assert results["sales_summary"]["available"] is True
        assert results["adhoc1"]["available"] is True
        assert results["bad"]["available"] is False
        assert "whitelist" in results["bad"]["unavailable_reason"]
        assert results["adhoc2"]["available"] is False
        assert "natural_language" in results["adhoc2"]["unavailable_reason"]

    def test_service_invoker_exception_isolated(self):
        facets = [_svc_facet(), _svc_facet(facet_id="bad2")]
        async def svc(service, args):
            if service == "ErpKpiService.sales_summary_for_period":
                raise RuntimeError("db down")
            return {"rows": [{"a": 1}], "source": "x"}
        async def ad_inv(args):
            return {"success": True, "rows": [{"a": 1}], "sql": "SELECT 1"}
        intent = _intent(facets)
        results = asyncio.run(execute_facets(
            intent, service_invoker=svc, ad_hoc_invoker=ad_inv,
        ))
        assert all(r["available"] is False for r in results.values())

    def test_execution_log_populated(self):
        facets = [_svc_facet()]
        async def svc(service, args):
            return {"rows": [{"a": 1}], "source": "x"}
        async def ad_inv(args):
            return {"success": True, "rows": [{"a": 1}]}
        intent = _intent(facets)
        results = asyncio.run(execute_facets(
            intent, service_invoker=svc, ad_hoc_invoker=ad_inv,
        ))
        assert any(
            log["step"] == "service_invoke"
            for log in results["sales_summary"]["execution_log"]
        )

    def test_ad_hoc_invoker_exception_isolated(self):
        facets = [_svc_facet(), _ad_facet()]
        async def svc(service, args):
            return {"rows": [{"a": 1}], "source": "x"}
        async def ad_inv(args):
            raise RuntimeError("nl2sql timeout")
        intent = _intent(facets)
        results = asyncio.run(execute_facets(
            intent, service_invoker=svc, ad_hoc_invoker=ad_inv,
        ))
        assert results["sales_summary"]["available"] is True
        assert results["adhoc"]["available"] is False
        assert "RuntimeError" in results["adhoc"]["unavailable_reason"]

    def test_empty_facets_returns_empty_dict(self):
        intent = _intent([])
        results = asyncio.run(execute_facets(intent))
        assert results == {}

    def test_per_facet_timeout_constant(self):
        # Spec: 60 seconds.
        assert PER_FACET_TIMEOUT_S == 60.0
