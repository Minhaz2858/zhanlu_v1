"""Tests for _normalize_tool_result_to_rows — EDIA structured-data normalizer.

Validates that ask_erp_kpi, ask_erp_writeback, ask_perception_intelligence,
and ask_forecast_pricing results are correctly flattened into row-lists
that the deferred-deliverable pipeline can consume.
"""

import pytest
import sys
import os

# ── Path setup for in-container pytest ──────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "app"))
sys.path.insert(0, "/app")
sys.path.insert(0, "/app/venv/lib/python3.11/site-packages")

from app.routers.agents import (
    _normalize_tool_result_to_rows,
    DATA_PRODUCING_TOOLS,
)


# ═══════════════════════════════════════════════════════════════════
# 1. ask_erp_kpi
# ═══════════════════════════════════════════════════════════════════

class TestNormalizeErpKpi:
    """_normalize_tool_result_to_rows("ask_erp_kpi", ...)"""

    def test_full_kpi_returns_one_row(self):
        result = {
            "success": True,
            "kpi": {
                "product_id": "isoprene",
                "material_names": ["异戊二烯", "isoprene"],
                "period_days": 30,
                "source": "mysql://erp",
                "is_live": True,
                "kpi": {
                    "inventory_tons": 1234.5,
                    "inventory_days": 36.0,
                    "sales_30d_tons": 5613.0,
                    "utilization_pct": 85.5,
                    "avg_price": 9500.0,
                    "revenue_30d": 53223500.0,
                    "cost_30d": 42578800.0,
                    "margin_pct": 20.0,
                },
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert len(rows) == 1
        row = rows[0]
        assert row["product_id"] == "isoprene"
        assert row["material_names"] == ["异戊二烯", "isoprene"]
        assert row["inventory_tons"] == 1234.5
        assert row["inventory_days"] == 36.0
        assert row["sales_30d_tons"] == 5613.0
        assert row["utilization_pct"] == 85.5
        assert row["avg_price"] == 9500.0
        assert row["revenue_30d"] == 53223500.0
        assert row["cost_30d"] == 42578800.0
        assert row["margin_pct"] == 20.0
        assert row["period_days"] == 30
        assert row["source"] == "mysql://erp"
        assert row["is_live"] is True

    def test_kpi_with_material_id_fallback(self):
        """When product_id is missing, fall back to material_id."""
        result = {
            "success": True,
            "kpi": {
                "material_id": "dcpd",
                "kpi": {"inventory_tons": 500.0},
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert len(rows) == 1
        assert rows[0]["product_id"] == "dcpd"

    def test_kpi_with_no_product_id_uses_unknown(self):
        """When both product_id and material_id are missing, use 'unknown'."""
        result = {
            "success": True,
            "kpi": {
                "kpi": {"inventory_tons": 500.0},
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert len(rows) == 1
        assert rows[0]["product_id"] == "unknown"

    def test_empty_kpi_returns_empty(self):
        result = {"success": True, "kpi": {}}
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert rows == []

    def test_missing_kpi_key_returns_empty(self):
        result = {"success": True}
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert rows == []

    def test_success_false_returns_empty(self):
        result = {"success": False, "kpi": {"product_id": "x", "kpi": {"inventory_tons": 1}}}
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert rows == []

    def test_partial_inner_kpi(self):
        """Inner kpi dict may have only some fields — rest should be None."""
        result = {
            "success": True,
            "kpi": {
                "product_id": "c5_resin",
                "kpi": {"inventory_tons": 2754.0},
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert len(rows) == 1
        assert rows[0]["inventory_tons"] == 2754.0
        assert rows[0]["sales_30d_tons"] is None
        assert rows[0]["revenue_30d"] is None
        assert rows[0]["margin_pct"] is None

    def test_non_dict_result_returns_empty(self):
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", "not a dict")
        assert rows == []

    def test_kpi_preserves_material_names_list(self):
        result = {
            "success": True,
            "kpi": {
                "product_id": "dcpd",
                "material_names": ["双环戊二烯", "DCPD", "dicyclopentadiene"],
                "kpi": {"inventory_tons": 100},
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        assert rows[0]["material_names"] == ["双环戊二烯", "DCPD", "dicyclopentadiene"]


# ═══════════════════════════════════════════════════════════════════
# 2. ask_erp_writeback
# ═══════════════════════════════════════════════════════════════════

class TestNormalizeErpWriteback:
    """_normalize_tool_result_to_rows("ask_erp_writeback", ...)"""

    def test_full_writeback_returns_one_row(self):
        result = {
            "success": True,
            "record": {
                "record_id": "rec-123",
                "product_id": "isoprene",
                "action": "price_update",
                "old_value": 9500.0,
                "new_value": 9800.0,
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "erp_writeback"
        assert row["record_id"] == "rec-123"
        assert row["product_id"] == "isoprene"
        assert row["old_value"] == 9500.0
        assert row["new_value"] == 9800.0

    def test_empty_record_returns_empty(self):
        result = {"success": True, "record": {}}
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        assert rows == []

    def test_missing_record_returns_empty(self):
        result = {"success": True}
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        assert rows == []

    def test_success_false_returns_empty(self):
        result = {"success": False, "record": {"record_id": "x"}}
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        assert rows == []

    def test_record_keys_spread_into_row(self):
        """All keys from 'record' dict should appear in the row."""
        result = {
            "success": True,
            "record": {"custom_field": "hello", "another": 42},
        }
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        assert len(rows) == 1
        assert rows[0]["custom_field"] == "hello"
        assert rows[0]["another"] == 42
        assert rows[0]["action"] == "erp_writeback"


# ═══════════════════════════════════════════════════════════════════
# 3. ask_perception_intelligence
# ═══════════════════════════════════════════════════════════════════

class TestNormalizePerceptionIntelligence:
    """_normalize_tool_result_to_rows("ask_perception_intelligence", ...)"""

    def test_both_keys_returns_two_rows(self):
        result = {
            "success": True,
            "perception": {"event": "price_drop", "severity": "high"},
            "intelligence": {"insight": "oversupply", "confidence": 0.85},
        }
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert len(rows) == 2
        types = [r["data_type"] for r in rows]
        assert "perception" in types
        assert "intelligence" in types

    def test_perception_only_returns_one_row(self):
        result = {
            "success": True,
            "perception": {"event": "price_drop"},
        }
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "perception"
        assert rows[0]["event"] == "price_drop"

    def test_intelligence_only_returns_one_row(self):
        result = {
            "success": True,
            "intelligence": {"insight": "oversupply"},
        }
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "intelligence"

    def test_empty_dicts_returns_empty(self):
        result = {"success": True, "perception": {}, "intelligence": {}}
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        # Empty dicts are skipped — no meaningful data to extract
        assert rows == []

    def test_missing_keys_returns_empty(self):
        result = {"success": True}
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert rows == []

    def test_non_dict_blob_skipped(self):
        result = {
            "success": True,
            "perception": "not a dict",
            "intelligence": {"insight": "valid"},
        }
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "intelligence"

    def test_success_false_returns_empty(self):
        result = {"success": False, "perception": {"event": "x"}, "intelligence": {"y": 1}}
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert rows == []


# ═══════════════════════════════════════════════════════════════════
# 4. ask_forecast_pricing
# ═══════════════════════════════════════════════════════════════════

class TestNormalizeForecastPricing:
    """_normalize_tool_result_to_rows("ask_forecast_pricing", ...)"""

    def test_both_keys_returns_two_rows(self):
        result = {
            "success": True,
            "forecast": {"horizon_days": 7, "forecast_value": 120.5},
            "pricing": {"recommended_price": 9800.0, "confidence": "high"},
        }
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert len(rows) == 2
        types = [r["data_type"] for r in rows]
        assert "forecast" in types
        assert "pricing" in types

    def test_forecast_only(self):
        result = {
            "success": True,
            "forecast": {"horizon_days": 7, "forecast_value": 120.5},
        }
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "forecast"
        assert rows[0]["horizon_days"] == 7

    def test_pricing_only(self):
        result = {
            "success": True,
            "pricing": {"recommended_price": 9800.0},
        }
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "pricing"

    def test_missing_keys_returns_empty(self):
        result = {"success": True}
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert rows == []

    def test_non_dict_blob_skipped(self):
        result = {
            "success": True,
            "forecast": None,
            "pricing": {"recommended_price": 9800.0},
        }
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert len(rows) == 1
        assert rows[0]["data_type"] == "pricing"

    def test_success_false_returns_empty(self):
        result = {"success": False, "forecast": {"a": 1}, "pricing": {"b": 2}}
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert rows == []


# ═══════════════════════════════════════════════════════════════════
# 5. Unknown / edge cases
# ═══════════════════════════════════════════════════════════════════

class TestNormalizeUnknownTools:
    """Unknown tool names should return empty list."""

    def test_unknown_tool_returns_empty(self):
        result = {"success": True, "rows": [{"a": 1}]}
        rows = _normalize_tool_result_to_rows("unknown_tool", result)
        assert rows == []

    def test_ask_data_agent_not_normalized(self):
        """ask_data_agent already has rows — normalizer should not touch it."""
        result = {"success": True, "rows": [{"col1": "val1"}]}
        rows = _normalize_tool_result_to_rows("ask_data_agent", result)
        assert rows == []

    def test_none_result_returns_empty(self):
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", None)
        assert rows == []

    def test_list_result_returns_empty(self):
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", [1, 2, 3])
        assert rows == []


# ═══════════════════════════════════════════════════════════════════
# 6. DATA_PRODUCING_TOOLS membership
# ═══════════════════════════════════════════════════════════════════

class TestDataProducingToolsMembership:
    """Verify all EDIA structured-data tools are in DATA_PRODUCING_TOOLS."""

    EXPECTED_EDIA_TOOLS = {
        "ask_erp_kpi",
        "ask_erp_writeback",
        "ask_perception_intelligence",
        "ask_forecast_pricing",
    }

    EXPECTED_SQL_TOOLS = {
        "ask_data_agent",
        "execute_query",
        "execute_sql",
        "sql_query",
        "forecast_brief",
    }

    def test_edia_tools_in_set(self):
        for tool in self.EXPECTED_EDIA_TOOLS:
            assert tool in DATA_PRODUCING_TOOLS, f"{tool} missing from DATA_PRODUCING_TOOLS"

    def test_sql_tools_still_in_set(self):
        for tool in self.EXPECTED_SQL_TOOLS:
            assert tool in DATA_PRODUCING_TOOLS, f"{tool} missing from DATA_PRODUCING_TOOLS"

    def test_total_tool_count(self):
        expected = self.EXPECTED_EDIA_TOOLS | self.EXPECTED_SQL_TOOLS
        assert DATA_PRODUCING_TOOLS == expected


# ═══════════════════════════════════════════════════════════════════
# 7. Integration: normalization → rows shape usable by record_dataset
# ═══════════════════════════════════════════════════════════════════

class TestNormalizationOutputShape:
    """Verify normalized rows are plain dicts with JSON-serializable values."""

    def test_kpi_row_is_json_serializable(self):
        import json
        result = {
            "success": True,
            "kpi": {
                "product_id": "isoprene",
                "material_names": ["异戊二烯"],
                "kpi": {"inventory_tons": 1234.5},
            },
        }
        rows = _normalize_tool_result_to_rows("ask_erp_kpi", result)
        serialized = json.dumps(rows)
        assert serialized  # no exception

    def test_writeback_row_is_json_serializable(self):
        import json
        result = {
            "success": True,
            "record": {"record_id": "r1", "delta": 0.05},
        }
        rows = _normalize_tool_result_to_rows("ask_erp_writeback", result)
        serialized = json.dumps(rows)
        assert serialized

    def test_perception_row_keys_do_not_collide(self):
        """data_type key should not overwrite existing keys."""
        result = {
            "success": True,
            "perception": {"data_type": "should_be_overwritten", "event": "price_drop"},
        }
        rows = _normalize_tool_result_to_rows("ask_perception_intelligence", result)
        assert rows[0]["data_type"] == "perception"  # our key wins
        assert rows[0]["event"] == "price_drop"

    def test_forecast_row_keys_do_not_collide(self):
        result = {
            "success": True,
            "forecast": {"data_type": "should_be_overwritten", "horizon": 7},
        }
        rows = _normalize_tool_result_to_rows("ask_forecast_pricing", result)
        assert rows[0]["data_type"] == "forecast"
        assert rows[0]["horizon"] == 7
