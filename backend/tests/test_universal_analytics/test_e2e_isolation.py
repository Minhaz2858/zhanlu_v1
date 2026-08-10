"""
E2E Group 2: Cross-Project Data Isolation.

Verifies tools scoped to Project A only see Project A's KBs,
and Project B only sees Project B's KBs — no data leakage.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch

from .helpers import make_ctx, call_handler


def test_project_a_context_only_sees_its_own_kb(db, kb_db_a, kb_db_b):
    """context.get_bound_kbs with Project A context returns only kb_db_a."""
    from app.services.universal_analytics.context import get_bound_kbs

    kbs = get_bound_kbs(make_ctx([kb_db_a.id]), db)
    kb_ids = {str(k.id) for k in kbs}
    assert str(kb_db_a.id) in kb_ids, "kb_db_a should be in Project A context"
    assert str(kb_db_b.id) not in kb_ids, "kb_db_b should NOT leak into Project A context"


def test_query_kb_b_with_project_a_context_rejected(db, kb_db_a, kb_db_b):
    """Querying kb_b when only kb_a is bound should be rejected."""
    from app.services.universal_analytics.tools import _universal_query

    result = call_handler(_universal_query,
                         {"sql": "SELECT 1", "kb_id": kb_db_b.id}, db,
                         context=make_ctx([kb_db_a.id]))
    assert result.get("success") is False, f"Should reject unbound KB, got: {result}"


def test_dual_bound_query_respects_explicit_kb_id(db, kb_db_a, kb_db_b):
    """With both KBs bound, query with explicit kb_id should work."""
    from app.services.universal_analytics.tools import _universal_query

    mock_result = {"rows": [{"id": 1, "item": "Gadget X"}]}
    with patch("app.services.db.query_service.QueryService.execute",
               return_value=mock_result):
        result = call_handler(_universal_query,
                             {"sql": "SELECT * FROM orders", "kb_id": kb_db_b.id}, db,
                             context=make_ctx([kb_db_a.id, kb_db_b.id]))

    assert result.get("success") is True, f"Expected success with dual-bound: {result}"
    assert len(result.get("rows", [])) == 1


def test_kpi_isolation_different_kbs(db, kb_db_a, kb_db_b):
    """KPI on kb_a vs kb_b should use different connections (mock sanity)."""
    from app.services.universal_analytics.tools import _universal_kpi

    mock_a = {"rows": [{"kpi": "yoy", "current": 100, "previous": 90, "change_pct": 11.1}]}
    mock_b = {"rows": [{"kpi": "yoy", "current": 500, "previous": 480, "change_pct": 4.2}]}

    with patch("app.services.db.query_service.QueryService.execute",
               side_effect=[mock_a, mock_b]):
        r_a = call_handler(_universal_kpi,
                          {"table": "sales", "time_column": "d", "measure": "v"},
                          db, context=make_ctx([kb_db_a.id]))
        r_b = call_handler(_universal_kpi,
                          {"table": "orders", "time_column": "d", "measure": "v"},
                          db, context=make_ctx([kb_db_b.id]))

    assert r_a.get("success") and r_b.get("success")
    a_val = r_a["rows"][0]["current"]
    b_val = r_b["rows"][0]["current"]
    assert a_val != b_val, f"Data isolation broken: both KBs returned {a_val}"


def test_describe_isolation(db, kb_db_a, kb_db_b):
    """Describe on kb_a should use its own schema."""
    from app.services.universal_analytics.tools import _universal_describe

    schema_a = {"sales": {"id": "int"}}
    schema_b = {"orders": {"item": "varchar"}}

    with patch("app.services.db.schema_service.SchemaService.describe_all",
               side_effect=[schema_a, schema_b]):
        r_a = call_handler(_universal_describe, {}, db, context=make_ctx([kb_db_a.id]))
        r_b = call_handler(_universal_describe, {}, db, context=make_ctx([kb_db_b.id]))

    assert r_a.get("success") and r_b.get("success")
    assert "sales" in r_a.get("tables", {})
    assert "orders" in r_b.get("tables", {})
    assert "orders" not in r_a.get("tables", {})
    assert "sales" not in r_b.get("tables", {})
