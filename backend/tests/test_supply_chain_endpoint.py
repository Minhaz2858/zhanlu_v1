"""get_supply_chain — thin wrapper over get_grade_kpis_batch + summary math."""
from app.schemas.market_dashboard import GradeKpiData, GradeKpiRow
from app.services import market_dashboard_service as svc


def _row(grade, sku, sales=None, inventory=None, days=None, pending=None):
    return GradeKpiRow(
        grade=grade,
        sku_code=sku,
        sales_30d_tons=sales,
        inventory_tons=inventory,
        inventory_days=days,
        pending_tons=pending,
    )


def test_get_supply_chain_computes_weighted_summary(monkeypatch):
    """3 SKUs (one null inventory, one null pending) → totals + weighted
    avg_days_pending = Σ(days_pendingᵢ·pendingᵢ) / Σ(pendingᵢ)."""
    rows = [
        _row("45#", "G101000001", sales=100.0, inventory=50.0, days=5.0, pending=20.0),
        _row("35#", "G101000002", sales=200.0, inventory=None, days=None, pending=40.0),
        _row("55#", "G101000003", sales=50.0, inventory=10.0, days=2.0, pending=None),
    ]
    monkeypatch.setattr(
        svc, "get_grade_kpis_batch",
        lambda pids: {pids[0]: GradeKpiData(product_id=pids[0], rows=rows, is_live=True)},
    )

    result = svc.get_supply_chain("isoprene")

    assert result.product_id == "isoprene"
    assert result.is_live is True
    s = result.summary
    assert s is not None
    # totals skip null values
    assert s.sales_30d_tons == 350.0
    assert s.inventory_tons == 60.0   # 50 + 10 (null skipped)
    assert s.pending_tons == 60.0     # 20 + 40 (null skipped)
    # weighted days: row A = 6 (20/100*30), row B = 6 (40/200*30)
    #   → (6*20 + 6*40) / (20 + 40) = 360 / 60 = 6.0
    assert s.avg_days_pending == 6.0


def test_get_supply_chain_zero_sales_skips_avg_days(monkeypatch):
    """A row with pending but zero 30d sales must not divide by zero."""
    rows = [
        _row("45#", "G101000001", sales=100.0, inventory=50.0, days=5.0, pending=20.0),
        _row("35#", "G101000002", sales=0.0, inventory=None, days=None, pending=999.0),
    ]
    monkeypatch.setattr(
        svc, "get_grade_kpis_batch",
        lambda pids: {pids[0]: GradeKpiData(product_id=pids[0], rows=rows, is_live=True)},
    )

    result = svc.get_supply_chain("isoprene")
    # Only row A qualifies → 6.0
    assert result.summary.avg_days_pending == 6.0


def test_get_supply_chain_empty_rows_returns_none_summary(monkeypatch):
    """No ERP mapping → empty rows, summary fields all None (never crash)."""
    monkeypatch.setattr(
        svc, "get_grade_kpis_batch",
        lambda pids: {pids[0]: GradeKpiData(product_id=pids[0], rows=[], is_live=False)},
    )

    result = svc.get_supply_chain("crude_oil")
    assert result.rows == []
    assert result.is_live is False
    s = result.summary
    assert s is not None
    assert s.sales_30d_tons is None
    assert s.inventory_tons is None
    assert s.pending_tons is None
    assert s.avg_days_pending is None


def test_get_supply_chain_preserves_window_dates(monkeypatch):
    """window_start/window_end flow through the wrapper untouched."""
    monkeypatch.setattr(
        svc, "get_grade_kpis_batch",
        lambda pids: {
            pids[0]: GradeKpiData(
                product_id=pids[0],
                rows=[],
                window_start="2026-07-16",
                window_end="2026-08-15",
            )
        },
    )

    result = svc.get_supply_chain("isoprene")
    assert result.window_start == "2026-07-16"
    assert result.window_end == "2026-08-15"


def test_get_grade_kpis_batch_sets_window_dates(monkeypatch):
    """The batch function exposes the actual 30-day window (start = end - 30d)."""

    class _FakeResult:
        def __init__(self, sql):
            self.sql = sql

        def first(self):
            if "MAX(DATE(PLANDATE))" in self.sql:
                return ("2026-08-15",)
            return None

        def all(self):
            return []

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params=None):
            return _FakeResult(str(statement))

    class _FakeEngine:
        def connect(self):
            return _FakeConn()

    monkeypatch.setattr(svc, "get_mysql_engine", lambda: _FakeEngine())

    result = svc.get_grade_kpis_batch(["isoprene"])["isoprene"]

    assert result.window_end == "2026-08-15"
    assert result.window_start == "2026-07-16"
