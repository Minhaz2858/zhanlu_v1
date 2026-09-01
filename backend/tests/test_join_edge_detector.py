"""Tests for join_edge_detector — index-time join edge inference."""

from app.services.knowledge_graph.join_edge_detector import (
    STRUCTURAL_NOISE_COLUMNS,
    detect_join_edges,
    type_bucket,
)


def _tbl(name, columns, foreign_keys=None):
    return {
        "table_name": name,
        "columns": columns,
        "foreign_keys": foreign_keys or [],
    }


def _col(name, data_type, samples=None):
    c = {"column_name": name, "data_type": data_type, "is_primary_key": False}
    if samples is not None:
        c["value_samples"] = samples
    return c


def _find(edges, t1, t2):
    for e in edges:
        if {e["source_table"], e["target_table"]} == {t1, t2}:
            return e
    return None


# ── type_bucket ────────────────────────────────────────────────────────

def test_type_bucket_classifies_joinable_families():
    assert type_bucket("int") == "int"
    assert type_bucket("bigint") == "int"
    assert type_bucket("varchar(255)") == "varchar"
    assert type_bucket("char(8)") == "varchar"
    assert type_bucket("decimal(10,2)") is None
    assert type_bucket("date") is None
    assert type_bucket("float") is None
    assert type_bucket("text") is None
    assert type_bucket("blob") is None
    assert type_bucket(None) is None


# ── VALUE_OVERLAP: Kingdee-style (no declared FK) ──────────────────────

def test_value_overlap_kingdee_abbreviations_no_fk():
    # FMATERIALID (child) vs FMATERIALID (parent) — same opaque column name,
    # containment detected purely from value overlap.
    sales = _tbl("erp_product_sales_details", [
        _col("FMATERIALID", "int", ["1001", "1002", "1003", "1004"]),
        _col("shipment_quantity", "decimal(18,2)"),
    ])
    product = _tbl("erp_product", [
        _col("FMATERIALID", "int", ["1001", "1002", "1003", "1004", "1005", "1006"]),
        _col("FNAME", "varchar(255)"),
    ])
    edges = detect_join_edges([sales, product])
    e = _find(edges, "erp_product_sales_details", "erp_product")
    assert e is not None
    assert e["kind"] == "VALUE_OVERLAP"
    assert e["confidence"] == 1.0
    assert e["source_columns"] == ["FMATERIALID"]
    assert e["target_columns"] == ["FMATERIALID"]


def test_value_overlap_different_column_names_saas():
    # users.id <-> orders.user_id: no shared name, pure value containment.
    users = _tbl("users", [
        _col("id", "int", ["1", "2", "3", "4", "5"]),
        _col("email", "varchar(255)"),
    ])
    orders = _tbl("orders", [
        _col("user_id", "int", ["2", "3", "4"]),
        _col("total", "decimal(10,2)"),
    ])
    edges = detect_join_edges([users, orders])
    e = _find(edges, "users", "orders")
    assert e is not None
    assert e["kind"] == "VALUE_OVERLAP"
    assert e["confidence"] == 1.0  # {2,3,4} fully contained in users.id
    # Canonical alphabetical direction: "orders" < "users".
    assert e["source_table"] == "orders"
    assert e["target_table"] == "users"
    assert e["source_columns"] == ["user_id"]
    assert e["target_columns"] == ["id"]


def test_value_overlap_german_column_names():
    # bestellungen.kunde_id <-> kunden.id — opaque German names, no FK.
    kunden = _tbl("kunden", [
        _col("id", "int", ["10", "20", "30", "40"]),
    ])
    bestellungen = _tbl("bestellungen", [
        _col("kunde_id", "int", ["10", "20", "30"]),
        _col("betrag", "decimal(10,2)"),
    ])
    edges = detect_join_edges([kunden, bestellungen])
    e = _find(edges, "kunden", "bestellungen")
    assert e is not None
    assert e["kind"] == "VALUE_OVERLAP"


# ── NAME_MATCH ─────────────────────────────────────────────────────────

