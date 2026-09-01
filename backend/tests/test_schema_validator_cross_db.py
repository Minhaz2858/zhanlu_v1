"""Schema-validator cross-database validation against the shop_db fixture.

Proves the validator + goal-contract detectors are database-agnostic by
exercising them against a clean, English snake_case shop schema
(orders / order_items / products / customers — deliberately NOT ERP-shaped):
unknown column -> did-you-mean, unknown table -> did-you-mean, FK-master
JOIN hint, plus real SQLite rows feeding the metadata-only shape detector.
"""

import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.goal_contract import is_metadata_only_rows
from app.services.nl2sql.schema_validator import validate_against_schema

from tests.fixtures.shop_db.conftest import (
    SHOP_COLUMNS,
    SHOP_TABLES,
    build_shop_db,
    make_schema_service,
    query_shop,
    shop_schema_graph,
)

SCHEMA_GRAPH = "app.services.db.schema_graph.SchemaGraph"


class TestSchemaValidatorCrossDb(unittest.TestCase):
    """Did-you-mean + FK hints against the shop fixture (no ERP naming)."""

    def _make_db(self):
        db = MagicMock()
        q = MagicMock()
        q.filter.return_value.first.return_value = MagicMock(
            id="shop-kb", db_type="sqlite"
        )
        db.query.return_value = q
        return db

    def _ctx(self, svc, graph):
        stack = ExitStack()
        stack.enter_context(
            patch("app.services.nl2sql.schema_validator.SchemaService",
                  return_value=svc)
        )
        stack.enter_context(patch(SCHEMA_GRAPH, return_value=graph))
        return stack

    def test_unknown_column_gets_did_you_mean(self):
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT product_nam FROM products", "shop-kb", db,
                did_you_mean=True,
            )
        self.assertFalse(result["is_valid"])
        text = " ".join(result["available_suggestions"])
        self.assertIn("product_name", text)

    def test_unknown_qualified_column_gets_suggestion(self):
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT products.product_nam FROM products", "shop-kb", db,
                did_you_mean=True,
            )
        self.assertFalse(result["is_valid"])
        text = " ".join(result["available_suggestions"])
        self.assertIn("product_name", text)
        self.assertIn("products", result["available_columns"])

    def test_unknown_table_gets_did_you_mean(self):
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT * FROM productz", "shop-kb", db, did_you_mean=True,
            )
        self.assertFalse(result["is_valid"])
        text = " ".join(result["available_suggestions"])
        self.assertIn("productz", text)
        self.assertIn("products", text)

    def test_fk_master_hint_joins_products(self):
        """Guessing a products column on order_items gets a JOIN hint."""
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT order_items.product_name FROM order_items",
                "shop-kb", db, did_you_mean=True,
            )
        self.assertFalse(result["is_valid"])
        text = " ".join(result["available_suggestions"])
        self.assertIn("product_name is an FK to products.product_id", text)
        self.assertIn("JOIN that table first", text)

    def test_did_you_mean_param_false_disables_suggestions(self):
        """Per-call opt-out: the new did_you_mean param overrides the flag."""
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT product_nam FROM products", "shop-kb", db,
                did_you_mean=False,
            )
        self.assertFalse(result["is_valid"])
        self.assertEqual(result["available_suggestions"], [])

    def test_valid_shop_sql_passes(self):
        db = self._make_db()
        svc = make_schema_service(columns=SHOP_COLUMNS, tables=SHOP_TABLES)
        with self._ctx(svc, shop_schema_graph()):
            result = validate_against_schema(
                "SELECT product_name, list_price FROM products",
                "shop-kb", db, did_you_mean=True,
            )
        self.assertTrue(result["is_valid"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["available_suggestions"], [])


class TestShopDbRealRows(unittest.TestCase):
    """Real SQLite rows from the fixture feeding the shape detector."""

    def test_metadata_query_rows_are_flagged(self):
        path = build_shop_db()
        rows = query_shop(
            path,
            "SELECT MIN(order_date) AS min_d, MAX(order_date) AS max_d "
            "FROM orders",
        )
        self.assertEqual(len(rows), 1)
        self.assertTrue(is_metadata_only_rows(rows))

    def test_business_aggregation_rows_not_flagged(self):
        path = build_shop_db()
        rows = query_shop(
            path,
            "SELECT p.product_name, SUM(oi.quantity * oi.unit_price) AS revenue "
            "FROM order_items oi "
            "JOIN products p ON p.product_id = oi.product_id "
            "GROUP BY p.product_name",
        )
        self.assertEqual(len(rows), 3)
        self.assertFalse(is_metadata_only_rows(rows))
        names = {r["product_name"] for r in rows}
        self.assertEqual(names, {"Widget", "Gizmo", "Sprocket"})


if __name__ == "__main__":
    unittest.main()
