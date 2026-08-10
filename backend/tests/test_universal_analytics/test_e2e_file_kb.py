"""
E2E Group 3: File-Based KB Handling.

File KBs (CSV, PDF, etc.) must NOT be treated as databases.
All universal analytics tools should return clear error when only file KBs are bound.
"""
from __future__ import annotations

from .helpers import make_ctx, call_handler


def test_get_bound_kbs_excludes_file_kbs(db, kb_file, kb_db_a):
    """get_bound_kbs should only return DB KBs, exclude file KBs."""
    from app.services.universal_analytics.context import get_bound_kbs

    kbs = get_bound_kbs(make_ctx([kb_file.id, kb_db_a.id]), db)
    kb_ids = {str(k.id) for k in kbs}

    assert str(kb_db_a.id) in kb_ids, "DB KB should be included"
    assert str(kb_file.id) not in kb_ids, "File KB should be excluded"


def test_all_file_kbs_bound_returns_empty(db, kb_file, kb_file_b):
    """When only file KBs are bound, get_bound_kbs returns empty list."""
    from app.services.universal_analytics.context import get_bound_kbs

    kbs = get_bound_kbs(make_ctx([kb_file.id, kb_file_b.id]), db)
    assert len(kbs) == 0, f"Expected no DB KBs, got {len(kbs)}"


def test_query_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_query

    result = call_handler(_universal_query, {"sql": "SELECT 1"}, db,
                         context=make_ctx([kb_file.id]))
    assert result.get("success") is False
    assert "no database" in str(result.get("error", "")).lower()


def test_kpi_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_kpi

    result = call_handler(_universal_kpi,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([kb_file.id]))
    assert result.get("success") is False


def test_trend_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_trend

    result = call_handler(_universal_trend,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([kb_file.id]))
    assert result.get("success") is False


def test_forecast_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_forecast

    result = call_handler(_universal_forecast,
                         {"table": "x", "time_column": "d", "measure": "v"},
                         db, context=make_ctx([kb_file.id]))
    assert result.get("success") is False


def test_describe_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_describe

    result = call_handler(_universal_describe, {}, db,
                         context=make_ctx([kb_file.id]))
    assert result.get("success") is False


def test_discover_with_only_file_kbs_fails(db, kb_file):
    from app.services.universal_analytics.tools import _universal_discover

    result = call_handler(_universal_discover, {}, db,
                         context=make_ctx([kb_file.id]))
    assert result.get("success") is False


def test_auto_discover_should_discover_rejects_file_kb(db, kb_file):
    """_should_discover in auto_discover must return False for file KBs."""
    from app.services.universal_analytics.auto_discover import _should_discover

    assert not _should_discover(kb_file), \
        f"File KB source_kind={kb_file.source_kind} must not be discoverable"
