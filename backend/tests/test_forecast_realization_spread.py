"""Test realization spread computation and application."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from app.services.forecasting.ops.realization_spread import (
    apply_realization_spread,
    compute_realization_spread,
    update_target_spread,
)


def _make_db_mock(erp_df, quote_df):
    """Build a DB mock where pd.read_sql returns erp then quote."""
    db = MagicMock()
    # Patch pd.read_sql to return ERP first, then quotation
    read_sql_patch = patch(
        "app.services.forecasting.ops.realization_spread.pd.read_sql",
        side_effect=[erp_df, quote_df],
    )
    return db, read_sql_patch


@patch("app.services.forecasting.ops.realization_spread._ERP_TABLE_MAP", {"isoprene": "sale_erp_v_异戊二烯_data"})
@patch("app.models.forecasting.ForecastTarget")
def test_compute_spread_with_clean_data(mock_tgt_cls):
    """When ERP and quotation overlap cleanly, median ratio is computed."""
    erp_df = pd.DataFrame({
        "date": pd.date_range("2026-05-01", periods=30),
        "price": [1000.0] * 30,
    })
    quote_df = pd.DataFrame({
        "date": pd.date_range("2026-05-01", periods=30),
        "price": [1050.0] * 30,
    })
    tgt = MagicMock()
    tgt.datasource = {"table": "md_t_lz_price", "measure": "price", "filter": "product = 'isoprene'"}
    mock_tgt_cls.__table__ = MagicMock()
    mock_query = MagicMock()
    mock_query.filter.return_value.first.return_value = tgt
    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    with patch("app.services.forecasting.ops.realization_spread.pd.read_sql", side_effect=[erp_df, quote_df]):
        spread = compute_realization_spread(mock_session, product_id="isoprene", product_key="isoprene")

    assert spread is not None
    assert abs(spread - 0.9524) < 0.001


@patch("app.services.forecasting.ops.realization_spread._ERP_TABLE_MAP", {"isoprene": "sale_erp_v_异戊二烯_data"})
@patch("app.models.forecasting.ForecastTarget")
def test_compute_spread_returns_none_when_no_overlap(mock_tgt_cls):
    """When ERP and quotation dates don't overlap, return None."""
    erp_df = pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=5),
        "price": [1000.0] * 5,
    })
    quote_df = pd.DataFrame({
        "date": pd.date_range("2026-06-01", periods=5),
        "price": [1050.0] * 5,
    })
    tgt = MagicMock()
    tgt.datasource = {"table": "md_t_lz_price", "measure": "price"}
    mock_query = MagicMock()
    mock_query.filter.return_value.first.return_value = tgt
    mock_session = MagicMock()
    mock_session.query.return_value = mock_query

    with patch("app.services.forecasting.ops.realization_spread.pd.read_sql", side_effect=[erp_df, quote_df]):
        spread = compute_realization_spread(mock_session, product_id="isoprene", product_key="isoprene")

    assert spread is None


def test_apply_spread_adjusts_forecast():
    """apply_realization_spread correctly discounts the forecast."""
    assert apply_realization_spread(1000.0, 0.05) == 950.0
    assert apply_realization_spread(1000.0, 0.0) == 1000.0
    assert apply_realization_spread(1000.0, None) == 1000.0


def test_apply_spread_with_high_spread():
    """Large spreads (ERP much lower than quotation) discount aggressively."""
    assert apply_realization_spread(1000.0, 0.20) == 800.0
    assert apply_realization_spread(1000.0, -0.10) == 1100.0


@patch("app.services.forecasting.ops.realization_spread.compute_realization_spread")
def test_update_target_spread_persists(mock_compute):
    """update_target_spread writes the spread into target.model_config."""
    mock_compute.return_value = 0.97
    db = MagicMock()
    target = MagicMock()
    target.model_config = {"some_other_key": 1}

    result = update_target_spread(db, target, product_id="isoprene")
    assert result == 0.97
    assert target.model_config["realization_spread"] == 0.97
    assert target.model_config["some_other_key"] == 1
    db.commit.assert_called_once()
