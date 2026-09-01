"""Test: realized-price backfill pipeline."""
import os
import pytest
from unittest.mock import patch, MagicMock

os.environ.setdefault("FORECAST_DECISION_LOGGING_ENABLED", "true")


def test_backfill_fills_actual_price_t():
    """backfill_realized_prices should fill actual_price_t from ERP data."""
    from app.services.forecasting.accuracy_tracker import backfill_realized_prices

    db = MagicMock()

    log1 = MagicMock()
    log1.id = "log-1"
    log1.product_id = "裂解C5-裂解C5均价"
    log1.as_of_date = MagicMock()
    log1.as_of_date.isoformat.return_value = "2026-08-01T00:00:00"

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_row = ("2026-08-01", 8500.0)
    mock_conn.execute.return_value.fetchone.return_value = mock_row
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

    # Mock SQLAlchemy query chain: db.query(ForecastDecisionLog).filter(...).limit(200).all()
    query_mock = MagicMock()
    query_mock.filter.return_value.limit.return_value.all.side_effect = [
        [log1],  # first call: null_t_logs
        [],      # second call: null_th_logs
    ]
    db.query.return_value = query_mock

    with patch("app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine", return_value=mock_engine):
        result = backfill_realized_prices(db)

    assert result["backfilled_t"] >= 1


def test_backfill_handles_no_mysql_engine():
    """When MySQL engine is None, return early with error."""
    from app.services.forecasting.accuracy_tracker import backfill_realized_prices

    with patch("app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine", return_value=None):
        result = backfill_realized_prices(MagicMock())

    assert result["backfilled_t"] == 0
    assert len(result.get("errors", [])) > 0


def test_backfill_fills_actual_price_th_and_roi():
    """When actual_price_t is set and horizon passed, fill actual_price_th + roi."""
    from app.services.forecasting.accuracy_tracker import backfill_realized_prices

    db = MagicMock()

    import datetime as _dt
    log2 = MagicMock()
    log2.id = "log-2"
    log2.product_id = "裂解C5-裂解C5均价"
    log2.as_of_date = _dt.datetime(2026, 7, 1, tzinfo=_dt.timezone.utc)
    log2.horizon_day = 30
    log2.actual_price_t = 8000.0
    log2.actual_price_th = None
    log2.roi_pct = None
    log2.action = "buy"

    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_row = ("2026-07-31", 8400.0)
    mock_conn.execute.return_value.fetchone.return_value = mock_row
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

    query_mock = MagicMock()
    query_mock.filter.return_value.limit.return_value.all.side_effect = [
        [],       # null_t_logs: none
        [log2],   # null_th_logs: one
    ]
    db.query.return_value = query_mock

    with patch("app.services.forecasting.features.exogenous_loaders._resolve_mysql_engine", return_value=mock_engine):
        with patch("app.services.forecasting.features.decision_roi.score_decision", return_value=5.0):
            result = backfill_realized_prices(db)

    assert result["backfilled_th"] >= 1
