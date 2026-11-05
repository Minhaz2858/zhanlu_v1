"""2C Tier C — MSSQL/Oracle protocol-shape acceptance (Decision 1: parity
implementation DEFERRED; this stage only verifies current behavior).

Verified here, per the reviewer's decision:
  * ``table_facts`` returning ``{}`` means UNAVAILABLE metadata — never
    zero rows, zero cardinality, or zero bytes;
  * missing metadata is disclosed honestly (NULLs, "not collected");
  * discovery/classification/relation inference still run generically on
    engine-specific metadata shapes (a dialect-accurate MSSQL/Oracle double
    drives the REAL catalog indexer);
  * the engine's SQL dialect renders unfamiliar identifiers correctly
    (no server needed).

No MSSQL/Oracle server is contacted. The doubles implement the BaseConnector
protocol with engine-shaped metadata (nvarchar/NUMBER types, schema-qualified
names).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

import helpers_2c_schemas as fx  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models.knowledge_catalog import KBTableMeta, KBTableRelation  # noqa: E402


class _FakeMSSQLConn:
    """MSSQL-shaped result rows for one engine session."""

    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def execute(self, sql, params=None):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None


class FakeMSSQLConnector:
    """The BaseConnector protocol with MSSQL-shaped metadata.

    Deliberately does NOT implement ``table_facts`` (mirrors the real
    connector: parity deferred). Metadata shapes follow sys/information_
    schema conventions: schema-qualified names, nvarchar types.
    """

    dialect = "mssql"
    db_type = "mssql"

    def __init__(self, kb):
        self.kb = kb
        self._conn = None
        self._tables = {
            "dbo.txn_hdr": [
                {"name": "txn_id", "data_type": "int", "nullable": False,
                 "pk": True},
                {"name": "cust_no", "data_type": "nvarchar", "nullable": True},
                {"name": "grand_total", "data_type": "decimal",
                 "nullable": False},
            ],
            "dbo.cust_ref": [
                {"name": "cust_id", "data_type": "int", "nullable": False,
                 "pk": True},
                {"name": "cust_no", "data_type": "nvarchar", "nullable": False},
            ],
        }

    def __enter__(self):
        self._conn = _FakeMSSQLConn(self._tables)
        return self

    def __exit__(self, *exc):
        self._conn = None
        return False

    def list_tables(self):
        return [t.split(".")[-1] for t in self._tables]

    def describe_table(self, table: str):
        cols = []
        for t, cdefs in self._tables.items():
            if t.split(".")[-1] == table:
                for i, c in enumerate(cdefs):
                    cols.append({
                        "name": c.get("name"), "data_type": c.get("data_type"),
                        "nullable": c.get("nullable"), "default": None,
                        "pk": c.get("pk", False), "comment": None,
                        "ordinal": i,
                    })
        return cols

    def get_indexes(self, table: str):
        for t, cdefs in self._tables.items():
            if t.split(".")[-1] == table:
                pk = [c["name"] for c in cdefs if c["pk"]]
                if pk:
                    return [{
                        "name": f"PK__{table}", "columns": pk,
                        "unique": True, "primary": True,
                    }]
        return []

    def get_foreign_keys(self, table: str):
        if str(table).upper() == "TXN_HDR":
            return [{
                "column": "cust_no", "ref_schema": "dbo",
                "ref_table": "cust_ref", "ref_column": "cust_no",
            }]
        return []

    def shape(self):
        return [
            (t.split(".")[-1], c["name"])
            for t, cdefs in self._tables.items()
            for c in cdefs
        ]

    def test_connection(self):
        return {"ok": True, "info": "Fake protocol double"}

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        s = str(sql).lower()
        if "count(*)" in s:
            # Answer only the row-count probe; the double's tables are empty.
            return [{"n": 0}]
        return []


class FakeOracleConnector(FakeMSSQLConnector):
    """Oracle-shaped double: NUMBER/VARCHAR2 types, no table_facts.

    Column/table names mirror what a real Oracle driver reports (upper
    case) — the FK metadata names its target in the SAME case the source
    reports, as a real driver would.
    """

    dialect = "oracle"
    db_type = "oracle"

    def __init__(self, kb):
        super().__init__(kb)
        self._tables = {
            "CUSTREF.TXN_HDR": [
                {"name": "TXN_ID", "data_type": "NUMBER", "nullable": False,
                 "pk": True},
                {"name": "CUST_NO", "data_type": "VARCHAR2", "nullable": True},
                {"name": "GRAND_TOTAL", "data_type": "NUMBER",
                 "nullable": False},
            ],
            "CUSTREF.CUST_REF": [
                {"name": "CUST_ID", "data_type": "NUMBER", "nullable": False,
                 "pk": True},
                {"name": "CUST_NO", "data_type": "VARCHAR2",
                 "nullable": False},
            ],
        }

    def get_foreign_keys(self, table: str):
        if str(table).upper() == "TXN_HDR":
            return [{
                "column": "CUST_NO", "ref_schema": "CUSTREF",
                "ref_table": "CUST_REF", "ref_column": "CUST_NO",
            }]
        return []


def _patch_connector(monkeypatch, cls):
    import app.services.db.connector_factory as cf

    cf._ensure_registry()
    monkeypatch.setitem(cf._REGISTRY, cls.db_type, cls)
    # The lazy driver check would demand a real pymssql/cx_Oracle install —
    # the double makes that unnecessary. (The factory imports `ensure` from
    # tool_handlers.lazy_deps inside get_connector.)
    import app.services.tool_handlers.lazy_deps as lazy_deps

    monkeypatch.setattr(lazy_deps, "ensure", lambda feature: None)


def test_deferred_parity_table_facts_is_not_implemented():
    """Decision 1: the real connector classes do NOT implement
    ``table_facts`` — they inherit the protocol default (class-level
    check; no driver or server needed)."""
    from app.services.db.mssql import MSSQLConnector
    from app.services.db.oracle import OracleConnector

    # The protocol's table_facts is optional: the connectors do not define
    # it AT ALL (the indexer's _safe_* guard treats absence as unavailable).
    assert not hasattr(MSSQLConnector, "table_facts"), (
        "MSSQLConnector now implements table_facts — the deferred parity "
        "workstream has started; update this stage's evidence"
    )
    assert not hasattr(OracleConnector, "table_facts"), (
        "OracleConnector now implements table_facts — the deferred parity "
        "workstream has started; update this stage's evidence"
    )


@pytest.mark.parametrize("engine", ["mssql", "oracle"])
def test_discovery_over_deferred_parity_engine_is_honest(
    monkeypatch, engine,
):
    """Full REAL discovery over an engine whose facts are unavailable:
    catalog rows exist, exact counts measured, estimates stay NULL, and
    the FK relationship is still discovered from the engine's metadata."""
    import asyncio

    from app.models.knowledge_base import KnowledgeBase
    from app.services.knowledge_graph.catalog_indexer import index_kb_catalog
    from scripts.backfill_schema_classification import backfill

    cls = FakeMSSQLConnector if engine == "mssql" else FakeOracleConnector
    _patch_connector(monkeypatch, cls)
    fx.apply_discovery_flags(monkeypatch)

    db = SessionLocal()
    try:
        kb = KnowledgeBase(
            id=f"kb-2c-{engine}-{uuid.uuid4().hex[:8]}",
            name=f"2c_{engine}_protocol",
            org_id=f"org-2c-{uuid.uuid4().hex[:8]}",
            app_id="app-2c",
            type="database",
            source_kind="database",
            db_type=engine,
            database_name="fake",
            catalog_status="ready",
            nl2sql_state="nl2sql_ready",
        )
        db.add(kb)
        db.commit()

        asyncio.run(index_kb_catalog(kb, db))
        backfill(kb_id=kb.id)
        db.expire_all()

        metas = {
            m.table_name: m
            for m in db.query(KBTableMeta)
            .filter(KBTableMeta.kb_id == kb.id)
            .all()
        }
        assert {n.lower() for n in metas} == {"txn_hdr", "cust_ref"}, (
            f"{engine}: discovery did not index the engine's tables"
        )
        for name, m in metas.items():
            # Exact COUNT works on every engine — the authoritative fact.
            assert m.row_count == 0 and m.row_count_source == "exact", (
                f"{engine}.{name}: empty double tables must count 0 EXACTLY"
            )
            # UNAVAILABLE facts stay absent/NULL — never fabricated zeros.
            assert not hasattr(m, "est_rows") or m.est_rows is None, (
                f"{engine}.{name}: unavailable row estimate became a number"
            )
            assert m.size_bytes is None, (
                f"{engine}.{name}: unavailable size became a number"
            )
            assert m.source_comment is None
            assert not (m.description_en or "").strip()
            assert m.description_source == ""
        # The engine's OWN FK metadata drove relation discovery.
        rels = (
            db.query(KBTableRelation)
            .filter(KBTableRelation.kb_id == kb.id)
            .all()
        )
        fk = [r for r in rels if r.relation_type == "FK"]
        assert fk, (
            f"{engine}: the engine-reported FK was not persisted as a "
            "relation"
        )
        assert fk[0].source_cardinality == "many"
        assert fk[0].confidence == 1.0
        # Classification: business (the FK edge is positive evidence).
        assert all(m.schema_class == "business" for m in metas.values())
    finally:
        db.close()


@pytest.mark.parametrize(
    "engine,quoted",
    [
        ("mssql", "[txn_hdr]"),
        ("oracle", '"txn_hdr"'),
    ],
)
def test_dialect_quotes_unfamiliar_identifiers(engine, quoted):
    """Dialect-correct SQL rendering for a name the engine has never seen —
    no server needed, the profile is the contract."""
    from app.services.db.dialect import profile_for

    assert profile_for(engine).quote_ident("txn_hdr") == quoted
