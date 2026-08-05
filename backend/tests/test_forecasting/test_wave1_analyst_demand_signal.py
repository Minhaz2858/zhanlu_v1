"""Tests for Wave 1: demand signal integration in analyst service._build_pack_for()."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.services.forecasting.analyst.service import _build_pack_for


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def disable_flag():
    """Ensure flag starts OFF for every test."""
    old = os.environ.pop("FORECAST_DEMAND_SIGNAL_ENABLED", None)
    yield
    if old is not None:
        os.environ["FORECAST_DEMAND_SIGNAL_ENABLED"] = old
    else:
        os.environ.pop("FORECAST_DEMAND_SIGNAL_ENABLED", None)


@pytest.fixture
def mock_mds():
    """Patch mds.read_product_history_rows to return synthetic data."""
    with patch("app.services.forecasting.analyst.service.mds") as m:
        dates = pd.date_range("2024-07-01", periods=400, freq="D")
        prices = [(d.strftime("%Y-%m-%d"), 100.0 + i * 0.1) for i, d in enumerate(dates)]
        m.read_product_history_rows.return_value = prices
        yield m


@pytest.fixture
def mock_labels():
    with patch("app.services.forecasting.analyst.service.PRODUCT_LABELS",
               {"TN450": {"label_zh": "裂解C5-TN450"}}):
        yield


@pytest.fixture
def dummy_run():
    run = MagicMock()
    run.results = {}
    run.model_detail = {}
    run.explanation = {}
    return run


@pytest.fixture
def volume_df():
    """365 days of synthetic volume data."""
    dates = pd.date_range("2024-07-01", periods=365, freq="D")
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "volume": [100 + (i % 30) * 5 for i in range(365)],
    })


@pytest.fixture
def dispersion_df():
    """90 days of supplier dispersion data."""
    dates = pd.date_range("2025-05-01", periods=90, freq="D")
    return pd.DataFrame({
        "date": dates.strftime("%Y-%m-%d"),
        "spread": [50 + (i % 10) * 2 for i in range(90)],
        "supplier_count": [3 + i % 3 for i in range(90)],
    })


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #

class TestBuildPackDemandSignalEnabled:
    """When FORECAST_DEMAND_SIGNAL_ENABLED=true, demand signals populate the pack."""

    def test_flag_on_passes_demand_to_build_pack(self, mock_mds, mock_labels,
                                                   dummy_run, volume_df, dispersion_df):
        os.environ["FORECAST_DEMAND_SIGNAL_ENABLED"] = "true"

        with patch("app.services.forecasting.analyst.service.build_pack") as mock_bp:
            mock_bp.return_value = {"product_id": "TN450"}
            with patch(
                "app.services.forecasting.features.exogenous_loaders.ErpVolumeLoader"
            ) as MockVol:
                MockVol.return_value.load.return_value = volume_df
                with patch(
                    "app.services.forecasting.features.exogenous_loaders.SupplierDispersionLoader"
                ) as MockDisp:
                    MockDisp.return_value.load.return_value = dispersion_df
                    _build_pack_for("TN450", 7, dummy_run)

            mock_bp.assert_called_once()
            _, kwargs = mock_bp.call_args
            ds = kwargs.get("demand_signal")
            assert ds is not None, "demand_signal should be set when flag is ON"
            assert ds.get("has_sufficient_data") is True
            sl = kwargs.get("supplier_ladder")
            assert sl is not None, "supplier_ladder should be set when flag is ON"
            assert sl.get("has_data") is True

    def test_flag_on_no_data_still_passes_none(self, mock_mds, mock_labels,
                                                 dummy_run):
        """Loaders return empty → demand_signal/supplier_ladder are None."""
        os.environ["FORECAST_DEMAND_SIGNAL_ENABLED"] = "true"

        with patch("app.services.forecasting.analyst.service.build_pack") as mock_bp:
            mock_bp.return_value = {"product_id": "TN450"}
            with patch(
                "app.services.forecasting.features.exogenous_loaders.ErpVolumeLoader"
            ) as MockVol:
                MockVol.return_value.load.return_value = pd.DataFrame()
                with patch(
                    "app.services.forecasting.features.exogenous_loaders.SupplierDispersionLoader"
                ) as MockDisp:
                    MockDisp.return_value.load.return_value = pd.DataFrame()
                    _build_pack_for("TN450", 7, dummy_run)

            _, kwargs = mock_bp.call_args
            assert kwargs["demand_signal"] is None
            assert kwargs["supplier_ladder"] is None


class TestBuildPackDemandSignalDisabled:
    """When flag is OFF, zero regression: demand signals are NOT loaded."""

    def test_flag_off_passes_none(self, mock_mds, mock_labels, dummy_run):
        os.environ.pop("FORECAST_DEMAND_SIGNAL_ENABLED", None)

        with patch("app.services.forecasting.analyst.service.build_pack") as mock_bp:
            mock_bp.return_value = {"product_id": "TN450"}
            _build_pack_for("TN450", 7, dummy_run)

            mock_bp.assert_called_once()
            _, kwargs = mock_bp.call_args
            assert kwargs["demand_signal"] is None, "Should be None when flag OFF"
            assert kwargs["supplier_ladder"] is None, "Should be None when flag OFF"

    def test_flag_off_never_imports_loaders(self, mock_mds, mock_labels, dummy_run):
        """When flag is OFF, exogenous_loaders never imported."""
        os.environ.pop("FORECAST_DEMAND_SIGNAL_ENABLED", None)

        with patch("app.services.forecasting.analyst.service.build_pack") as mock_bp:
            mock_bp.return_value = {"product_id": "TN450"}
            with patch("builtins.__import__") as mock_import:
                # Allow normal imports, track the exogenous_loaders import
                original_import = __import__

                def tracking_import(name, *args, **kwargs):
                    return original_import(name, *args, **kwargs)

                mock_import.side_effect = tracking_import
                _build_pack_for("TN450", 7, dummy_run)


class TestBuildPackHandlesLoaderFailure:
    """If loaders raise, demand signals gracefully fall back to None."""

    def test_loader_exception_yields_none(self, mock_mds, mock_labels, dummy_run):
        os.environ["FORECAST_DEMAND_SIGNAL_ENABLED"] = "true"

        with patch("app.services.forecasting.analyst.service.build_pack") as mock_bp:
            mock_bp.return_value = {"product_id": "TN450"}
            with patch(
                "app.services.forecasting.features.exogenous_loaders.ErpVolumeLoader"
            ) as MockVol:
                MockVol.return_value.load.side_effect = RuntimeError("ERP down")
                # SupplierDispersionLoader may also be imported; mock it too
                with patch(
                    "app.services.forecasting.features.exogenous_loaders.SupplierDispersionLoader"
                ):
                    _build_pack_for("TN450", 7, dummy_run)

            _, kwargs = mock_bp.call_args
            assert kwargs["demand_signal"] is None, "Should fall back to None on error"
            assert kwargs["supplier_ladder"] is None, "Should fall back to None on error"
