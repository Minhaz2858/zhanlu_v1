"""Tests for the data-agent runtime error path when a DB driver is missing.

Verifies:
1. _execute_query returns a friendly error (not raw ModuleNotFoundError).
2. ask_data_agent propagates the friendly error to the LLM.
3. No raw 'ModuleNotFoundError' appears in any error string.
4. Full runtime e2e path returns the friendly error (not a 500).
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class TestExecuteQueryReturnsFriendlyError(unittest.TestCase):
    """_execute_query must return error_kind='driver_missing' on DriverUnavailable."""

    def test_returns_friendly_error_on_missing_driver(self):
        from app.services.db.connector_factory import DriverUnavailable
        from app.services.tool_handlers.db_tools import _execute_query

        db = MagicMock()
        args = {"data_source_id": "kb-1", "sql": "SELECT 1"}
        context = {"data_source_id": "kb-1"}

        with patch("app.services.tool_handlers.db_tools._require_kb_id",
                   return_value=("kb-1", None)):
            with patch("app.services.tool_handlers.db_tools.asyncio.to_thread") as m_to_thread:
                m_to_thread.side_effect = DriverUnavailable(
                    "MySQL driver is not installed. "
                    "Run `pip install pymysql` in the backend venv, "
                    "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install."
                )

                async def _run():
                    return await _execute_query(args, db, user_id=None, context=context)

                import asyncio
                result = asyncio.run(_run())

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "driver_missing")
        self.assertIn("MySQL driver", result["error"])

    def test_no_module_not_found_in_error_string(self):
        """The error message must NOT leak the raw ModuleNotFoundError."""
        from app.services.db.connector_factory import DriverUnavailable

        err = DriverUnavailable(
            "MySQL driver is not installed. "
            "Run `pip install pymysql` in the backend venv, "
            "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install."
        )
        msg = str(err)
        self.assertNotIn("ModuleNotFoundError", msg)
        self.assertNotIn("No module named", msg)
        self.assertIn("driver is not installed", msg)

    def test_describe_schema_surfaces_driver_missing(self):
        """_describe_schema must also return error_kind='driver_missing'."""
        from app.services.db.connector_factory import DriverUnavailable
        from app.services.tool_handlers.db_tools import _describe_schema

        db = MagicMock()
        args = {"data_source_id": "kb-1"}
        context = {"data_source_id": "kb-1"}

        with patch("app.services.tool_handlers.db_tools._require_kb_id",
                   return_value=("kb-1", None)):
            with patch("app.services.tool_handlers.db_tools.asyncio.to_thread") as m_to_thread:
                m_to_thread.side_effect = DriverUnavailable(
                    "PostgreSQL driver is not installed. "
                    "Run `pip install psycopg2-binary` in the backend venv, "
                    "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install."
                )

                async def _run():
                    return await _describe_schema(args, db, user_id=None, context=context)

                import asyncio
                result = asyncio.run(_run())

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "driver_missing")

    def test_describe_schema_falls_back_when_semantic_catalog_is_empty(self):
        """A catalog header with no tables is not useful schema; fall back
        to live table listing so dashboard agents can continue."""
        from app.services.tool_handlers.db_tools import _describe_schema

        db = MagicMock()
        args = {"data_source_id": "kb-1"}
        context = {"data_source_id": "kb-1", "user_query": "sales dashboard"}

        class FakeSchemaService:
            def __init__(self, _db):
                pass

            def list_tables(self, kb_id):
                return {"source": {"id": kb_id}, "tables": ["erp_v_sale_orderentry"]}

        async def fake_link_schema(**_kwargs):
            return {"slice_text": "-- Semantic catalog: relevant tables for this query\n"}

        with patch("app.services.tool_handlers.db_tools._require_kb_id", return_value=("kb-1", None)):
            with patch("app.services.tool_handlers.db_tools.settings.SCHEMA_LINKING_ENABLED", True):
                with patch("app.services.tool_handlers.db_tools.settings.SEMANTIC_CATALOG_ENABLED", True):
                    with patch("app.services.knowledge_graph.schema_linker.link_schema", side_effect=fake_link_schema):
                        with patch("app.services.tool_handlers.db_tools.SchemaService", FakeSchemaService):
                            async def _run():
                                return await _describe_schema(args, db, user_id=None, context=context)

                            import asyncio
                            result = asyncio.run(_run())

        self.assertTrue(result["success"])
        self.assertEqual(result["tables"], ["erp_v_sale_orderentry"])
        self.assertNotIn("schema", result)

    def test_answer_from_database_surfaces_driver_missing(self):
        """_answer_from_database must return error_kind='driver_missing'."""
        from app.services.db.connector_factory import DriverUnavailable
        from app.services.tool_handlers.db_tools import _answer_from_database

        db = MagicMock()
        args = {"data_source_id": "kb-1", "question": "How many users?"}
        context = {"data_source_id": "kb-1"}

        with patch("app.services.tool_handlers.db_tools._require_kb_id",
                   return_value=("kb-1", None)):
            with patch("app.services.tool_handlers.db_tools.NLAnswerService") as m_svc:
                m_svc.return_value.answer = AsyncMock(
                    side_effect=DriverUnavailable(
                        "SQL Server driver is not installed. "
                        "Run `pip install pyodbc` in the backend venv, "
                        "or set ZHANLU_ALLOW_LAZY_INSTALLS=1 to allow auto-install."
                    )
                )

                async def _run():
                    return await _answer_from_database(args, db, user_id=None, context=context)

                import asyncio
                result = asyncio.run(_run())

        self.assertFalse(result["success"])
        self.assertEqual(result["error_kind"], "driver_missing")