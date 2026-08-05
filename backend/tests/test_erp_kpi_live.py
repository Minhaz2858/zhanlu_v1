"""Tests for ErpKpiService live read from a project's KnowledgeBase.

TDD target (bug: Ecisco BI assistant said "ERP data not yet connected"
even when a MySQL KB was bound to the project).

These tests:
1. Stub fallback when no KB / unreachable DB
2. Live read path against a SQLite-mocked KB that mirrors the live
   `aipdp_data_warehouse_prod` schema (`erp_v_sale_orderentry` +
   `erp_v_stk_inventory`)
3. Product → material_name mapping
4. Defensive handling of `produce_qty` / `plan_qty` (varbinary(0) in live
   MySQL — utilization must return None, not 0.0)

The test fixture seeds the SQLite with the *same* column names used in
EDIA_5.1.2's erp_kpi_service.py so the SQL stays portable.
"""
from __future__ import annotations

import sqlite3
import tempfile
import os
from datetime import date
from types import SimpleNamespace

import pytest


def _build_kb(path: str) -> SimpleNamespace:
    """Construct a minimal KnowledgeBase-shaped object pointing at SQLite."""
    return SimpleNamespace(
        id="kb-test",
        db_type="sqlite",
        host=None,
        port=None,
        database_name=None,
        username=None,
        password=None,
        api_url=path,  # SQLite connector uses api_url as the file path
        file_url=None,
    )


