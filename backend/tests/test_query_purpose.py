"""Tests for query-purpose classification (app/services/query_purpose.py).

Deterministic, database-agnostic tagging of every query result:
  - probe      — shape/coverage probes (bare aggregates, LIMIT samples, metadata-only)
  - auxiliary  — reference lookups (entity masters / dimensions / bridges)
  - answer     — business data rows that may feed the deliverable + synthesis

Catalog role resolution is memoized per turn and uses kb_table_meta.table_role
plus per-project ProjectCatalogOverlay.table_role (overlay wins).
Fail-open: unknown table role → treated as answer (never blocks deliverables).
"""

from __future__ import annotations

import pytest

from app.services.query_purpose import (
    TableRoleResolver,
    classify_query_purpose,
)
from app.models.knowledge_catalog import KBTableMeta, ProjectCatalogOverlay


# ── pure classifier: shape-based probes ──────────────────────────────────


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT MIN(FDATE), MAX(FDATE) FROM erp_t_sal_outstock",
        "select max(shipment_date) from erp_t_sal_outstock",
        "SELECT COUNT(*) FROM erp_product_sales_details",
        "SELECT MIN(FDATE), MAX(FDATE) FROM t WHERE FType=1",
    ],
)
def test_bare_aggregate_is_probe(sql: str) -> None:
    assert classify_query_purpose(sql, [], {"erp_t_sal_outstock": "fact"}) == "probe"


def test_limit_sample_without_aggregation_is_probe() -> None:
    sql = "SELECT * FROM erp_product_sales_details LIMIT 5"
    assert classify_query_purpose(sql, [], {"erp_product_sales_details": "fact"}) == "probe"


def test_limit_after_where_is_answer() -> None:
    sql = (
        "SELECT shipment_date, shipment_quantity FROM erp_t_sal_outstock "
        "WHERE shipment_date >= '2026-07-01' LIMIT 100"
    )
    rows = [{"shipment_date": "2026-08-01", "shipment_quantity": 42}]
    assert classify_query_purpose(sql, rows, {"erp_t_sal_outstock": "fact"}) == "answer"


def test_metadata_only_rows_shape_is_probe() -> None:
    # Even though the table is a fact table, the RESULT is a metadata snapshot.
    rows = [{"max(shipment_date)": "2026-08-21", "min(shipment_date)": "2026-01-01"}]
    sql = "SELECT MIN(shipment_date), MAX(shipment_date) FROM erp_t_sal_outstock"
    assert classify_query_purpose(sql, rows, {"erp_t_sal_outstock": "fact"}) == "probe"


def test_empty_rows_are_not_answer() -> None:
    sql = "SELECT * FROM erp_t_sal_outstock WHERE shipment_date >= '2099-01-01'"
    assert classify_query_purpose(sql, [], {"erp_t_sal_outstock": "fact"}) == "probe"


# ── pure classifier: role-based ──────────────────────────────────────────


def test_fact_table_query_is_answer() -> None:
    sql = (
        "SELECT shipment_date, shipment_quantity FROM erp_t_sal_outstock "
        "WHERE shipment_date >= '2026-07-01'"
    )
    rows = [{"shipment_date": "2026-08-01", "shipment_quantity": 42}]
    assert classify_query_purpose(sql, rows, {"erp_t_sal_outstock": "fact"}) == "answer"


def test_entity_master_reference_lookup_is_auxiliary() -> None:
    # Reference/mapping lookups (e.g. _ref_material_mapping) are NOT the answer.
    sql = "SELECT FNAME, FMaterialID FROM _ref_material_mapping LIMIT 100"
    rows = [{"FNAME": "PVC", "FMaterialID": "M1"}]
    assert (
        classify_query_purpose(sql, rows, {"_ref_material_mapping": "entity_master"})
        == "auxiliary"
    )


def test_dimension_label_lookup_is_auxiliary() -> None:
    sql = "SELECT product_name FROM product_dim WHERE product_id = 7"
    rows = [{"product_name": "PVC Resin"}]
    assert classify_query_purpose(sql, rows, {"product_dim": "dimension"}) == "auxiliary"


