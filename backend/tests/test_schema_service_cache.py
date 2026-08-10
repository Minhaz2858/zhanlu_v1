"""Tests for the SchemaService TTL cache (Tier 1 latency work).

describe_all() on a remote warehouse takes seconds per call and was
repeated on EVERY query. The TTL cache must:
1. Serve repeat calls from cache (one introspection per TTL window).
2. Key by (kb_id, op, args) — different max_tables are distinct entries.
3. Respect SCHEMA_CACHE_TTL_SECONDS <= 0 (disabled).
4. Support explicit invalidation per KB.
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


def _db_with_kb():
    db = MagicMock()
    kb = MagicMock()
    kb.id = "kb-1"
    kb.name = "warehouse"
    kb.db_type = "mysql"
    kb.database_name = "prod"
    kb.source_kind = "database"
    kb.is_deleted = False
    db.query.return_value.filter.return_value.first.return_value = kb
    return db


class _FakeConn:
    """Context-manager connector that counts introspection calls."""

    def __init__(self):
        self.list_tables_calls = 0
        self.describe_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_tables(self):
        self.list_tables_calls += 1
        return ["t1", "t2"]

    def describe_table(self, t):
        self.describe_calls += 1
        return [
            {"name": "id", "type": "BIGINT", "nullable": False, "pk": True, "default": None}
        ]


class TestSchemaServiceCache(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        self._old_ttl = settings.SCHEMA_CACHE_TTL_SECONDS
        settings.SCHEMA_CACHE_TTL_SECONDS = 3600
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache()

    def tearDown(self):
        from app.config import settings
        settings.SCHEMA_CACHE_TTL_SECONDS = self._old_ttl
        from app.services.db.schema_service import invalidate_schema_cache
        invalidate_schema_cache()

    def _svc(self):
        from app.services.db.schema_service import SchemaService
        return SchemaService(_db_with_kb())

    def test_describe_all_caches_second_call(self):
        conn = _FakeConn()
        with patch("app.services.db.schema_service.get_connector", return_value=conn):
            svc = self._svc()
            r1 = svc.describe_all("kb-1", max_tables=30)
            r2 = svc.describe_all("kb-1", max_tables=30)
        self.assertEqual(conn.list_tables_calls, 1)
        self.assertEqual(conn.describe_calls, 2)  # t1, t2 introspected once each
        self.assertEqual(r1, r2)
        self.assertEqual(len(r2["tables"]), 2)

    def test_distinct_entries_per_max_tables(self):
        conn = _FakeConn()
        with patch("app.services.db.schema_service.get_connector", return_value=conn):
            svc = self._svc()
            svc.describe_all("kb-1", max_tables=30)
            svc.describe_all("kb-1", max_tables=10)
        self.assertEqual(conn.list_tables_calls, 2)

    def test_cache_disabled_when_ttl_nonpositive(self):
        from app.config import settings
        settings.SCHEMA_CACHE_TTL_SECONDS = 0
        conn = _FakeConn()
        with patch("app.services.db.schema_service.get_connector", return_value=conn):
            svc = self._svc()
            svc.describe_all("kb-1")
            svc.describe_all("kb-1")
        self.assertEqual(conn.list_tables_calls, 2)

    def test_invalidate_drops_entries(self):
        from app.services.db.schema_service import invalidate_schema_cache
        conn = _FakeConn()
        with patch("app.services.db.schema_service.get_connector", return_value=conn):
            svc = self._svc()
            svc.describe_all("kb-1")
            invalidate_schema_cache("kb-1")
            svc.describe_all("kb-1")
        self.assertEqual(conn.list_tables_calls, 2)

    def test_list_tables_and_describe_table_cached(self):
        conn = _FakeConn()
        with patch("app.services.db.schema_service.get_connector", return_value=conn):
            svc = self._svc()
            svc.list_tables("kb-1")
            svc.list_tables("kb-1")
            svc.describe_table("kb-1", "t1")
            svc.describe_table("kb-1", "t1")
        self.assertEqual(conn.list_tables_calls, 1)
        self.assertEqual(conn.describe_calls, 1)


if __name__ == "__main__":
    unittest.main()