@pytest.fixture
def seeded_sqlite_kb():
    """Seed an in-temp-file SQLite with one row each in the live ERP tables."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        conn = sqlite3.connect(path)
        # Mirror the live `aipdp_data_warehouse_prod` schema (subset).
        # NOTE: produce_qty / plan_qty exist as varbinary(0) in the live
        # MySQL; we keep them NULL here to mimic that.
        conn.executescript(
            """
            CREATE TABLE erp_v_sale_orderentry (
                material_name TEXT,
                FDELIQTY REAL,
                PLANDATE TEXT,
                produce_qty REAL,
                plan_qty REAL
            );
            CREATE TABLE erp_v_stk_inventory (
                material_name TEXT,
                FBASEQTY REAL
            );
            """
        )
        # Latest date: 2026-07-30 (matches live DB)
        # Sales rows: isoprene 998.89 tons (30d), cracked_c5 4600.57 tons (30d)
        sales = [
            # isoprene — 30d window
            ("异戊二烯", 200.0, "2026-07-29 00:00:00", None, None),
            ("异戊二烯", 300.0, "2026-07-20 00:00:00", None, None),
            ("异戊二烯", 498.89, "2026-07-05 00:00:00", None, None),
            # isoprene — outside 30d window (should be excluded)
            ("异戊二烯", 100.0, "2026-05-01 00:00:00", None, None),
            # cracked_c5
            ("工业用裂解碳五", 1600.57, "2026-07-25 00:00:00", None, None),
            ("工业用裂解碳五", 3000.0, "2026-07-10 00:00:00", None, None),
            # wrong product
            ("苯乙烯", 999.0, "2026-07-25 00:00:00", None, None),
        ]
        conn.executemany(
            "INSERT INTO erp_v_sale_orderentry VALUES (?, ?, ?, ?, ?)", sales
        )
        # Inventory: isoprene 800, cracked_c5 498.87
        inv = [
            ("异戊二烯", 800.0),
            ("工业用裂解碳五", 498.87),
        ]
        conn.executemany("INSERT INTO erp_v_stk_inventory VALUES (?, ?)", inv)
        conn.commit()
        conn.close()
        yield _build_kb(path)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Test 1: stub fallback when kb is None
# ---------------------------------------------------------------------------
def test_get_product_kpis_stub_when_no_kb():
    """No KB bound → structured stub with stub=True, kpi all None.

    This preserves backward compatibility: callers that don't pass a KB
    (e.g. the LLM in a project without a MySQL Resource) get the
    "no live data" signal instead of an exception.
    """
    from app.services.erp.kpi import ErpKpiService

    svc = ErpKpiService(db=None, org_id="test-org")
    result = svc.get_product_kpis(kb=None, product_id="isoprene")

    assert result["stub"] is True
    assert result["is_live"] is False
    assert result["product_id"] == "isoprene"
    assert result["kpi"]["inventory_tons"] is None
    assert result["kpi"]["sales_30d_tons"] is None
    assert result["kpi"]["utilization_pct"] is None
    assert "not yet connected" in result["summary"].lower() or "no kb" in result["summary"].lower()


# ---------------------------------------------------------------------------
# Test 2: live read against seeded SQLite (isoprene)
# ---------------------------------------------------------------------------
def test_get_product_kpis_live_isoprene(seeded_sqlite_kb):
    """Live read: isoprene returns inventory=800t, sales=998.89t, util=None."""
    from app.services.erp.kpi import ErpKpiService

    svc = ErpKpiService(db=None, org_id="test-org")
    result = svc.get_product_kpis(
        kb=seeded_sqlite_kb, product_id="isoprene", days=30
    )

    assert result["stub"] is False
    assert result["is_live"] is True
    assert result["product_id"] == "isoprene"
    assert result["kpi"]["inventory_tons"] == 800.0
    # 998.89 (in window) — 100 (out of window, must be excluded)
    assert result["kpi"]["sales_30d_tons"] == 998.89
    # inventory_days = inventory_tons / (sales_30d_tons / 30)
    # = 800 / (998.89 / 30) = 800 / 33.29633 = 24.0294
    assert result["kpi"]["inventory_days"] == pytest.approx(24.03, abs=0.01)
    # produce_qty/plan_qty are NULL in the live DB → utilization should be None
    assert result["kpi"]["utilization_pct"] is None
    assert "isoprene" in result["summary"].lower() or "异戊二烯" in result["summary"]


# ---------------------------------------------------------------------------
# Test 3: live read for cracked_c5
# ---------------------------------------------------------------------------
def test_get_product_kpis_live_cracked_c5(seeded_sqlite_kb):
    """Live read: cracked_c5 returns inventory=498.87t, sales=4600.57t."""
    from app.services.erp.kpi import ErpKpiService

    svc = ErpKpiService(db=None, org_id="test-org")
    result = svc.get_product_kpis(
        kb=seeded_sqlite_kb, product_id="cracked_c5", days=30
    )

    assert result["stub"] is False
    assert result["is_live"] is True
    assert result["kpi"]["inventory_tons"] == 498.87
    assert result["kpi"]["sales_30d_tons"] == 4600.57


# ---------------------------------------------------------------------------
# Test 4: product → material_name mapping
# ---------------------------------------------------------------------------
def test_product_material_name_mapping():
    """Sanity check: known product IDs map to the right Chinese material names."""
    from app.services.erp.kpi import _PRODUCT_MATERIAL_NAMES

    assert _PRODUCT_MATERIAL_NAMES["isoprene"] == ["异戊二烯"]
    assert _PRODUCT_MATERIAL_NAMES["cracked_c5"] == ["工业用裂解碳五", "裂解碳五"]
    assert "异戊二烯" in _PRODUCT_MATERIAL_NAMES["piperylene"] or \
           "间戊二烯" in _PRODUCT_MATERIAL_NAMES["piperylene"]
    # dcpd maps to 双环戊二烯 (NOT 双环戊二烯 in some other product)
    assert _PRODUCT_MATERIAL_NAMES["dcpd"] == ["双环戊二烯"]


# ---------------------------------------------------------------------------
# Test 5: utilization with NULL produce_qty/plan_qty (live MySQL behavior)
# ---------------------------------------------------------------------------
def test_get_product_kpis_live_utilization_null(seeded_sqlite_kb):
    """produce_qty/plan_qty are varbinary(0) in the live MySQL → util=None, not 0.0.

    This is a real-world case: the live MySQL's `produce_qty` and `plan_qty`
    columns exist but are unpopulated (varbinary(0)). Summing them gives 0.
    Reporting "0% utilization" would be wrong — the data is missing, not
    zero. The service must distinguish "no data" from "zero utilization".
    """
    from app.services.erp.kpi import ErpKpiService

    svc = ErpKpiService(db=None, org_id="test-org")
    result = svc.get_product_kpis(
        kb=seeded_sqlite_kb, product_id="isoprene", days=30
    )

    # utilization_pct must be None when both produced and planned are 0/NULL.
    # (If this returns 0.0, the service is over-confident in missing data.)
    assert result["kpi"]["utilization_pct"] is None, (
        "utilization_pct should be None when produce_qty/plan_qty are "
        "unpopulated, not 0.0 (which would be misleading)"
    )


# ---------------------------------------------------------------------------
# Test 6: connector failure → graceful stub
# ---------------------------------------------------------------------------
def test_get_product_kpis_stub_on_connector_failure():
    """If connector raises, return stub instead of letting exception bubble."""
    from app.services.erp.kpi import ErpKpiService

    # Build a KB pointing at a non-existent file — connector will fail
    bad_kb = _build_kb("/nonexistent/path/to/sqlite.db")

    svc = ErpKpiService(db=None, org_id="test-org")
    result = svc.get_product_kpis(kb=bad_kb, product_id="isoprene")

    assert result["stub"] is True
    assert result["is_live"] is False
    assert result["kpi"]["inventory_tons"] is None
    # Summary should mention the failure reason
    assert "summary" in result
    assert result["summary"]
