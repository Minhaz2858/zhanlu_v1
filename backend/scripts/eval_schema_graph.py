"""Schema-Aware Multi-Table Analysis — offline eval harness.

Evaluates the two deterministic components of the schema-aware capability
without an LLM in the loop:

1. **Join-edge detection** (`join_edge_detector.detect_join_edges`) against
   three synthetic schemas — Kingdee/ERP abbreviations, SaaS snake_case, and
   German naming — scoring precision/recall vs hand-written gold edges.
2. **Structural SQL validation** (`schema_validator.validate_against_schema`)
   over a question/SQL set, scoring hallucinated-column detection.

An optional live warehouse probe (`--live`) is best-effort and skips cleanly
when the DB is unreachable.

Usage:
    backend/venv/bin/python backend/scripts/eval_schema_graph.py [--live]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Allow running as a standalone script from any CWD.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.knowledge_graph.join_edge_detector import detect_join_edges


# ---------------------------------------------------------------------------
# Synthetic schemas
# ---------------------------------------------------------------------------
# Each table: table_name, columns[column_name, data_type, value_samples],
# foreign_keys[{column, ref_table, ref_column}].

ERP_SCHEMA = [
    {
        "table_name": "erp_product_sales_details",
        "columns": [
            {"column_name": "FMATERIALID", "data_type": "int",
             "value_samples": [1001, 1002, 1003, 1004, 1005]},
            {"column_name": "FNAME", "data_type": "varchar",
             "value_samples": ["c5", "c9", "ethylene"]},
            {"column_name": "FSUPPLIERID", "data_type": "int",
             "value_samples": [501, 502, 503, 504]},
            {"column_name": "shipment_quantity", "data_type": "decimal", "value_samples": []},
        ],
        "foreign_keys": [
            {"column": "FSUPPLIERID", "ref_table": "erp_partner", "ref_column": "FSUPPLIERID"}
        ],
    },
    {
        "table_name": "erp_partner",
        "columns": [
            {"column_name": "FSUPPLIERID", "data_type": "int",
             "value_samples": [501, 502, 503, 504, 505, 506]},
            {"column_name": "FNAME", "data_type": "varchar",
             "value_samples": ["Sinopec", "CNPC"]},
        ],
        "foreign_keys": [],
    },
    {
        "table_name": "erp_product",
        "columns": [
            {"column_name": "FMATERIALID", "data_type": "int",
             "value_samples": [1001, 1002, 1003, 1004, 1005, 1006]},
            {"column_name": "FNAME", "data_type": "varchar",
             "value_samples": ["c5", "c9"]},
        ],
        "foreign_keys": [],
    },
]

# Gold edges: (table_a, table_b) -> expected kind.
ERP_GOLD = {
    ("erp_partner", "erp_product_sales_details"): "FK",
    ("erp_product", "erp_product_sales_details"): "VALUE_OVERLAP",
}

SAAS_SCHEMA = [
    {
        "table_name": "orders",
        "columns": [
            {"column_name": "id", "data_type": "int",
             "value_samples": [1, 2, 3, 4, 5]},
            {"column_name": "user_id", "data_type": "int",
             "value_samples": [101, 102, 103, 104, 105]},
            {"column_name": "status", "data_type": "varchar",
             "value_samples": ["paid", "open"]},
        ],
        "foreign_keys": [
            {"column": "user_id", "ref_table": "users", "ref_column": "id"}
        ],
    },
    {
        "table_name": "users",
        "columns": [
            {"column_name": "id", "data_type": "int",
             "value_samples": [101, 102, 103, 104, 105, 106]},
            {"column_name": "email", "data_type": "varchar",
             "value_samples": ["a@x.com", "b@x.com"]},
        ],
        "foreign_keys": [],
    },
    {
        "table_name": "line_items",
        "columns": [
            {"column_name": "id", "data_type": "int",
             "value_samples": [9001, 9002, 9003]},
            {"column_name": "order_id", "data_type": "int",
             "value_samples": [1, 2, 3, 4, 5]},
            {"column_name": "product_id", "data_type": "int",
             "value_samples": [7001, 7002, 7003, 7004, 7005]},
        ],
        "foreign_keys": [
            {"column": "order_id", "ref_table": "orders", "ref_column": "id"}
        ],
    },
    {
        "table_name": "products",
        "columns": [
            {"column_name": "id", "data_type": "int",
             "value_samples": [7001, 7002, 7003, 7004, 7005, 7006]},
            {"column_name": "sku", "data_type": "varchar",
             "value_samples": ["SKU-A", "SKU-B"]},
        ],
        "foreign_keys": [],
    },
]

SAAS_GOLD = {
    ("orders", "users"): "FK",
    ("line_items", "orders"): "FK",
    ("line_items", "products"): "VALUE_OVERLAP",
}

GERMAN_SCHEMA = [
    {
        "table_name": "bestellungen",
        "columns": [
            {"column_name": "bestellung_id", "data_type": "int",
             "value_samples": [11, 12, 13, 14, 15]},
            {"column_name": "kunde_id", "data_type": "int",
             "value_samples": [201, 202, 203, 204, 205]},
            {"column_name": "artikel_id", "data_type": "int",
             "value_samples": [301, 302, 303, 304, 305]},
        ],
        "foreign_keys": [
            {"column": "kunde_id", "ref_table": "kunden", "ref_column": "kunde_id"}
        ],
    },
    {
        "table_name": "kunden",
        "columns": [
            {"column_name": "kunde_id", "data_type": "int",
             "value_samples": [201, 202, 203, 204, 205, 206]},
            {"column_name": "kunde_name", "data_type": "varchar",
             "value_samples": ["Mueller GmbH", "Schmidt AG"]},
        ],
        "foreign_keys": [],
    },
    {
        "table_name": "artikel",
        "columns": [
            {"column_name": "artikel_id", "data_type": "int",
             "value_samples": [301, 302, 303, 304, 305, 306]},
            {"column_name": "artikel_name", "data_type": "varchar",
             "value_samples": ["Stahl", "Kupfer"]},
        ],
        "foreign_keys": [],
    },
]

GERMAN_GOLD = {
    ("bestellungen", "kunden"): "FK",
    ("artikel", "bestellungen"): "VALUE_OVERLAP",
}


# ---------------------------------------------------------------------------
# Validator eval fixtures: (sql, tables, expect_valid, hallucinated_col)
# ---------------------------------------------------------------------------

VALIDATOR_CASES = [
    {
        "sql": "SELECT FNAME, shipment_quantity FROM erp_product_sales_details",
        "tables": {
            "erp_product_sales_details": ["FMATERIALID", "FNAME", "shipment_quantity"],
        },
        "expect_valid": True,
        "hallucinated": None,
    },
    {
        "sql": "SELECT material_name FROM erp_product_sales_details",
        "tables": {
            "erp_product_sales_details": ["FMATERIALID", "material_id", "FNAME"],
        },
        "expect_valid": False,
        "hallucinated": "material_name",
    },
    {
        "sql": "SELECT o.amount FROM orders o",
        "tables": {"orders": ["id", "amount"]},
        "expect_valid": True,
        "hallucinated": None,
    },
    {
        "sql": "SELECT nonexistent_col FROM orders",
        "tables": {"orders": ["id", "amount"]},
        "expect_valid": False,
        "hallucinated": "nonexistent_col",
    },
    {
        "sql": "SELECT * FROM ghost_table",
        "tables": {"orders": ["id"]},
        "expect_valid": False,
        "hallucinated": "ghost_table",
        "table_hallucination": True,
    },
]


def _canon(t1, t2):
    return (t1, t2) if t1 <= t2 else (t2, t1)


def eval_edge_detection(schema, gold):
    edges = detect_join_edges(schema)
    got = {}
    for e in edges:
        pair = _canon(e["source_table"], e["target_table"])
        # A declared FK pair is skipped by the detector (persisted separately),
        # so re-derive the FK label from the schema's declared FKs.
        got[pair] = e

    # Reconstruct declared-FK pairs so gold "FK" edges are accounted for even
    # though detect_join_edges intentionally omits them.
    declared = set()
    for t in schema:
        for fk in t.get("foreign_keys", []):
            declared.add(_canon(t["table_name"], fk["ref_table"]))

    tp = fn = fp = 0
    detail = []
    for pair, expected_kind in gold.items():
        found = pair in got or pair in declared
        kind = "FK" if pair in declared else got.get(pair, {}).get("kind", "?")
        if found and expected_kind == kind:
            tp += 1
            detail.append(f"  OK  {pair[0]} <-> {pair[1]} ({kind})")
        elif found:
            fn += 1
            detail.append(f"  WRONG-KIND {pair[0]} <-> {pair[1]} expected={expected_kind} got={kind}")
        else:
            fn += 1
            detail.append(f"  MISS {pair[0]} <-> {pair[1]} expected={expected_kind}")
    # False positives: inferred edges not in gold.
    for pair, e in got.items():
        if pair not in gold and pair not in declared:
            fp += 1
            detail.append(f"  FP   {pair[0]} <-> {pair[1]} ({e['kind']})")

    return {"tp": tp, "fn": fn, "fp": fp, "detail": detail}


def eval_validator(cases):
    correct = 0
    total = len(cases)
    detail = []
    for c in cases:
        db = MagicMock()
        kb = MagicMock()
        kb.id = "kb1"
        kb.db_type = "mysql"
        db.query.return_value.filter.return_value.first.return_value = kb

        svc = MagicMock()

        def _desc(kb_id, table, _tables=c["tables"]):
            cols = _tables.get(table, [])
            return {"columns": [{"name": n} for n in cols]}

        svc.describe_table.side_effect = _desc
        with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
            from app.services.nl2sql.schema_validator import validate_against_schema
            res = validate_against_schema(c["sql"], "kb1", db)

        ok = res["is_valid"] == c["expect_valid"]
        # For COLUMN hallucinations (not unknown tables), verify that
        # available_columns surfaces the real column list so the LLM can
        # self-correct. Unknown tables correctly yield no available columns.
        if (
            c["expect_valid"] is False
            and res["is_valid"] is False
            and not c.get("table_hallucination")
        ):
            avail = res.get("available_columns", {})
            real_cols = {n for cols in c["tables"].values() for n in cols}
            surfaced = set().union(*[set(v) for v in avail.values()])
            if not (real_cols & surfaced):
                ok = False
        if ok:
            correct += 1
        detail.append(
            f"  {'OK ' if ok else 'FAIL'} valid={res['is_valid']} sql={c['sql']!r}"
        )
    return {"correct": correct, "total": total, "detail": detail}


def _live_warehouse():
    """Best-effort live probe — skipped if the app DB/warehouse is unreachable."""
    try:
        from app.db.session import SessionLocal
        from app.models.knowledge_base import KnowledgeBase
        from app.services.db.schema_service import SchemaService

        with SessionLocal() as db:
            kb = db.query(KnowledgeBase).filter(
                KnowledgeBase.source_kind == "database"
            ).first()
            if not kb:
                print("  [live] no database KB found — skipped")
                return None
            svc = SchemaService(db)
            tables = svc.list_tables(kb.id)
            print(f"  [live] KB={kb.name} tables={len(tables)}")
            return {"kb": kb.name, "tables": len(tables)}
    except Exception as exc:
        print(f"  [live] unavailable: {exc}")
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="probe live warehouse KB")
    args = parser.parse_args()

    print("=" * 70)
    print("Schema-Aware Multi-Table Analysis — offline eval")
    print("=" * 70)

    print("\n[1] Join-edge detection (precision/recall vs gold edges)")
    for name, schema, gold in [
        ("ERP/Kingdee", ERP_SCHEMA, ERP_GOLD),
        ("SaaS snake_case", SAAS_SCHEMA, SAAS_GOLD),
        ("German naming", GERMAN_SCHEMA, GERMAN_GOLD),
    ]:
        r = eval_edge_detection(schema, gold)
        precision = r["tp"] / (r["tp"] + r["fp"]) if (r["tp"] + r["fp"]) else 0.0
        recall = r["tp"] / (r["tp"] + r["fn"]) if (r["tp"] + r["fn"]) else 0.0
        print(f"\n  {name}: tp={r['tp']} fn={r['fn']} fp={r['fp']} "
              f"precision={precision:.2f} recall={recall:.2f}")
        for line in r["detail"]:
            print(line)

    print("\n[2] Structural SQL validation (hallucinated-column detection)")
    r = eval_validator(VALIDATOR_CASES)
    rate = r["correct"] / r["total"]
    print(f"\n  accuracy={r['correct']}/{r['total']} = {rate:.2f}")
    for line in r["detail"]:
        print(line)

    if args.live:
        print("\n[3] Live warehouse probe")
        _live_warehouse()

    print("\n" + "=" * 70)
    print("Done. (Tool-iteration count vs baseline requires the LLM agent loop")
    print("and is measured separately via the live smoke test.)")
    print("=" * 70)


if __name__ == "__main__":
    main()
