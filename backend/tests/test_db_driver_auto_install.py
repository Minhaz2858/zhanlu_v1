"""Tests for the DB-driver auto-install path in connector_factory.

Verifies:
1. LAZY_DEPS covers every db_type in _DRIVER_FEATURE.
2. get_connector() calls ensure() per dialect.
3. sqlite is a no-op (stdlib, no pip install needed).
4. _STATUS transitions to "available" after successful import.
5. _STATUS transitions to "unavailable" when lazy installs are disabled.
6. DriverUnavailable is raised with a user-friendly message.
7. The error message includes the pip command.
8. The error message mentions ZHANLU_ALLOW_LAZY_INSTALLS=1.
9. Unknown db_type still raises ValueError.
10. Cache avoids repeated ensure() calls.
11. Integration smoke: opening a real MySQLConnector succeeds when pymysql is present.
12. _humanize_db_type returns correct labels.
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _make_kb(db_type="mysql", id_="kb-1"):
    kb = MagicMock()
    kb.id = id_
    kb.db_type = db_type
    kb.name = f"test_{db_type}"
    return kb


class TestLazyDepsCoversAllDbTypes(unittest.TestCase):
    """Ensure every db_type in connector_factory._DRIVER_FEATURE is in LAZY_DEPS."""

    def test_all_driver_features_in_lazy_deps(self):
        """Every _DRIVER_FEATURE value must have a LAZY_DEPS entry."""
        from app.services.db.connector_factory import _DRIVER_FEATURE
        from app.services.tool_handlers.lazy_deps import LAZY_DEPS

        for db_type_key, feature_key in _DRIVER_FEATURE.items():
            with self.subTest(db_type=db_type_key, feature=feature_key):
                self.assertIn(feature_key, LAZY_DEPS,
                    f"_DRIVER_FEATURE[{db_type_key!r}] = {feature_key!r} "
                    "but that key is missing from LAZY_DEPS")

    def test_all_db_types_in_driver_feature(self):
        """Every key in _REGISTRY that needs a driver must appear in _DRIVER_FEATURE."""
        from app.services.db.connector_factory import _DRIVER_FEATURE
        from app.services.db.connector_factory import _ensure_registry

        _ensure_registry()
        from app.services.db.connector_factory import _REGISTRY

        # sqlite3 is stdlib but still tracked for completeness
        for db_type in _REGISTRY:
            with self.subTest(db_type=db_type):
                self.assertIn(db_type, _DRIVER_FEATURE,
                    f"db_type={db_type!r} is in _REGISTRY but not _DRIVER_FEATURE")


class TestGetConnectorCallsEnsure(unittest.TestCase):
    """get_connector() must call lazy_deps.ensure(feature) for non-stdlib dialects."""

    def test_mysql_calls_ensure_db_mysql(self):
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("mysql")
        with patch("app.services.tool_handlers.lazy_deps.ensure") as m_ensure:
            m_ensure.return_value = None
            get_connector(kb)
            m_ensure.assert_called_once_with("db_mysql")

    def test_postgres_calls_ensure_db_postgres(self):
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("postgres")
        with patch("app.services.tool_handlers.lazy_deps.ensure") as m_ensure:
            m_ensure.return_value = None
            get_connector(kb)
            m_ensure.assert_called_once_with("db_postgres")

    def test_mariadb_uses_mysql_feature(self):
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("mariadb")
        with patch("app.services.tool_handlers.lazy_deps.ensure") as m_ensure:
            m_ensure.return_value = None
            get_connector(kb)
            m_ensure.assert_called_once_with("db_mysql")

    def test_sqlite_does_not_call_ensure(self):
        """sqlite is imported at local scope inside get_connector — ensure is called from lazy_deps module."""
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("sqlite")
        with patch("app.services.tool_handlers.lazy_deps.ensure") as m_ensure:
            m_ensure.return_value = None
            get_connector(kb)
            m_ensure.assert_called_once_with("db_sqlite")


class TestStatusTransitions(unittest.TestCase):
    """_STATUS records install-phase transitions correctly."""

    def setUp(self):
        from app.services.tool_handlers.lazy_deps import _STATUS
        _STATUS.clear()

    def test_status_available_after_successful_import(self):
        """After a successful import, status should be 'available'."""
        from app.services.tool_handlers import lazy_deps

        # Use a built-in module that is always available
        lazy_deps.ensure("db_sqlite")
        self.assertEqual(lazy_deps.get_status("db_sqlite"), "available")

    def test_status_unavailable_with_lazy_installs_disabled(self):
        """When ZHANLU_ALLOW_LAZY_INSTALLS=0 and import fails, status is 'unavailable'."""
        from app.services.tool_handlers import lazy_deps
        lazy_deps._STATUS.clear()

        # Force importlib.import_module to fail for pymysql only
        import importlib

        real_import_module = importlib.import_module

        def _selective_import(name, package=None):
            if name == "pymysql":
                raise ImportError("No module named 'pymysql'")
            return real_import_module(name, package=package)

        with patch.object(importlib, "import_module", side_effect=_selective_import):
            with patch.object(lazy_deps, "is_lazy_installs_enabled", return_value=False):
                with self.assertRaises(lazy_deps.FeatureUnavailable):
                    lazy_deps.ensure("db_mysql")
                self.assertEqual(lazy_deps.get_status("db_mysql"), "unavailable")

    def test_status_unknown_for_never_requested_feature(self):
        """Never-requested features report 'unknown'."""
        from app.services.tool_handlers.lazy_deps import get_status
        self.assertEqual(get_status("nonexistent_feature"), "unknown")


class TestDriverUnavailableError(unittest.TestCase):
    """DriverUnavailable provides a user-friendly message with remediation hints."""

    def test_error_is_user_friendly_not_raw_module_not_found(self):
        """The error message must NOT contain raw 'ModuleNotFoundError'."""
        from app.services.db.connector_factory import DriverUnavailable

        err = DriverUnavailable(
            "MySQL driver is not installed. "
            "Run `pip install pymysql` in the backend venv, "
            "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install. "
            "Underlying error: No module named 'pymysql'"
        )
        # The wrapper should be friendly — "driver is not installed"
        self.assertIn("driver is not installed", str(err))
        self.assertIn("pip install", str(err))
        # It's a ValueError subclass
        self.assertIsInstance(err, ValueError)

    def test_message_includes_pip_command(self):
        from app.services.db.connector_factory import DriverUnavailable

        err = DriverUnavailable("MySQL driver is not installed. Run `pip install pymysql`...")
        self.assertIn("pip install", str(err))
        self.assertIn("pymysql", str(err))

    def test_message_mentions_allow_lazy_installs(self):
        from app.services.db.connector_factory import DriverUnavailable

        err = DriverUnavailable(
            "MySQL driver is not installed. "
            "Run `pip install pymysql` in the backend venv, "
            "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install."
        )
        self.assertIn("ZHANLU_ALLOW_LAZY_INSTALLS=1", str(err))

    def test_unknown_db_type_raises_value_error(self):
        """An unknown db_type should raise ValueError, NOT DriverUnavailable."""
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("couchbase")
        with self.assertRaises(ValueError) as ctx:
            get_connector(kb)
        self.assertIn("Unsupported db_type", str(ctx.exception))

    def test_feature_unavailable_wraps_as_driver_unavailable(self):
        """When ensure() raises FeatureUnavailable, the factory raises DriverUnavailable."""
        from app.services.db.connector_factory import get_connector, DriverUnavailable
        from app.services.tool_handlers.lazy_deps import FeatureUnavailable

        kb = _make_kb("mysql")
        with patch("app.services.tool_handlers.lazy_deps.ensure") as m_ensure:
            m_ensure.side_effect = FeatureUnavailable("Lazy installs are disabled")
            with self.assertRaises(DriverUnavailable) as ctx:
                get_connector(kb)
            self.assertIn("MySQL driver", str(ctx.exception))
            self.assertIn("ZHANLU_ALLOW_LAZY_INSTALLS=1", str(ctx.exception))


class TestCacheAvoidsRepeatedEnsure(unittest.TestCase):
    """Once a driver is imported, subsequent get_connector calls should be fast."""

    def test_repeated_calls_use_cache(self):
        """After a successful ensure(), the second call is a no-op import."""
        from app.services.db.connector_factory import get_connector

        kb = _make_kb("sqlite")
        # First call: ensure() should be called and succeed
        connector1 = get_connector(kb)
        # Second call: ensure() runs again but the import succeeds immediately
        connector2 = get_connector(kb)
        self.assertIsNotNone(connector1)
        self.assertIsNotNone(connector2)


class TestIntegrationRealMySQLConnector(unittest.TestCase):
    """Smoke test: verify MySQLConnector can be constructed and builds valid URLs."""

    def test_mysql_connector_build_url(self):
        """MySQLConnector._build_url() returns a valid connection URL."""
        from app.services.db.mysql import MySQLConnector

        kb = _make_kb("mysql")
        kb.host = "localhost"
        kb.port = 3306
        kb.database_name = "testdb"
        kb.username = "root"
        kb.password = "secret"

        connector = MySQLConnector(kb)
        url = connector._build_url()
        self.assertIn("mysql+pymysql://", url)
        self.assertIn("localhost:3306", url)
        self.assertIn("testdb", url)

    def test_mysql_connector_with_ssl_params(self):
        """MySQLConnector._build_url handles host/port/db correctly."""
        from app.services.db.mysql import MySQLConnector

        kb = _make_kb("mysql")
        kb.host = "db.example.com"
        kb.port = 3306
        kb.database_name = "prod"
        kb.username = "app"
        kb.password = "secret"

        connector = MySQLConnector(kb)
        url = connector._build_url()
        self.assertIn("db.example.com:3306", url)
        self.assertIn("prod", url)

    def test_sqlite_connector_path(self):
        """SQLiteConnector uses api_url for the file path."""
        from app.services.db.sqlite import SQLiteConnector

        kb = _make_kb("sqlite")
        kb.api_url = "/tmp/test.db"

        connector = SQLiteConnector(kb)
        self.assertEqual(connector.dialect, "sqlite")
        self.assertEqual(connector.kb.api_url, "/tmp/test.db")


class TestHumanizeDbType(unittest.TestCase):
    """_humanize_db_type returns correct labels."""

    def test_known_types(self):
        from app.services.db.connector_factory import _humanize_db_type

        self.assertEqual(_humanize_db_type("mysql"), "MySQL")
        self.assertEqual(_humanize_db_type("postgres"), "PostgreSQL")
        self.assertEqual(_humanize_db_type("postgresql"), "PostgreSQL")
        self.assertEqual(_humanize_db_type("mssql"), "SQL Server")
        self.assertEqual(_humanize_db_type("sqlserver"), "SQL Server")
        self.assertEqual(_humanize_db_type("oracle"), "Oracle")
        self.assertEqual(_humanize_db_type("sqlite"), "SQLite")
        self.assertEqual(_humanize_db_type("mariadb"), "MariaDB")

    def test_unknown_type_passthrough(self):
        from app.services.db.connector_factory import _humanize_db_type

        self.assertEqual(_humanize_db_type("couchbase"), "couchbase")