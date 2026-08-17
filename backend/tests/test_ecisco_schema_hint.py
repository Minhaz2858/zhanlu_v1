"""Tests for the ecisco_bi_assistant F_PAEZ_JC schema deprecation hint.

Verifies:
- `get_system_prompt("ecisco_bi_assistant")` includes the deprecation rule
  steering the LLM to `F_PAEZ_JC` over the stale `material_model` column.
- Other agents do NOT receive the hint (scoping requirement).
- The `describe_schema` annotation only fires for the ecisco_bi_assistant.
"""

from __future__ import annotations

import pytest

from app.services.agent_prompts import get_system_prompt


class TestEciscoSchemaHint:
    """Verify the deprecation hint is scoped to ecisco_bi_assistant only."""

    def test_ecisco_bi_assistant_gets_f_paez_jc_hint(self):
        prompt = get_system_prompt("ecisco_bi_assistant")
        text = prompt.lower()
        assert "f_paez_jc" in text, (
            "ecisco_bi_assistant prompt must mention F_PAEZ_JC"
        )
        assert "material_model" in text, (
            "ecisco_bi_assistant prompt must flag material_model as deprecated"
        )

    def test_other_agents_do_not_get_hint(self):
        # A representative non-ecisco agent must NOT receive the hint.
        for agent in ["general_agent", "forecast_agent", "analysis_agent"]:
            prompt = get_system_prompt(agent)
            assert "F_PAEZ_JC" not in prompt, (
                f"{agent} must NOT receive the F_PAEZ_JC deprecation hint"
            )

    def test_hint_is_flag_gated(self, monkeypatch):
        from app.services.agent_prompts import settings

        monkeypatch.setattr(settings, "ECISCO_BI_SCHEMA_DEPRECATION_ENABLED", False)
        prompt = get_system_prompt("ecisco_bi_assistant")
        assert "F_PAEZ_JC" not in prompt, (
            "Deprecation hint must be suppressed when the flag is off"
        )


class TestDescribeSchemaAnnotation:
    """Verify the describe_schema post-processing annotates material_model."""

    def _annotate(self):
        from app.services.tool_handlers.db_tools import _annotate_deprecated_columns

        return _annotate_deprecated_columns

    def test_annotates_material_model_for_ecisco(self):
        annotate = self._annotate()
        result = {
            "table": "erp_product_sales_details",
            "columns": [
                {"name": "FNAME", "type": "varchar", "pk": False},
                {"name": "material_model", "type": "varchar", "pk": False},
                {"name": "F_PAEZ_JC", "type": "varchar", "pk": False},
            ],
        }
        out = annotate(result, "ecisco_bi_assistant")
        cols = {c["name"]: c for c in out["columns"]}
        assert cols["material_model"].get("deprecated") is True
        assert "F_PAEZ_JC" in cols["material_model"].get("note", "")
        assert "deprecated" not in cols["F_PAEZ_JC"]
        assert "deprecated" not in cols["FNAME"]

    def test_does_not_annotate_for_other_agents(self):
        annotate = self._annotate()
        result = {
            "table": "t",
            "columns": [{"name": "material_model", "type": "varchar"}],
        }
        out = annotate(result, "general_agent")
        assert "deprecated" not in out["columns"][0]

    def test_annotates_describe_all_shape(self):
        annotate = self._annotate()
        result = {
            "tables": [
                {
                    "table": "a",
                    "columns": [{"name": "material_model", "type": "varchar"}],
                }
            ]
        }
        out = annotate(result, "ecisco_bi_assistant")
        assert out["tables"][0]["columns"][0].get("deprecated") is True
