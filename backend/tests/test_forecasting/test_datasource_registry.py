"""Tests for datasource_registry.py — P1: DataSource Strategy registry.

Tests the BaseDataSource ABC, EdiaMysqlStrategy, GenericKBStrategy,
get_datasource() factory, and the engine's _fetch_series() dispatch.
"""

import pandas as pd
import pytest
from unittest.mock import MagicMock, patch, PropertyMock

from app.services.forecasting.datasource_registry import (
    BaseDataSource,
    EdiaMysqlStrategy,
    GenericKBStrategy,
    get_datasource,
    quote_identifier,
)


# ── Synthetic helpers ──────────────────────────────────────────────

def _make_target(datasource: dict) -> MagicMock:
    """Return a mock ForecastTarget with the given datasource dict."""
    target = MagicMock()
    target.id = "target-001"
    target.org_id = "org-001"
    target.datasource = datasource
    return target


# ── Factory tests ───────────────────────────────────────────────────

class TestGetDatasource:
    def test_edia_mysql_returns_edia_strategy(self):
        s = get_datasource("edia_mysql")
        assert isinstance(s, EdiaMysqlStrategy)

    def test_generic_kb_returns_generic_strategy(self):
        s = get_datasource("generic_kb")
        assert isinstance(s, GenericKBStrategy)

    def test_unknown_source_falls_back_to_edia_strategy(self):
        s = get_datasource("nonexistent")
        assert isinstance(s, EdiaMysqlStrategy)


# ── Quote identifier tests ──────────────────────────────────────────

class TestQuoteIdentifier:
    def test_mysql_backtick_quoting(self):
        assert quote_identifier("my_table", "mysql") == "`my_table`"

    def test_postgres_double_quote(self):
        assert quote_identifier("my_table", "postgres") == '"my_table"'

    def test_unknown_db_type_defaults_double_quote(self):
        assert quote_identifier("my_table", None) == '"my_table"'
        assert quote_identifier("my_table", "oracle") == '"my_table"'

    def test_empty_name_is_passthrough(self):
        assert quote_identifier("", "mysql") == ""


# ── EdiaMysqlStrategy tests ─────────────────────────────────────────

class TestEdiaMysqlStrategy:
    @patch("app.services.forecasting.mysql_data_source.MysqlDataSource")
    def test_fetch_delegates_to_mysql_data_source(self, mock_mysql_cls):
        # Arrange
        mock_src = MagicMock()
        mock_src.read_history.return_value = pd.DataFrame({
            "ds": pd.date_range("2026-01-01", periods=5),
            "y": [10.0, 20.0, 30.0, 40.0, 50.0],
        })
        mock_mysql_cls.return_value = mock_src

        target = _make_target({"source": "edia_mysql", "table": "md_t_lz_price"})
        strategy = EdiaMysqlStrategy()
        db = MagicMock()

        # Act
        result = strategy.fetch(target, db)

        # Assert
        assert isinstance(result, pd.Series)
        assert len(result) == 5
        assert result.index[0] == pd.Timestamp("2026-01-01")
        assert result.iloc[-1] == 50.0
        mock_src.read_history.assert_called_once_with(target.datasource)

    @patch("app.services.forecasting.mysql_data_source.MysqlDataSource")
    def test_fetch_returns_none_on_error(self, mock_mysql_cls):
        mock_mysql_cls.side_effect = RuntimeError("connection failed")
        target = _make_target({"source": "edia_mysql"})
        strategy = EdiaMysqlStrategy()

        result = strategy.fetch(target, MagicMock())
        assert result is None


# ── GenericKBStrategy tests ─────────────────────────────────────────

class TestGenericKBStrategy:
    @patch("app.services.db.query_service.QueryService")
    def test_fetch_builds_sql_and_returns_series(self, mock_qs_cls):
        # Arrange
        mock_qs = MagicMock()
        mock_qs.execute.return_value = {
            "rows": [
                {"t": "2026-01-01", "y": 100.0},
                {"t": "2026-01-02", "y": 200.0},
                {"t": "2026-01-03", "y": 300.0},
            ]
        }
        mock_qs_cls.return_value = mock_qs

        mock_db = MagicMock()
        mock_kb = MagicMock()
        mock_kb.db_type = "mysql"
        # First query: KnowledgeBase lookup
        mock_db.query.return_value.filter.return_value.first.return_value = mock_kb

        target = _make_target({
            "source": "generic_kb",
            "kb_id": "kb-123",
            "table": "sales",
            "time_column": "date",
            "measure": "revenue",
            "dimensions": ["region"],
        })

        strategy = GenericKBStrategy()

        # Act
        result = strategy.fetch(target, mock_db)

        # Assert
        assert isinstance(result, pd.Series)
        assert len(result) == 3
        assert result.iloc[0] == 100.0

        # Verify SQL was generated with MySQL backtick quoting
        call_sql = mock_qs.execute.call_args[0][1]
        assert "`date`" in call_sql
        assert "`revenue`" in call_sql
        assert "`sales`" in call_sql
        assert "`region`" in call_sql

    def test_fetch_returns_none_when_missing_config(self):
        target = _make_target({"source": "generic_kb"})  # no table/time_column/measure
        strategy = GenericKBStrategy()
        result = strategy.fetch(target, MagicMock())
        assert result is None

    @patch("app.services.db.query_service.QueryService")
    def test_fetch_returns_none_when_query_fails(self, mock_qs_cls):
        mock_qs = MagicMock()
        mock_qs.execute.side_effect = RuntimeError("query timeout")
        mock_qs_cls.return_value = mock_qs

        mock_db = MagicMock()
        mock_kb = MagicMock()
        mock_kb.db_type = "postgres"
        mock_db.query.return_value.filter.return_value.first.return_value = mock_kb

        target = _make_target({
            "source": "generic_kb",
            "kb_id": "kb-123",
            "table": "t",
            "time_column": "ts",
            "measure": "val",
        })

        strategy = GenericKBStrategy()
        result = strategy.fetch(target, mock_db)
        assert result is None


# ── Engine dispatch integration test ────────────────────────────────

class TestEngineDispatch:
    def test_fetch_series_dispatches_via_registry(self):
        """Verify engine._fetch_series delegates to get_datasource().fetch()."""
        from app.models.forecasting import ForecastTarget

        target = MagicMock(spec=ForecastTarget)
        target.id = "t-1"
        target.datasource = {"source": "edia_mysql", "table": "x"}

        with patch(
            "app.services.forecasting.datasource_registry.get_datasource"
        ) as mock_get:
            mock_strategy = MagicMock()
            mock_strategy.fetch.return_value = pd.Series(
                [1.0, 2.0], index=pd.date_range("2026-01-01", periods=2)
            )
            mock_get.return_value = mock_strategy

            from app.services.forecasting.engine import ForecastEngine
            engine = ForecastEngine(MagicMock())
            result = engine._fetch_series(target)

            # Must have called get_datasource with the right source type
            mock_get.assert_called_once_with("edia_mysql")
            mock_strategy.fetch.assert_called_once_with(target, engine._db)
            assert isinstance(result, pd.Series)
            assert len(result) == 2
