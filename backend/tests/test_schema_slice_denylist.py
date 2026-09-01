"""Tests for Phase 4 — Schema-slice internal-table denylist.

_app.internal tables (auth_user, cockpit_definitions, dataset_permissions, …)
must NOT appear in the compact schema slice so ERP business tables get the
limited <300-char budget.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class _FakeTable:
    def __init__(self, id, table_name, kb_id, table_role="TABLE", table_type="TABLE"):
        self.id = id
        self.table_name = table_name
        self.kb_id = kb_id
        self.table_role = table_role
        self.table_type = table_type


class _FakeCol:
    def __init__(self, table_meta_id, column_name, data_type="varchar", is_primary_key=False, ordinal=0):
        self.table_meta_id = table_meta_id
        self.column_name = column_name
        self.data_type = data_type
        self.is_primary_key = is_primary_key
        self.ordinal = ordinal


class TestSchemaSliceDenylist:
    def test_internal_tables_excluded(self):
        from app.services.data_source_runtime.data_source_runtime import _build_schema_slice

        db = MagicMock()

        tables = [
            _FakeTable(1, "auth_user", "kb1"),
            _FakeTable(2, "cockpit_definitions", "kb1"),
            _FakeTable(3, "dataset_permissions", "kb1"),
            _FakeTable(4, "erp_t_sal_outstock", "kb1", table_role="fact"),
            _FakeTable(5, "erp_t_sal_outstockentry", "kb1"),
        ]

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "KBTableMeta":
                m.filter.return_value.all.return_value = tables
            elif model.__name__ == "KBColumnMeta":
                cols = [
                    _FakeCol(1, "id", is_primary_key=True),
                    _FakeCol(4, "FID", is_primary_key=True),
                    _FakeCol(4, "FREALQTY", data_type="decimal"),
                    _FakeCol(4, "F_PAEZ_BHSAMOUNT", data_type="decimal"),
                    _FakeCol(5, "FID", is_primary_key=True),
                    _FakeCol(5, "FENTRYID", is_primary_key=True),
                ]
                m.filter.return_value.order_by.return_value.all.return_value = cols
            elif model.__name__ == "KBTableRelation":
                m.filter.return_value.all.return_value = []
            return m

        db.query.side_effect = fake_query

        result = _build_schema_slice(db, ["kb1"])
        slice_str = result.get("kb1", "")

        assert "auth_user" not in slice_str
        assert "cockpit_definitions" not in slice_str
        assert "dataset_permissions" not in slice_str
        assert "erp_t_sal_outstock" in slice_str

    def test_internal_table_prefixes_excluded(self):
        from app.services.data_source_runtime.data_source_runtime import _build_schema_slice

        db = MagicMock()
        tables = [
            _FakeTable(1, "access_model_config", "kb1"),
            _FakeTable(2, "data_sources", "kb1"),
            _FakeTable(3, "chat_messages", "kb1"),
            _FakeTable(4, "kb_documents", "kb1"),
            _FakeTable(5, "erp_v_stk_inventory", "kb1"),
        ]

        def fake_query(model):
            m = MagicMock()
            if model.__name__ == "KBTableMeta":
                m.filter.return_value.all.return_value = tables
            elif model.__name__ == "KBColumnMeta":
                cols = [
                    _FakeCol(1, "id", is_primary_key=True),
                    _FakeCol(2, "id", is_primary_key=True),
                    _FakeCol(3, "id", is_primary_key=True),
                    _FakeCol(4, "id", is_primary_key=True),
                    _FakeCol(5, "FBASEQTY", data_type="decimal"),
                ]
                m.filter.return_value.order_by.return_value.all.return_value = cols
            elif model.__name__ == "KBTableRelation":
                m.filter.return_value.all.return_value = []
            return m

        db.query.side_effect = fake_query

        result = _build_schema_slice(db, ["kb1"])
        slice_str = result.get("kb1", "")

        assert "access_model_config" not in slice_str
        assert "data_sources" not in slice_str
        assert "chat_messages" not in slice_str
        assert "kb_documents" not in slice_str
        assert "erp_v_stk_inventory" in slice_str


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
