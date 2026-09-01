"""End-to-end integration tests for the data-agent performance stack (A+B+C)."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.db.query_composer import compose_queries
from app.services.query_result_cache import put_result, get_cached_result, invalidate


_CONCEPT_CATALOG = (
    "- **Sales (销售)**: erp_t_sal_outstockentry + erp_t_sal_outstock\n"
    "  Measures: Volume→FREALQTY, Revenue→FYKSBGHAMOUNT, Margin→FYKSBGHALLAMOUNT\n"
    "  Columns: FREALQTY, FYKSBGHAMOUNT, FYKSBGHALLAMOUNT\n"
    "  Date column: FDATE"
)


def test_composer_produces_fetch_data_batch_compatible_sql():
    """Composed SQL should work as a fetch_data_batch query."""
    queries = compose_queries(
        metrics=["volume", "revenue", "margin"],
        kb_id="kb1",
        filters={"period": "July 2026"},
        concept_catalog=_CONCEPT_CATALOG,
    )
    assert len(queries) == 1
    q = queries[0]
    # Must be valid SELECT
    assert q.sql.strip().upper().startswith("SELECT")
    # Must have all 3 metrics
    assert "FREALQTY" in q.sql
    assert "FYKSBGHAMOUNT" in q.sql
    assert "FYKSBGHALLAMOUNT" in q.sql
    # Must have date filter
    assert "2026-07" in q.sql or "FDATE" in q.sql


def test_cache_then_composer_prevents_requery():
    """After first query, cache should prevent re-execution."""
    invalidate()
    result = {"success": True, "rows": [{"volume": 5000}], "answer": "5K"}
    put_result("July 2026 sales volume", "kb1", result)
    cached = get_cached_result("July 2026 sales volume", "kb1")
    assert cached is not None
    assert cached["rows"][0]["volume"] == 5000


def test_composer_falls_back_when_no_catalog():
    """Without a concept catalog, composer returns [] → caller uses ask_data_agent."""
    queries = compose_queries(
        metrics=["volume", "revenue"],
        kb_id="kb1",
        filters={"period": "July 2026"},
        concept_catalog="",
    )
    assert queries == []


def test_metric_extraction_from_question():
    """_extract_metrics_from_question should find metrics in natural language."""
    from app.services.tool_handlers.delegation_tools import _extract_metrics_from_question
    assert "volume" in _extract_metrics_from_question("July 2026 sales volume and revenue")
    assert "revenue" in _extract_metrics_from_question("July 2026 sales volume and revenue")
    assert "inventory" in _extract_metrics_from_question("show me inventory levels")


def test_period_extraction_from_question():
    """_extract_period_filter should extract dates from natural language."""
    from app.services.tool_handlers.delegation_tools import _extract_period_filter
    assert _extract_period_filter("July 2026 sales")["period"] == "July 2026"
    assert _extract_period_filter("2026-07 sales")["period"] == "2026-07"
    assert _extract_period_filter("2026年7月销售")["period"] == "2026-07"
