"""Catalog meaning reaches every describe path — not just the schema linker.

Defect: the connector can only report name/type/nullable/default/pk, so any
describe path that skipped the schema linker handed the model NAMES ONLY. That
is how look-alike columns on one table (FCLOSESTATUS vs FDOCUMENTSTATUS) get
chosen on name plausibility — the live "same question, two different columns"
failure. The meaning lives in kb_column_meta; these tests pin that it is merged
into the column dicts the agent actually reads.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta
from app.services.db import schema_service as ss
from app.services.db.schema_service import SchemaService

KB = "kb-merge"
TABLE = "erp_t_crm_contract"
OTHER_TABLE = "erp_t_sal_order"


@pytest.fixture()
def db():
    """Only the two catalog tables — no other DDL needed for the merge."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    KBTableMeta.__table__.create(engine)
    KBColumnMeta.__table__.create(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _seed(db, *, kb=KB, table=TABLE, column="FDOCUMENTSTATUS", **fields):
    meta = KBTableMeta(id=f"m-{table}", kb_id=kb, table_name=table, schema_name="")
    db.add(meta)
    db.flush()
    col = KBColumnMeta(
        table_meta_id=meta.id, column_name=column, ordinal=0, data_type="varchar",
        **fields,
    )
    db.add(col)
    db.commit()
    return col


def _merge(db, columns, table=TABLE, kb=KB):
    tables = [{"table": table, "columns": [{"name": c, "type": "varchar"} for c in columns]}]
    annotated = SchemaService(db)._merge_catalog_meaning(kb, tables)
    return annotated, tables[0]["columns"]


def test_description_and_semantics_are_attached(db):
    _seed(
        db,
        description_zh="单据状态",
        description_en="Document status",
        semantic_type="dimension",
        enum_values=["C", "D"],
    )
    annotated, columns = _merge(db, ["FDOCUMENTSTATUS", "FID"])
    assert annotated == 1
    assert columns[0]["description"] == "单据状态"
    assert columns[0]["semantic_type"] == "dimension"
    assert columns[0]["enum_values"] == ["C", "D"]
    # The un-catalogued column is left exactly as the connector reported it.
    assert set(columns[1]) == {"name", "type"}


def test_column_match_is_case_insensitive(db):
    _seed(db, column="FDOCUMENTSTATUS", description_zh="单据状态")
    annotated, columns = _merge(db, ["fdocumentstatus"])
    assert annotated == 1
    assert columns[0]["description"] == "单据状态"


def test_english_description_is_used_when_there_is_no_chinese_one(db):
    _seed(db, description_en="Document status")
    _annotated, columns = _merge(db, ["FDOCUMENTSTATUS"])
    assert columns[0]["description"] == "Document status"
    assert columns[0]["description_en"] == "Document status"


def test_columns_of_other_tables_and_other_kbs_are_not_leaked(db):
    _seed(db, description_zh="单据状态")
    _seed(db, kb="kb-other", table=OTHER_TABLE, column="FDOCUMENTSTATUS",
          description_zh="不应出现")
    annotated, columns = _merge(db, ["FDOCUMENTSTATUS"], table=OTHER_TABLE)
    assert annotated == 0
    assert "description" not in columns[0]


def test_no_catalog_rows_is_a_no_op(db):
    annotated, columns = _merge(db, ["FDOCUMENTSTATUS"])
    assert annotated == 0
    assert columns[0] == {"name": "FDOCUMENTSTATUS", "type": "varchar"}


def test_merge_never_raises_on_a_broken_session():
    class _Boom:
        def query(self, *a, **k):
            raise RuntimeError("catalog table missing")

    assert SchemaService(_Boom())._merge_catalog_meaning(KB, [{"table": TABLE, "columns": []}]) == 0


def test_enum_values_alone_still_counts_as_annotated(db):
    _seed(db, enum_values=["OPEN", "CLOSED"])
    annotated, columns = _merge(db, ["FDOCUMENTSTATUS"])
    assert annotated == 1
    assert columns[0]["enum_values"] == ["OPEN", "CLOSED"]


def test_the_plain_describe_path_returns_the_meaning(db, monkeypatch):
    """End-to-end through describe_table: the merge is wired, not just callable."""
    # Unique table name → no cross-test hit in the module-level schema cache,
    # and the seeded catalog row must name the same table we probe.
    probe_table = "contract_merge_probe"
    _seed(db, table=probe_table, description_zh="单据状态", semantic_type="dimension",
          enum_values=["C", "D"])

    class _FakeConnector:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def describe_table(self, table):
            return [
                {"name": "FDOCUMENTSTATUS", "type": "varchar", "nullable": False,
                 "default": None, "pk": False},
                {"name": "FID", "type": "varchar", "nullable": False,
                 "default": None, "pk": True},
            ]

    monkeypatch.setattr(ss, "get_connector", lambda kb: _FakeConnector())
    monkeypatch.setattr(
        ss.SchemaService, "_load_kb",
        lambda self, kb_id: SimpleNamespace(
            id=kb_id, name="KB", db_type="mysql", database_name="db",
        ),
    )
    # Unique table name → no cross-test cache hit in the module-level cache.
    out = SchemaService(db).describe_table(KB, "contract_merge_probe")
    cols = {c["name"]: c for c in out["columns"]}
    assert cols["FDOCUMENTSTATUS"]["description"] == "单据状态"
    assert cols["FDOCUMENTSTATUS"]["semantic_type"] == "dimension"
    assert "description" not in cols["FID"]
