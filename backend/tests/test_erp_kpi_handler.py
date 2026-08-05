"""Tests for the ask_erp_kpi tool handler.

TDD target: the handler must (a) resolve the MySQL KB from the
project's bound_kb_ids in the tool context, (b) hand it to
ErpKpiService, (c) return a clear error when no MySQL KB is bound.

The handler does NOT use a real DB here — it uses a mock DB and a
mock KnowledgeBase. The live-against-MySQL path is exercised by
test_erp_kpi_live.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest


def _kb(db_type: str = "mysql", name: str = "aipdp_data_warehouse_prod") -> SimpleNamespace:
    return SimpleNamespace(
        id="kb-1",
        db_type=db_type,
        host="10.10.10.49",
        port=3306,
        database_name="aipdp_data_warehouse_prod",
        username="root",
        password="x",
        name=name,
    )


def test_handler_resolves_kb_from_context_bound_ids():
    """Handler should pick the first MySQL KB from context.bound_kb_ids."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    captured: dict = {}

    class FakeQuery:
        def filter(self, *a, **kw): return self
        def all(self): return [_kb()]

    fake_db = MagicMock()
    fake_db.query.return_value = FakeQuery()

    with patch("app.services.erp.tool.ErpKpiService") as MockSvc:
        instance = MockSvc.return_value

        def fake_get(*args, **kw):
            captured["product_id"] = args[0] if args else kw.get("product_id")
            captured.update(kw)
            return {
                "is_live": True, "stub": False, "kpi": {"inventory_tons": 1.0}
            }

        instance.get_product_kpis = fake_get
        result = asyncio.run(_handle_ask_erp_kpi(
            args={"product": "isoprene", "days": 30},
            db=fake_db,
            user_id="u1",
            context={"bound_kb_ids": ["kb-1"], "org_id": "org1"},
        ))

    assert result["success"] is True
    # The handler must have passed the MySQL KB to the service
    assert captured.get("kb") is not None
    assert captured["kb"].db_type == "mysql"
    assert captured["product_id"] == "isoprene"
    assert captured["days"] == 30


def test_handler_returns_error_when_no_mysql_kb_bound():
    """No MySQL KB bound → clear error telling user to add a MySQL Resource."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    fake_db = MagicMock()
    # Empty bound list — handler should not even hit the DB
    result = asyncio.run(_handle_ask_erp_kpi(
        args={"product": "isoprene"},
        db=fake_db,
        user_id="u1",
        context={"bound_kb_ids": [], "org_id": "org1"},
    ))

    assert result["success"] is False
    assert "MySQL" in result["error"]
    assert "Resources" in result["error"]


def test_handler_ignores_non_mysql_kbs():
    """If only Postgres / Mongo KBs are bound, return the same error."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    class FakeQuery:
        def filter(self, *a, **kw): return self
        def all(self): return [_kb(db_type="postgres"), _kb(db_type="mongodb")]

    fake_db = MagicMock()
    fake_db.query.return_value = FakeQuery()

    result = asyncio.run(_handle_ask_erp_kpi(
        args={"product": "isoprene"},
        db=fake_db,
        user_id="u1",
        context={"bound_kb_ids": ["kb-1", "kb-2"], "org_id": "org1"},
    ))

    assert result["success"] is False
    assert "MySQL" in result["error"]


def test_handler_picks_first_mysql_when_multiple_bound():
    """When multiple MySQL KBs are bound, the handler picks the first."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    captured: dict = {}

    class FakeQuery:
        def filter(self, *a, **kw): return self
        def all(self): return [_kb(name="primary"), _kb(name="secondary")]

    fake_db = MagicMock()
    fake_db.query.return_value = FakeQuery()

    with patch("app.services.erp.tool.ErpKpiService") as MockSvc:
        instance = MockSvc.return_value

        def fake_get(*args, **kw):
            captured["product_id"] = args[0] if args else kw.get("product_id")
            captured.update(kw)
            return {
                "is_live": True, "stub": False, "kpi": {}
            }

        instance.get_product_kpis = fake_get
        asyncio.run(_handle_ask_erp_kpi(
            args={"product": "isoprene"},
            db=fake_db,
            user_id="u1",
            context={"bound_kb_ids": ["kb-1", "kb-2"], "org_id": "org1"},
        ))

    assert captured["kb"].name == "primary"


def test_handler_returns_error_for_unresolvable_product():
    """Handler short-circuits before touching the DB when product is unknown."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    result = asyncio.run(_handle_ask_erp_kpi(
        args={"product": "definitely-not-a-real-product-xyz"},
        db=MagicMock(),
        user_id="u1",
        context={"bound_kb_ids": ["kb-1"], "org_id": "org1"},
    ))

    assert result["success"] is False
    assert "resolve" in result["error"].lower() or "could not" in result["error"].lower()


def test_handler_requires_product_arg():
    """Empty / missing product arg → clear error."""
    from app.services.erp.tool import _handle_ask_erp_kpi

    result = asyncio.run(_handle_ask_erp_kpi(
        args={},
        db=MagicMock(),
        user_id="u1",
        context={"bound_kb_ids": ["kb-1"], "org_id": "org1"},
    ))

    assert result["success"] is False
    assert "product" in result["error"].lower()
