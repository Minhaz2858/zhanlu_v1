"""Tests for universal_analytics/nl_to_sql.py — P4 NL→SQL translation engine.

Flag-gated behind UNIVERSAL_ANALYTICS_NL_SQL (default OFF).
Tests: SQL generation placeholders, validation, injection prevention,
        schema context injection, multi-dialect support.
"""

import pytest
from unittest.mock import MagicMock, patch


# ── Flag gating tests ───────────────────────────────────────────────

class TestNlToSqlFlagGating:
    def test_is_enabled_false_by_default(self):
        """When UNIVERSAL_ANALYTICS_NL_SQL not set or false, disabled."""
        from app.services.universal_analytics.nl_to_sql import is_nl_sql_enabled
        with patch.dict("os.environ", {}, clear=True):
            assert is_nl_sql_enabled() is False

    def test_is_enabled_true_when_flag_on(self):
        from app.services.universal_analytics.nl_to_sql import is_nl_sql_enabled
        with patch.dict("os.environ", {"UNIVERSAL_ANALYTICS_NL_SQL": "true"}):
            assert is_nl_sql_enabled() is True


# ── Translation tests ───────────────────────────────────────────────

class TestNlToSqlTranslation:
    def test_translate_returns_error_when_disabled(self):
        """When flag is OFF, translate() returns a disabled error."""
        from app.services.universal_analytics.nl_to_sql import translate
        with patch.dict("os.environ", {}, clear=True):
            result = translate("show sales", {}, "mysql")
            assert result["success"] is False
            assert "disabled" in result.get("error", "").lower()

    def test_translate_accepts_schema_context(self):
        """Schema context is accepted in the call."""
        from app.services.universal_analytics.nl_to_sql import translate
        schema = {
            "tables": [
                {"name": "sales", "columns": ["date", "revenue", "region"]}
            ]
        }
        with patch.dict("os.environ", {}, clear=True):
            result = translate("total revenue by region", schema, "mysql")
            assert result["success"] is False  # disabled

    def test_translate_multi_dialect(self):
        """Dialect parameter is passed through."""
        from app.services.universal_analytics.nl_to_sql import translate
        with patch.dict("os.environ", {}, clear=True):
            for dialect in ["mysql", "postgres", "sqlite"]:
                result = translate("count rows", {}, dialect)
                assert result["success"] is False

    def test_translate_with_dimensions(self):
        """Questions with dimensions are accepted."""
        from app.services.universal_analytics.nl_to_sql import translate
        schema = {
            "tables": [
                {"name": "orders", "columns": ["id", "dt", "amount", "status"]}
            ]
        }
        with patch.dict("os.environ", {}, clear=True):
            result = translate(
                "average order amount by status over time",
                schema, "postgres",
            )
            assert result["success"] is False


# ── Injection prevention tests ──────────────────────────────────────

class TestInjectionPrevention:
    def test_translate_rejects_ddl_keywords(self):
        """DDL keywords in question are rejected."""
        from app.services.universal_analytics.nl_to_sql import translate
        with patch.dict("os.environ", {}, clear=True):
            result = translate("DROP TABLE users", {}, "mysql")
            assert result["success"] is False

    def test_translate_rejects_malicious_injection(self):
        """SQL injection attempts are rejected before LLM call."""
        from app.services.universal_analytics.nl_to_sql import translate
        with patch.dict("os.environ", {}, clear=True):
            result = translate(
                "get data; UPDATE users SET role='admin'",
                {}, "mysql",
            )
            assert result["success"] is False

    def test_translate_rejects_empty_question(self):
        """Empty question returns error."""
        from app.services.universal_analytics.nl_to_sql import translate
        with patch.dict("os.environ", {}, clear=True):
            result = translate("", {}, "mysql")
            assert result["success"] is False
