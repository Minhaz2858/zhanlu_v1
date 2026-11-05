"""2C acceptance — reviewer P0 #1: no hardcoded database/table/customer
assumptions in the shared pipeline.

The four synthetic schemas were built with names the pipeline has NEVER
seen (txn_hdr, sku_ledger, dispense_log, formulary_item, batch_trace,
journal_line, account_map, ...). If ANY of those names — or any customer
identifier — appears as a literal in the discovery/retrieval/enforcement/
dialect code, the pipeline is teaching itself a specific database and this
test fails.

Generic STRUCTURAL vocabulary (date/amount/region/name-token regexes) is
deliberately NOT flagged here: those are engine-agnostic heuristics, and
they are separately documented in the 2C evidence report. What must never
appear is a specific table, schema, or customer name.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

# The shared pipeline modules that decide WHAT the customer's database
# means. None of them may hardcode a customer database.
PIPELINE_MODULES = [
    "app/services/db/table_retrieval.py",
    "app/services/db/schema_classification.py",
    "app/services/db/execution_gate.py",
    "app/services/db/result_contract.py",
    "app/services/db/query_contract.py",
    "app/services/db/connector_factory.py",
    "app/services/db/dialect.py",
    "app/services/db/base.py",
    "app/services/db/sqlite.py",
    "app/services/db/mysql.py",
    "app/services/db/postgres.py",
    "app/services/db/mssql.py",
    "app/services/db/oracle.py",
    "app/services/knowledge_graph/column_stats.py",
    "app/services/knowledge_graph/catalog_indexer.py",
    "app/services/knowledge_graph/join_edge_detector.py",
    "app/services/knowledge_graph/schema_linker.py",
]

# Customer-schema identifiers from the 2C fixtures — the pipeline must not
# know ANY of them. (Overlapping substrings like "amount_dr" must not leak
# either; these are word-bounded.)
FORBIDDEN_TABLE_NAMES = [
    "txn_hdr", "sku_ledger", "cust_ref", "formulary_item", "ward_stock",
    "dispense_log", "component_lot", "batch_trace", "workcell_event",
    "journal_line", "account_map", "settlement_ref", "meta_sync_state",
    "north_sku_ledger", "south_sku_ledger",
    # 2B-era fixtures too: no stage may teach the pipeline a fixture.
    "all_customers_rollup", "orders_north", "north_orders",
]

# Customer-specific SQL rules / ERP vocabulary: none of these business
# words may appear as identifiers or string literals in the pipeline.
FORBIDDEN_BUSINESS_LITERALS = [
    "erp_", "zhanlu_demo", "aipdp_", "ecisco", "lz_price",
    "sales_order_item", "sale_orderentry", "contract_execution",
]


def _code_text(path: Path) -> str:
    """The module's EXECUTABLE text: identifiers + non-docstring string
    literals. Docstrings are documentation (they may narrate historical
    examples); what must be customer-free is the CODE."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))

    def _docstring_nodes(node):
        body = getattr(node, "body", None)
        if not body:
            return []
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(
            first.value, ast.Constant
        ) and isinstance(first.value.value, str):
            return [first, first.value]
        return []

    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            docstrings.update(id(n) for n in _docstring_nodes(node))

    parts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                parts.append(node.value)
        elif isinstance(node, ast.Name):
            parts.append(node.id)
        elif isinstance(node, ast.Attribute):
            parts.append(node.attr)
    return "\n".join(parts)


def test_pipeline_modules_contain_no_customer_schema_names():
    root = Path(_BACKEND_ROOT)
    for rel in PIPELINE_MODULES:
        src = _code_text(root / rel)
        for name in FORBIDDEN_TABLE_NAMES:
            assert not re.search(rf"\b{re.escape(name)}\b", src, re.I), (
                f"{rel}: hardcoded customer-schema name {name!r} — the "
                "pipeline is teaching itself a specific database"
            )
        for lit in FORBIDDEN_BUSINESS_LITERALS:
            assert lit.lower() not in src.lower(), (
                f"{rel}: hardcoded business/ERP literal {lit!r}"
            )


def test_scope_verdicts_carry_no_vocabulary():
    """The scope machinery reaches the SAME structural verdicts for names
    it has never seen — evidence comes from fingerprints, decomposition,
    and observed values, never vocabulary."""
    from app.services.db.schema_classification import infer_scope

    def _cols(names):
        return [{"column_name": n, "data_type": "TEXT"} for n in names]

    tables = [
        {"table_name": "alpha_widget", "columns": _cols(
            ["id", "wcode", "scope_dim", "qty"])},
        {"table_name": "beta_widget", "columns": _cols(
            ["id", "wcode", "scope_dim", "qty"])},
        {"table_name": "widget", "columns": _cols(
            ["id", "wcode", "scope_dim", "qty", "src_scope", "built_on",
             "level"])},
    ]
    stats = {
        "alpha_widget": {"scope_dim": {"values": ["alpha"]}},
        "beta_widget": {"scope_dim": {"values": ["beta"]}},
    }
    verdicts = infer_scope(tables, column_stats=stats)
    by_name = dict(verdicts)
    # The rollup sibling (name decomposes the members) is global.
    assert by_name["widget"].level == "global", (
        f"rollup verdict: {by_name['widget']!r}"
    )
    for t in ("alpha_widget", "beta_widget"):
        v = by_name[t]
        assert v.level in ("partitioned", "global") and (
            v.level != "global"
        ), f"{t}: {v!r}"
        assert v.level == "partitioned", (
            f"{t}: expected a structural partition verdict, got {v.level!r}"
        )
