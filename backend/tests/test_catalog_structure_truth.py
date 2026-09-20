"""P1 — onboarding must collect what the SOURCE says about itself.

Measured gap this suite pins down (audit of the pre-2026-09-20 indexer):

* MySQL ``COLUMN_COMMENT`` — the only place an ERP states what a column means —
  was never SELECTed (``mysql.py`` asked for NAME/TYPE/NULLABLE/DEFAULT/KEY).
* ``COLUMN_KEY`` returned PRI/UNI/MUL and only ``PRI`` survived, as a bool;
  unique and secondary indexes were read from the server and thrown away.
* PostgreSQL comments (``pg_description``) and index definitions (``pg_index``)
  were read nowhere.
* ``kb_table_meta.size_bytes`` was declared and never written.
* A row count that timed out left ``row_count`` NULL with no record of whether
  the number was exact or an engine estimate.
* ``MAX_TABLES`` truncation was a log line: a source with more objects than the
  cap could still finish as ``nl2sql_ready``, so the agent presented a partial
  catalog as the whole database.

What is pinned here: the source's own words and structure are collected and
stored separately from anything we infer; "not collected" is never stored as
zero, as a guess, or as the same thing as "the source declares none".
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

from app.services.db import readiness as rd
from app.services.knowledge_graph import catalog_indexer as cat


# ── fake engines (dialect SQL is asserted by the connectors themselves) ─────


class _FakeResult:
    def __init__(self, rows: list):
        self._rows = rows

    def fetchall(self) -> list:
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, engine: "_FakeEngine", responses: dict):
        self._engine = engine
        self._responses = responses

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, *exc) -> None:
        return None

    def execute(self, statement, params=None, **_kw) -> _FakeResult:
        sql = str(statement)
        self._engine.queries.append(sql)
        for marker, rows in self._responses.items():
            if marker in sql:
                return _FakeResult(rows)
        return _FakeResult([])


class _FakeEngine:
    """Returns canned rows per SQL marker — one per information_schema probe."""

    def __init__(self, responses: dict):
        self.responses = responses
        self.queries: list[str] = []

    def connect(self) -> _FakeConn:
        return _FakeConn(self, self.responses)


# ── 1. connector level: the source's own facts ─────────────────────────────


def test_mysql_reads_column_comments_and_index_roles():
    from app.services.db.mysql import MySQLConnector

    conn = MySQLConnector.__new__(MySQLConnector)
    conn._engine = _FakeEngine(
        {
            "information_schema.COLUMNS": [
                # name, type, nullable, default, COLUMN_KEY, COLUMN_COMMENT
                ("FID", "int", "NO", None, "PRI", ""),
                ("FQTY", "decimal(18,4)", "YES", None, "MUL", "数量"),
                ("FCODE", "varchar(50)", "YES", None, "UNI", "业务编码\n  第二行"),
                ("FNOTE", "text", "YES", None, "", "  "),
            ]
        }
    )
    cols = conn.describe_table("erp_t_demo")
    by_name = {c["name"]: c for c in cols}

    assert by_name["FID"]["comment"] is None, "empty comment is 'not declared'"
    assert by_name["FQTY"]["comment"] == "数量"
    # The connector returns the source's value verbatim (it is evidence) …
    assert by_name["FCODE"]["comment"] == "业务编码\n  第二行"
    # … and normalisation happens once, on the way into the catalog.
    assert cat._clean_comment(by_name["FCODE"]["comment"]) == "业务编码 第二行"
    assert by_name["FNOTE"]["comment"] is None, "whitespace-only comment is None"

    assert by_name["FID"]["key"] == "PRI"
    assert by_name["FQTY"]["key"] == "MUL", "non-unique index role was discarded before"
    assert by_name["FCODE"]["key"] == "UNI", "unique key role was discarded before"
    assert by_name["FNOTE"]["key"] == ""


def test_mysql_table_facts_and_index_inventory():
    from app.services.db.mysql import MySQLConnector

    conn = MySQLConnector.__new__(MySQLConnector)
    conn._engine = _FakeEngine(
        {
            "information_schema.TABLES": [
                ("BASE TABLE", "InnoDB", "订单主表", 14589, 1048576, 524288),
            ],
            "information_schema.STATISTICS": [
                ("PRIMARY", 0, 1, "FID"),
                ("idx_plan", 1, 1, "PLANDATE"),
                ("idx_plan", 1, 2, "FSTATUS"),
            ],
        }
    )

    facts = conn.table_facts("erp_t_demo")
    assert facts["comment"] == "订单主表"
    assert facts["engine"] == "InnoDB"
    assert facts["est_rows"] == 14589
    assert facts["size_bytes"] == 1048576 + 524288
    assert facts["table_type"] == "BASE TABLE"

    indexes = {i["name"]: i for i in conn.get_indexes("erp_t_demo")}
    assert indexes["PRIMARY"]["primary"] is True and indexes["PRIMARY"]["unique"] is True
    assert indexes["PRIMARY"]["columns"] == ["FID"]
    # composite index parts must arrive in declared order
    assert indexes["idx_plan"]["columns"] == ["PLANDATE", "FSTATUS"]
    assert indexes["idx_plan"]["unique"] is False


def test_mysql_view_comment_is_not_echoed_as_a_business_label():
    """MySQL stores the literal 'VIEW' in TABLE_COMMENT — that is not a comment."""
    from app.services.db.mysql import MySQLConnector

    conn = MySQLConnector.__new__(MySQLConnector)
    conn._engine = _FakeEngine(
        {"information_schema.TABLES": [("VIEW", None, "VIEW", None, None, None)]}
    )
    facts = conn.table_facts("erp_v_sale_orderentry")
    assert "comment" not in facts
    assert facts["table_type"] == "VIEW"
    assert "est_rows" not in facts, "a NULL row estimate must be absent, not 0"


def test_postgres_reads_comments_keys_and_index_definitions():
    from app.services.db.postgres import PostgresConnector

    conn = PostgresConnector.__new__(PostgresConnector)
    conn._schema = "public"
    conn._engine = _FakeEngine(
        {
            "SELECT column_name, data_type, is_nullable, column_default": [
                ("fid", "integer", "NO", None),
                ("qty", "numeric", "YES", None),
            ],
            "SELECT a.attname, i.indisprimary": [
                ("fid", True, True),
                ("qty", False, False),
            ],
            "pg_description": [
                ("fid", "主键"),
                ("qty", None),
            ],
            "obj_description": [
                ("r", 42, 8192, "订单明细"),
            ],
            "FROM pg_index ix": [
                ("erp_t_demo_pkey", True, True, "fid"),
                ("ix_demo_qty", False, False, "qty"),
            ],
        }
    )

    cols = {c["name"]: c for c in conn.describe_table("erp_t_demo")}
    assert cols["fid"]["comment"] == "主键"
    assert cols["qty"]["comment"] is None
    assert cols["fid"]["key"] == "PRI"
    assert cols["qty"]["key"] == "MUL"

    facts = conn.table_facts("erp_t_demo")
    assert facts == {"table_type": "BASE TABLE", "est_rows": 42, "size_bytes": 8192, "comment": "订单明细"}

    indexes = {i["name"]: i for i in conn.get_indexes("erp_t_demo")}
    assert indexes["erp_t_demo_pkey"]["primary"] is True
    assert indexes["ix_demo_qty"]["columns"] == ["qty"]

    # Alias guard (real regression: reading indisunique/indisprimary off the
    # index's pg_class row — alias `i` — fails the whole statement on a live
    # server, and the catalog then records "indexes not collected" for every
    # PostgreSQL source). A canned-rows fake cannot catch that, so the emitted
    # SQL is asserted directly.
    import re

    index_sql = [q for q in conn._engine.queries if "FROM pg_index ix" in q]
    assert index_sql, "the index inventory query must be issued"
    assert re.search(r"\bi\.indis(unique|primary)\b", index_sql[0]) is None, index_sql[0]
    assert "ix.indisprimary" in index_sql[0]


def test_sqlite_reports_no_comment_rather_than_inventing_one():
    """SQLite has no COMMENT syntax: None is the honest answer."""
    import sqlite3

    from app.services.db.sqlite import SQLiteConnector

    conn = SQLiteConnector.__new__(SQLiteConnector)
    conn._conn = sqlite3.connect(":memory:")
    conn._conn.execute(
        "CREATE TABLE demo (id INTEGER PRIMARY KEY, code TEXT UNIQUE, note TEXT)"
    )
    conn._conn.execute("CREATE INDEX ix_demo_note ON demo (note)")
    # A composite PK DOES get a real index (unlike the rowid alias above).
    conn._conn.execute("CREATE TABLE comp (a TEXT, b TEXT, PRIMARY KEY (a, b))")

    cols = {c["name"]: c for c in conn.describe_table("demo")}
    assert cols["id"]["comment"] is None
    # The PK fact comes from table_info: a rowid alias appears in no index_list.
    assert cols["id"]["key"] == "PRI"
    assert cols["code"]["key"] == "UNI"
    assert cols["note"]["key"] == "MUL"

    indexes = {i["name"]: i for i in conn.get_indexes("demo")}
    assert any(i["unique"] and not i["primary"] for i in indexes.values())
    # …and the inventory stays what the source declares: no synthesised PK row.
    assert not any(i["primary"] for i in indexes.values())

    comp = {i["name"]: i for i in conn.get_indexes("comp")}
    primary = [i for i in comp.values() if i["primary"]]
    assert primary and primary[0]["columns"] == ["a", "b"]

    assert conn.table_facts("demo")["table_type"] == "BASE TABLE"


# ── 2. degradation: "not collected" must never look like an answer ──────────


class _NoFacts:
    """A dialect that implements neither optional collector (mssql / oracle)."""

    def describe_table(self, table):
        return [{"name": "a", "type": "int", "nullable": True, "default": None, "pk": False}]

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        return []

    def get_foreign_keys(self, table):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


class _FactsButIndexesRaise(_NoFacts):
    def table_facts(self, table):
        return {"est_rows": 7}

    def get_indexes(self, table):
        raise RuntimeError("permission denied for pg_index")


def test_missing_collectors_degrade_to_not_collected(monkeypatch):
    monkeypatch.setattr(cat, "get_connector", lambda kb: _NoFacts())
    assert cat._safe_table_facts(_NoFacts(), "t") == {}
    assert cat._safe_indexes(_NoFacts(), "t") is None, (
        "a dialect that cannot answer must yield None, not an empty list"
    )


def test_probe_failure_is_not_collected_and_not_a_zero(monkeypatch):
    conn = _FactsButIndexesRaise()
    assert cat._safe_table_facts(conn, "t") == {"est_rows": 7}
    assert cat._safe_indexes(conn, "t") is None, "a failed probe is not 'no indexes'"


def _kb_row(**kw) -> KnowledgeBase:
    return KnowledgeBase(
        id=str(uuid.uuid4()),
        name="Warehouse",
        source_kind="database",
        db_type="mysql",
        status="active",
        catalog_status="ready",
        **kw,
    )


def test_estimated_row_count_is_labelled_as_such(monkeypatch):
    """COUNT(*) timed out → the engine estimate is used AND its provenance kept."""
    monkeypatch.setattr(cat, "get_connector", lambda kb: _FactsButIndexesRaise())
    monkeypatch.setattr(cat.settings, "CATALOG_JOIN_EDGES_ENABLED", False)
    monkeypatch.setattr(cat.settings, "KG_COVERAGE_PROBE_ENABLED", False)

    table = cat._introspect_one_table(_kb_row(), "t")
    assert table["row_count"] == 7
    assert table["row_count_source"] == "estimated"
    assert table["est_rows"] == 7
    assert table["indexes"] is None, "uncollected indexes stay None"


def test_exact_count_wins_and_is_labelled(monkeypatch):
    class _Exact(_FactsButIndexesRaise):
        def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
            return [{"c": 1234}]

    monkeypatch.setattr(cat, "get_connector", lambda kb: _Exact())
    monkeypatch.setattr(cat.settings, "CATALOG_JOIN_EDGES_ENABLED", False)
    monkeypatch.setattr(cat.settings, "KG_COVERAGE_PROBE_ENABLED", False)

    table = cat._introspect_one_table(_kb_row(), "t")
    assert table["row_count"] == 1234
    assert table["row_count_source"] == "exact"
    assert table["est_rows"] == 7, "the estimate is still reported separately"


# ── 3. persistence: source truth never merges into our inference ───────────


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        try:
            os.unlink(path)
        except OSError:
            pass


def _table_payload(**kw) -> dict:
    payload = {
        "table_name": "erp_t_demo",
        "schema_name": "",
        "table_type": "BASE TABLE",
        "row_count": 100,
        "row_count_source": "exact",
        "size_bytes": 4096,
        "engine": "InnoDB",
        "table_comment": "订单主表",
        "indexes": [{"name": "PRIMARY", "columns": ["FID"], "unique": True, "primary": True}],
        "columns": [
            {
                "column_name": "FQTY",
                "ordinal": 1,
                "data_type": "decimal(18,4)",
                "is_nullable": True,
                "is_primary_key": False,
                "default_value": None,
                "comment": "数量",
                "key": "MUL",
            }
        ],
        "foreign_keys": [],
    }
    payload.update(kw)
    return payload


def test_source_facts_are_persisted_and_kept_apart_from_llm_text(db):
    kb = _kb_row()
    db.add(kb)
    db.commit()

    cat._persist_catalog(db, kb.id, [_table_payload()])

    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).one()
    assert meta.source_comment == "订单主表"
    assert meta.size_bytes == 4096
    assert meta.row_count_source == "exact"
    assert meta.indexes_json[0]["primary"] is True
    # the description pass produced nothing, so the SOURCE's words are used…
    assert meta.description_zh == "订单主表"
    assert meta.description_source == "source_comment"

    col = db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id == meta.id).one()
    assert col.source_comment == "数量"
    assert col.key_role == "MUL"
    assert col.description_source == "source_comment"
    assert col.description_zh == "数量"


def test_llm_description_wins_and_is_labelled_as_such(db):
    kb = _kb_row()
    db.add(kb)
    db.commit()

    payload = _table_payload(description_zh="订单主表（LLM）", description_en="Orders")
    payload["columns"][0]["description_zh"] = "数量（LLM）"
    cat._persist_catalog(db, kb.id, [payload])

    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).one()
    assert meta.description_source == "llm"
    assert meta.description_zh == "订单主表（LLM）"
    assert meta.source_comment == "订单主表", "the raw comment is still kept for audit"

    col = db.query(KBColumnMeta).filter(KBColumnMeta.table_meta_id == meta.id).one()
    assert col.description_source == "llm"
    assert col.description_zh == "数量（LLM）"
    assert col.source_comment == "数量"


def test_uncollected_indexes_stay_null_and_a_real_empty_list_stays_empty(db):
    kb = _kb_row()
    db.add(kb)
    db.commit()

    cat._persist_catalog(db, kb.id, [_table_payload(indexes=None)])
    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == kb.id).one()
    assert meta.indexes_json is None, "not collected"

    cat._persist_catalog(db, kb.id, [_table_payload(indexes=[])])
    db.refresh(meta)
    assert meta.indexes_json == [], "the source declares no index"


# ── 4. honesty about coverage ──────────────────────────────────────────────


def test_capped_catalog_cannot_claim_to_be_ready():
    tables = [
        {"table_name": "t1", "columns": [{"description_zh": "x"}], "description_zh": "x"}
    ]
    state, reason = cat._classify_completion(tables, truncated=700)
    assert state == rd.STATE_ENRICHMENT_PARTIAL
    assert "1 of 701 objects" in reason, reason
    assert "object cap" in reason

    # …and without truncation the same catalog is ready, so the cap is the
    # only thing that changed the verdict.
    state, _ = cat._classify_completion(tables)
    assert state == rd.STATE_READY


def test_description_prompt_carries_the_source_comment():
    prompt = cat._build_description_prompt(
        [
            {
                "table_name": "erp_t_demo",
                "table_type": "BASE TABLE",
                "row_count": 100,
                "row_count_source": "estimated",
                "table_comment": "订单主表",
                "columns": [
                    {
                        "column_name": "FQTY",
                        "data_type": "decimal(18,4)",
                        "is_primary_key": False,
                        "is_nullable": True,
                        "comment": "数量",
                    }
                ],
            }
        ]
    )[0]
    assert "数据库注释: 订单主表" in prompt
    assert "数据库注释: 数量" in prompt
    assert "估算" in prompt, "an estimated row count must not read as a measurement"


def test_normalize_desc_passes_comments_and_keys_through():
    _schema, _type, cols = cat._normalize_desc(
        [{"name": "FQTY", "type": "decimal", "nullable": True, "pk": False,
          "comment": "  数量  ", "key": "uni"}]
    )
    assert cols[0]["comment"] == "数量"
    assert cols[0]["key"] == "UNI"


def test_normalize_desc_tolerates_dialects_without_comments():
    _schema, _type, cols = cat._normalize_desc(
        [{"name": "a", "type": "int", "nullable": True, "pk": False}]
    )
    assert cols[0]["comment"] is None
    assert cols[0]["key"] == ""
