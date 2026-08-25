"""Tests for the Data-Concepts-aware query composer."""
import pytest
from app.services.db.query_composer import (
    compose_queries,
    ComposedQuery,
    _parse_concept_catalog,
    _map_metrics_to_tables,
)


_CATALOG_SALES = (
    "- **Sales (销售)**: erp_t_sal_outstockentry + erp_t_sal_outstock\n"
    "  Measures: Volume→FREALQTY, Revenue→FYKSBGHAMOUNT, Margin→FYKSBGHALLAMOUNT\n"
    "  Columns: FREALQTY, FYKSBGHAMOUNT, FYKSBGHALLAMOUNT, FMATERIALID\n"
    "  Date column: FDATE"
)

_CATALOG_INVENTORY = (
    "- **Inventory (库存)**: erp_t_stock\n"
    "  Measures: Qty→FQTY\n"
    "  Columns: FQTY, FMATERIALID\n"
    "  Date column: FDATE"
)


def test_parse_concept_catalog():
    parsed = _parse_concept_catalog(_CATALOG_SALES)
    assert "sales" in parsed or "销售" in parsed
    entry = list(parsed.values())[0]
    assert "erp_t_sal_outstockentry" in entry.tables
    assert "FREALQTY" in entry.measures.values()
    assert entry.date_column == "FDATE"


def test_map_metrics_to_tables():
    catalog = _parse_concept_catalog(_CATALOG_SALES + "\n" + _CATALOG_INVENTORY)
    mapping = _map_metrics_to_tables(
        metrics=["volume", "revenue", "margin", "inventory"],
        catalog=catalog,
    )
    assert len(mapping) >= 1
    all_metrics = [m for group in mapping.values() for m, _, _ in group]
    assert len(all_metrics) >= 3  # at least volume, revenue, margin


def test_compose_queries_groups_metrics_by_table():
    queries = compose_queries(
        metrics=["volume", "revenue", "margin"],
        kb_id="kb1",
        filters={"period": "2026-07"},
        concept_catalog=_CATALOG_SALES,
    )
    # All 3 metrics should be in ONE query (same table)
    assert len(queries) == 1
    assert "FREALQTY" in queries[0].sql
    assert "FYKSBGHAMOUNT" in queries[0].sql
    assert "FYKSBGHALLAMOUNT" in queries[0].sql
    assert "2026-07" in queries[0].sql


def test_compose_queries_separates_different_tables():
    queries = compose_queries(
        metrics=["volume", "revenue", "inventory"],
        kb_id="kb1",
        filters={"period": "2026-07"},
        concept_catalog=_CATALOG_SALES + "\n" + _CATALOG_INVENTORY,
    )
    # Should produce 2 queries: sales metrics + inventory
    assert len(queries) == 2
    labels = [q.label for q in queries]
    assert any("sales" in l for l in labels)
    assert any("inventory" in l or "stock" in l for l in labels)


def test_compose_queries_returns_empty_when_no_concepts():
    queries = compose_queries(
        metrics=["volume", "revenue"],
        kb_id="kb1",
        filters={"period": "2026-07"},
        concept_catalog="",
    )
    assert queries == []


def test_compose_queries_july_2026_format():
    queries = compose_queries(
        metrics=["volume", "revenue"],
        kb_id="kb1",
        filters={"period": "July 2026"},
        concept_catalog=_CATALOG_SALES,
    )
    assert len(queries) == 1
    assert "2026-07-01" in queries[0].sql
    assert "2026-08-01" in queries[0].sql
