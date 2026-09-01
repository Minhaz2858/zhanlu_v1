"""Synthetic shop-database fixture for cross-database validation tests.

Provides a REAL SQLite database with an English snake_case shop schema
(``orders`` / ``order_items`` / ``products`` / ``customers`` — deliberately
NOT ERP-shaped) plus catalog-metadata and schema-graph mocks so the schema
validator / goal contract can be exercised against a clean, self-contained
fixture instead of the ERP catalog.

Builders are plain functions so tests in ``backend/tests/`` can import them
directly (``from tests.fixtures.shop_db.conftest import ...``); the pytest
fixtures at the bottom wrap them for tests inside this directory.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from unittest.mock import MagicMock

import pytest

# English snake_case shop schema — deliberately NOT ERP-shaped.
SHOP_TABLES: list[str] = ["orders", "order_items", "products", "customers"]

SHOP_COLUMNS: dict[str, list[str]] = {
    "orders": ["order_id", "customer_id", "order_date", "total_amount", "status"],
    "order_items": ["item_id", "order_id", "product_id", "quantity", "unit_price"],
    "products": ["product_id", "product_name", "category", "list_price"],
    "customers": ["customer_id", "customer_name", "region"],
}

# FK relations: (child_table, child_column, parent_table, parent_column)
SHOP_FKS: list[tuple[str, str, str, str]] = [
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("orders", "customer_id", "customers", "customer_id"),
]

SHOP_SAMPLE_ROWS: dict[str, list[tuple]] = {
    "products": [
        (1, "Widget", "Gadgets", 10.0),
        (2, "Gizmo", "Gadgets", 20.0),
        (3, "Sprocket", "Parts", 5.0),
    ],
    "customers": [
        (1, "Alice", "North"),
        (2, "Bob", "South"),
        (3, "Carol", "North"),
    ],
    "orders": [
        (101, 1, "2026-01-05", 40.0, "shipped"),
        (102, 2, "2026-02-11", 15.0, "pending"),
        (103, 3, "2026-03-20", 10.0, "shipped"),
    ],
    "order_items": [
        (1, 101, 1, 2, 10.0),
        (2, 101, 2, 1, 20.0),
        (3, 102, 3, 3, 5.0),
        (4, 103, 1, 1, 10.0),
    ],
}

_SCHEMA_SQL: dict[str, str] = {
    "products": (
        "CREATE TABLE products ("
        " product_id INTEGER PRIMARY KEY,"
        " product_name TEXT NOT NULL,"
        " category TEXT NOT NULL,"
        " list_price REAL NOT NULL)"
    ),
    "customers": (
        "CREATE TABLE customers ("
        " customer_id INTEGER PRIMARY KEY,"
        " customer_name TEXT NOT NULL,"
        " region TEXT NOT NULL)"
    ),
    "orders": (
        "CREATE TABLE orders ("
        " order_id INTEGER PRIMARY KEY,"
        " customer_id INTEGER NOT NULL REFERENCES customers(customer_id),"
        " order_date TEXT NOT NULL,"
        " total_amount REAL NOT NULL,"
        " status TEXT NOT NULL)"
    ),
    "order_items": (
        "CREATE TABLE order_items ("
        " item_id INTEGER PRIMARY KEY,"
        " order_id INTEGER NOT NULL REFERENCES orders(order_id),"
        " product_id INTEGER NOT NULL REFERENCES products(product_id),"
        " quantity INTEGER NOT NULL,"
        " unit_price REAL NOT NULL)"
    ),
}


def build_shop_db(path: str | None = None) -> str:
    """Create the SQLite DB (tables + sample data). Returns the file path."""
    path = path or os.path.join(tempfile.mkdtemp(prefix="shop_db_"), "shop.sqlite3")
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        for table in SHOP_TABLES:
            cur.execute(_SCHEMA_SQL[table])
        for table in SHOP_TABLES:
            rows = SHOP_SAMPLE_ROWS.get(table, [])
            if rows:
                placeholders = ",".join("?" * len(rows[0]))
                cur.executemany(
                    f"INSERT INTO {table} VALUES ({placeholders})", rows
                )
        conn.commit()
    finally:
        conn.close()
    return path


def query_shop(path: str, sql: str) -> list[dict]:
    """Run a read-only SQL statement against the fixture and return rows."""
    conn = sqlite3.connect(path)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def shop_catalog_meta() -> dict:
    """catalog_meta dict mock derived from the fixture's schema."""
    return {
        "tables": list(SHOP_TABLES),
        "columns": {t: list(cols) for t, cols in SHOP_COLUMNS.items()},
        "fks": [list(fk) for fk in SHOP_FKS],
    }


def shop_schema_graph():
    """SchemaGraph mock: ``find_master_for_fk`` resolves a guessed name/date
    column on an FK child table to its master (mirrors the real role-based
    lookup, e.g. ``order_items.product_name`` -> ``products.product_id``)."""
    graph = MagicMock()

    def _find_master_for_fk(table: str, col: str):
        for child, ccol, parent, pcol in SHOP_FKS:
            if table == child and col in SHOP_COLUMNS.get(parent, []):
                return (parent, pcol, pcol)
        return None

    graph.find_master_for_fk.side_effect = _find_master_for_fk
    return graph


def make_schema_service(columns: dict[str, list[str]] | None = None,
                        tables: list[str] | None = None):
    """SchemaService mock backed by the fixture's catalog metadata."""
    svc = MagicMock()
    columns = columns or SHOP_COLUMNS

    def _describe_table(kb_id: str, table: str) -> dict:
        cols = columns.get(table, [])
        return {"columns": [{"name": c} for c in cols]}

    svc.describe_table.side_effect = _describe_table
    if tables is not None:
        svc.list_tables.return_value = {"tables": list(tables)}
    return svc


# ---------------------------------------------------------------------------
# pytest fixtures (module-scoped — built once per session)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shop_db_path():
    """Real SQLite file with the shop schema + sample data."""
    return build_shop_db()


@pytest.fixture(scope="module")
def shop_catalog_meta_fixture():
    return shop_catalog_meta()
