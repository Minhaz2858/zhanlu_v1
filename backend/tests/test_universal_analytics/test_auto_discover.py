"""Tests for universal_analytics/auto_discover.py — P3 zero-config auto-discovery."""

import pytest
from unittest.mock import MagicMock, patch


# ── Event listener tests ────────────────────────────────────────────

class TestEventListener:
    def test_fires_on_db_kb_insert(self):
        """after_insert event fires when a database-type KB is created."""
        from app.services.universal_analytics.auto_discover import (
            _should_discover,
        )
        kb = MagicMock()
        kb.source_kind = "db"
        kb.db_type = "mysql"
        assert _should_discover(kb) is True

    def test_skips_non_db_kb(self):
        """Non-database KBs (files, APIs) are filtered out."""
        from app.services.universal_analytics.auto_discover import (
            _should_discover,
        )
        kb = MagicMock()
        kb.source_kind = "file"
        kb.db_type = None
        assert _should_discover(kb) is False

    def test_skips_db_kb_without_db_type(self):
        """DB source_kind but no db_type set → skip."""
        from app.services.universal_analytics.auto_discover import (
            _should_discover,
        )
        kb = MagicMock()
        kb.source_kind = "db"
        kb.db_type = None
        assert _should_discover(kb) is False


# ── Discovery execution tests ───────────────────────────────────────

class TestDiscoveryExecution:
    def test_run_discovery_writes_targets(self):
        """Discovery thread writes ForecastTarget rows correctly."""
        from app.services.universal_analytics.auto_discover import (
            _run_discovery,
        )

        mock_db = MagicMock()
        # No existing targets → first() returns None
        mock_db.query.return_value.filter.return_value.first.return_value = None

        with patch(
            "app.deps.SessionLocal"
        ) as mock_session_cls:
            mock_session_cls.return_value = mock_db

            with patch(
                "app.services.forecasting.discovery.discover"
            ) as mock_discover:
                mock_discover.return_value = [
                    {
                        "table": "sales", "time_column": "date",
                        "measure": "revenue", "dimensions": ["region"],
                        "row_count": 1000,
                    },
                    {
                        "table": "inventory", "time_column": "updated_at",
                        "measure": "qty", "dimensions": [],
                        "row_count": 500,
                    },
                ]

                _run_discovery(
                    kb_id="kb-001", org_id="org-001", app_id="app-001",
                    sleep_s=0,
                )

        # Verify ForecastTarget rows were added
        assert mock_db.add.call_count == 2
        assert mock_db.commit.called

    def test_run_discovery_skips_duplicates(self):
        """Already-existing product_keys are skipped, not duplicated."""
        from app.services.universal_analytics.auto_discover import (
            _run_discovery,
        )

        mock_db = MagicMock()
        # Mock an existing target with the same product_key
        existing = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = existing

        with patch(
            "app.deps.SessionLocal"
        ) as mock_session_cls:
            mock_session_cls.return_value = mock_db

            with patch(
                "app.services.forecasting.discovery.discover"
            ) as mock_discover:
                mock_discover.return_value = [
                    {"table": "sales", "time_column": "date",
                     "measure": "revenue", "dimensions": [], "row_count": 100},
                ]
                _run_discovery(
                    kb_id="kb-002", org_id="org-001", app_id="app-001",
                    sleep_s=0,
                )

        # Should NOT add because already exists
        mock_db.add.assert_not_called()
        mock_db.commit.assert_called()


# ── Flag gating test ────────────────────────────────────────────────

class TestFlagGating:
    def test_auto_discover_disabled_when_flag_off(self):
        """When UNIVERSAL_ANALYTICS_AUTO_DISCOVER=false, should_discover=False."""
        from app.services.universal_analytics.auto_discover import (
            check_auto_discover_enabled,
        )
        with patch.dict("os.environ", {"UNIVERSAL_ANALYTICS_AUTO_DISCOVER": "false"}):
            assert check_auto_discover_enabled() is False

    def test_auto_discover_enabled_when_flag_on(self):
        """When UNIVERSAL_ANALYTICS_AUTO_DISCOVER=true, should_discover=True."""
        from app.services.universal_analytics.auto_discover import (
            check_auto_discover_enabled,
        )
        with patch.dict("os.environ", {"UNIVERSAL_ANALYTICS_AUTO_DISCOVER": "true"}):
            assert check_auto_discover_enabled() is True