def test_name_match_excludes_structural_noise_columns():
    # Both tables share id/status/created_at — must NOT connect via name match.
    a = _tbl("table_a", [
        _col("id", "int", ["1", "2"]),
        _col("status", "varchar(16)"),
        _col("created_at", "date"),
        _col("customer_id", "int"),
    ])
    b = _tbl("table_b", [
        _col("id", "int", ["3", "4"]),
        _col("status", "varchar(16)"),
        _col("created_at", "date"),
        _col("customer_id", "int"),
    ])
    edges = detect_join_edges([a, b])
    # Only customer_id (non-noise) may produce a name-match edge.
    for e in edges:
        assert "id" not in (e["source_columns"] + e["target_columns"])
        assert "status" not in (e["source_columns"] + e["target_columns"])
    e = _find(edges, "table_a", "table_b")
    assert e is not None
    assert e["kind"] == "NAME_MATCH"
    assert e["source_columns"] == ["customer_id"]
    assert e["target_columns"] == ["customer_id"]
    assert e["confidence"] == 0.5


def test_name_match_requires_type_compatibility():
    a = _tbl("a", [_col("code", "int", ["1", "2"])])
    b = _tbl("b", [_col("code", "varchar(16)", ["x", "y"])])
    edges = detect_join_edges([a, b])
    assert _find(edges, "a", "b") is None


def test_noise_stoplist_contains_structural_columns():
    for noise in ("id", "created_at", "updated_at", "status", "name",
                  "type", "remark", "created_by", "is_deleted", "org_id",
                  "tenant_id"):
        assert noise in STRUCTURAL_NOISE_COLUMNS


# ── Type mismatch rejection ───────────────────────────────────────────

def test_value_overlap_rejects_type_mismatch():
    # int values overlapping varchar values must NOT join.
    a = _tbl("a", [_col("code", "int", ["1", "2", "3", "4"])])
    b = _tbl("b", [_col("other_code", "varchar(16)", ["1", "2", "3", "4"])])
    edges = detect_join_edges([a, b])
    assert _find(edges, "a", "b") is None


# ── FK pair skip ───────────────────────────────────────────────────────

def test_declared_fk_pair_is_skipped():
    sales = _tbl("erp_sales", [
        _col("FMATERIALID", "int", ["1001", "1002", "1003"]),
    ], foreign_keys=[{"column": "FMATERIALID", "ref_table": "erp_product",
                      "ref_column": "FMATERIALID"}])
    product = _tbl("erp_product", [
        _col("FMATERIALID", "int", ["1001", "1002", "1003", "1004"]),
    ])
    edges = detect_join_edges([sales, product])
    assert _find(edges, "erp_sales", "erp_product") is None


# ── Ranking / ordering ─────────────────────────────────────────────────

def test_value_overlap_outranks_name_match():
    # Same-named non-noise column AND overlapping values -> value_overlap wins
    # only if its containment exceeds name-match 0.5 (full containment = 1.0).
    a = _tbl("a", [_col("customer_id", "int", ["1", "2", "3"])])
    b = _tbl("b", [_col("customer_id", "int", ["1", "2", "3", "4", "5"])])
    edges = detect_join_edges([a, b])
    e = _find(edges, "a", "b")
    assert e is not None
    assert e["kind"] == "VALUE_OVERLAP"
    assert e["confidence"] == 1.0


def test_output_sorted_by_confidence_desc():
    a = _tbl("t1", [_col("x_id", "int", ["1", "2", "3"])])
    b = _tbl("t2", [_col("x_id", "int", ["1", "2", "3", "4", "5"])])
    c = _tbl("t3", [_col("x_id", "int", ["3", "4", "5", "6", "7", "8"])])
    edges = detect_join_edges([a, b, c])
    confs = [e["confidence"] for e in edges]
    assert confs == sorted(confs, reverse=True)


def test_below_min_shared_no_edge():
    # Different column names (no name-match interference); only 2 shared
    # values < default min_shared=3 -> no VALUE_OVERLAP edge.
    a = _tbl("a", [_col("code_a", "int", ["1", "2", "100", "101"])])
    b = _tbl("b", [_col("code_b", "int", ["1", "2", "200", "201"])])
    edges = detect_join_edges([a, b])
    assert _find(edges, "a", "b") is None