def test_mixed_fact_and_dimension_is_answer() -> None:
    sql = (
        "SELECT p.product_name, s.shipment_quantity "
        "FROM erp_t_sal_outstock s JOIN product_dim p ON s.product_id = p.product_id "
        "WHERE s.shipment_date >= '2026-07-01'"
    )
    rows = [{"product_name": "PVC", "shipment_quantity": 10}]
    roles = {"erp_t_sal_outstock": "fact", "product_dim": "dimension"}
    assert classify_query_purpose(sql, rows, roles) == "answer"


def test_unknown_role_fails_open_to_answer() -> None:
    # Unknown / unindexed tables must never block a legitimate deliverable.
    sql = "SELECT * FROM legacy_snapshot WHERE date >= '2026-07-01'"
    rows = [{"date": "2026-08-01", "value": 1}]
    assert classify_query_purpose(sql, rows, {"legacy_snapshot": "unknown"}) == "answer"


def test_no_sql_fails_open_to_answer() -> None:
    rows = [{"shipment_date": "2026-08-01", "shipment_quantity": 42}]
    assert classify_query_purpose(None, rows, {}) == "answer"


def test_empty_roles_map_with_tables_fails_open() -> None:
    # No catalog info at all → answer (fail-open).
    sql = "SELECT * FROM erp_t_sal_outstock"
    rows = [{"shipment_date": "2026-08-01"}]
    assert classify_query_purpose(sql, rows, {}) == "answer"


# ── TableRoleResolver (memoized catalog role lookup) ─────────────────────


class _StubQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows


class _StubDB:
    def __init__(self, meta_rows, overlay_rows):
        self._meta_rows = meta_rows
        self._overlay_rows = overlay_rows

    def query(self, model):
        if model is KBTableMeta:
            return _StubQuery(self._meta_rows)
        return _StubQuery(self._overlay_rows)


def _meta(table_name: str, role: str) -> KBTableMeta:
    return KBTableMeta(kb_id="kb-1", table_name=table_name, table_role=role)


def test_resolver_returns_catalog_roles() -> None:
    db = _StubDB(
        meta_rows=[_meta("erp_t_sal_outstock", "fact"), _meta("product_dim", "dimension")],
        overlay_rows=[],
    )
    r = TableRoleResolver(db=db, kb_ids=["kb-1"], project_id="proj-1")
    roles = r.roles_for(["erp_t_sal_outstock", "product_dim", "ghost"])
    assert roles["erp_t_sal_outstock"] == "fact"
    assert roles["product_dim"] == "dimension"
    assert roles["ghost"] == "unknown"


def test_resolver_overlay_overrides_catalog_role() -> None:
    overlay = ProjectCatalogOverlay(
        project_id="proj-1", table_name="_ref_material_mapping", table_role="fact",
        scope="table_role",
    )
    db = _StubDB(
        meta_rows=[_meta("_ref_material_mapping", "entity_master")],
        overlay_rows=[overlay],
    )
    r = TableRoleResolver(db=db, kb_ids=["kb-1"], project_id="proj-1")
    assert r.roles_for(["_ref_material_mapping"])["_ref_material_mapping"] == "fact"


def test_resolver_without_project_ignores_overlays() -> None:
    overlay = ProjectCatalogOverlay(
        project_id="proj-1", table_name="_ref_material_mapping", table_role="fact",
        scope="table_role",
    )
    db = _StubDB(
        meta_rows=[_meta("_ref_material_mapping", "entity_master")],
        overlay_rows=[overlay],
    )
    r = TableRoleResolver(db=db, kb_ids=["kb-1"], project_id=None)
    assert r.roles_for(["_ref_material_mapping"])["_ref_material_mapping"] == "entity_master"


def test_resolver_unknown_table_default() -> None:
    db = _StubDB(meta_rows=[], overlay_rows=[])
    r = TableRoleResolver(db=db, kb_ids=["kb-1"], project_id="proj-1")
    assert r.roles_for(["does_not_exist"])["does_not_exist"] == "unknown"
