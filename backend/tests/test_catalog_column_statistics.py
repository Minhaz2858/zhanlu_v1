"""Slice 2 — onboarding must measure the DATA, not only the DDL.

Measured gap this suite pins down (audit of the pre-2026-09-20 indexer):

* Nothing ever read a single row during catalog indexing except up to 12
  single-column ``SELECT DISTINCT`` probes. Null rates, cardinalities, value
  ranges and value frequencies — the things that tell a model whether a column
  is usable at all — were never collected.
* Three code paths already expected that information and always found it empty:
  ``data_source_runtime._load_catalog_meta`` (feeds the answer-verification
  gate's catalog validation, so the gate silently ran in lexical-only mode),
  ``domain_discovery._cluster_digest``, and ``semantic_store.sample_values()``.
  ``KBColumnMeta.sample_values`` / ``KBTableMeta.sample_values`` were declared
  in the model and written by nobody.
* ``classify_column_semantics`` called every string column without a small
  enumeration a ``dimension`` — including a free-text column whose sampled
  values are all distinct, which offers the role gate a grouping key that
  cannot group anything.

What is pinned here: statistics come from ONE bounded sample per object; a
sampled figure is never reported as a measured total; and "not measured" is an
absent value, never zero.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

from app.services.db.sqlite import SQLiteConnector
from app.services.knowledge_graph import catalog_indexer as cat
from app.services.knowledge_graph import column_stats as cs


# ── helpers ────────────────────────────────────────────────────────────────


def _sqlite_conn(path: str) -> SQLiteConnector:
    return SQLiteConnector(SimpleNamespace(api_url=path, file_url=None))


def _column(name: str, data_type: str, **kw) -> dict:
    return {"column_name": name, "data_type": data_type, **kw}


def _rows(**cols) -> list[dict]:
    """Canned sample rows from column lists of equal length."""
    keys = list(cols)
    return [dict(zip(keys, values)) for values in zip(*(cols[k] for k in keys))]


@pytest.fixture
def shop_db():
    """A real SQLite file with real rows — the SQL path is exercised, not faked."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE orders ("
        "  id INTEGER PRIMARY KEY,"
        "  status VARCHAR(10),"
        "  note TEXT,"
        "  amount DECIMAL(10,2),"
        "  created DATE"
        ")"
    )
    raw.executemany(
        "INSERT INTO orders (id, status, note, amount, created) VALUES (?,?,?,?,?)",
        [
            (1, "NEW", "first order", 10.5, "2026-01-01"),
            (2, "PAID", "second", 0, "2026-01-02"),
            (3, "PAID", None, 99.25, "2026-01-03"),
            (4, "CANCELLED", "fourth", 4.0, "2026-01-04"),
        ],
    )
    raw.commit()
    raw.close()
    try:
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


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


# ── 1. the measurement itself (real SQL, real dialect) ─────────────────────


def test_one_bounded_sample_per_object_measures_the_data(shop_db):
    with _sqlite_conn(shop_db) as conn:
        stats = cs.profile_table_columns(
            conn,
            "orders",
            [
                _column("id", "INTEGER"),
                _column("status", "VARCHAR(10)"),
                _column("note", "TEXT"),
                _column("amount", "DECIMAL(10,2)"),
                _column("created", "DATE"),
            ],
        )

    assert stats["status"]["distinct"] == 3
    assert {v["value"] for v in stats["status"]["top_values"]} == {
        "NEW", "PAID", "CANCELLED",
    }
    # counts are what the sample saw, and the column is a cohort
    assert stats["status"]["top_values"][0] == {"value": "PAID", "count": 2}
    assert stats["status"]["nulls"] == 0 and stats["status"]["null_rate"] == 0.0

    assert stats["note"]["nulls"] == 1, "NULL in the sample is counted"
    assert stats["note"]["null_rate"] == 0.25
    # every note value differs → no vocabulary to enumerate
    assert stats["note"]["distinct"] == 3

    assert stats["amount"]["min"] == 0.0
    assert stats["amount"]["max"] == 99.25
    assert stats["amount"]["avg"] == pytest.approx(28.4375)
    assert stats["amount"]["zeros"] == 1, "an explicit zero is a fact about the data"

    assert stats["created"]["min"] == "2026-01-01"
    assert stats["created"]["max"] == "2026-01-04"
    assert stats["created"]["temporal"] is True


def test_a_bounded_sample_never_claims_an_exact_figure(shop_db):
    """cap reached → lower bounds, and every exactness flag says so."""
    with _sqlite_conn(shop_db) as conn:
        stats = cs.profile_table_columns(
            conn, "orders", [_column("id", "INTEGER"), _column("status", "VARCHAR(10)")],
            sample_rows=2,
        )

    assert stats["id"]["basis"] == "sample"
    assert stats["id"]["rows_sampled"] == 2
    assert stats["id"]["distinct_is_exact"] is False, (
        "4 distinct ids seen through a 2-row window is a lower bound"
    )
    assert stats["id"]["min_is_exact"] is False
    assert stats["id"]["max_is_exact"] is False
    assert stats["id"]["top_values_basis"] == "sample"


def test_a_complete_read_is_marked_exact(shop_db):
    with _sqlite_conn(shop_db) as conn:
        stats = cs.profile_table_columns(
            conn, "orders", [_column("status", "VARCHAR(10)")], sample_rows=200
        )
    entry = stats["status"]
    assert entry["basis"] == "full", "fewer rows than the cap = nothing left to read"
    assert entry["rows_sampled"] == 4
    assert entry["distinct"] == 3 and entry["distinct_is_exact"] is True


def test_an_empty_object_yields_no_statistics_not_zeros(shop_db):
    with _sqlite_conn(shop_db) as conn:
        conn.execute("CREATE TABLE empty_t (a INT, b VARCHAR(4))")
        assert cs.profile_table_columns(conn, "empty_t", [
            _column("a", "INT"), _column("b", "VARCHAR(4)"),
        ]) == {}, "no rows read → no entry at all, never nulls=0"


def test_an_unreadable_object_yields_no_statistics(monkeypatch):
    class _Broken:
        dialect = "mysql"

        def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
            raise RuntimeError("SELECT command denied")

    assert cs.profile_table_columns(
        _Broken(), "t", [_column("a", "INT")]
    ) == {}


def test_binary_and_json_columns_are_never_value_profiled():
    """A BLOB is never SELECTed; a JSON column is counted for NULLs only."""
    sqls: list[str] = []

    class _Conn:
        dialect = "mysql"

        def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
            sqls.append(sql)
            return [{"a": 1, "j": None, "j2": None}]

    stats = cs.profile_table_columns(
        _Conn(),
        "t",
        [
            _column("a", "INT"),
            _column("blob_col", "LONGBLOB"),
            _column("bin_col", "VARBINARY(64)"),
            _column("j", "JSON"),
        ],
    )
    assert "blob_col" not in stats and "bin_col" not in stats
    assert not any("blob_col" in s or "bin_col" in s for s in sqls), (
        "reading a BLOB into memory to count NULLs is how an index job dies"
    )
    assert stats["j"]["nulls"] == 1
    assert "top_values" not in stats["j"], "JSON values are not a vocabulary"


def test_a_numeric_string_column_stays_a_string(shop_db):
    """Declared type decides: a varchar holding digits is not a measure."""
    with _sqlite_conn(shop_db) as conn:
        conn.execute("CREATE TABLE codes (c VARCHAR(6))")
        conn.execute("INSERT INTO codes VALUES ('007'), ('12'), ('9')")
        stats = cs.profile_table_columns(conn, "codes", [_column("c", "VARCHAR(6)")])
    assert "numeric" not in stats["c"]
    assert stats["c"]["min_len"] == 1 and stats["c"]["max_len"] == 3


def test_a_boolean_is_not_a_number():
    rows = _rows(flag=[True, False, None])
    stats = cs.compute_column_stats(
        rows, [_column("flag", "BOOLEAN")], cap=200
    )
    assert "avg" not in stats["flag"], "True is not 1"
    assert stats["flag"]["nulls"] == 1


def test_high_cardinality_columns_are_not_enumerated():
    rows = _rows(free=[f"row-{i}" for i in range(80)])
    stats = cs.compute_column_stats(rows, [_column("free", "VARCHAR(20)")], cap=200)
    assert stats["free"]["distinct"] == 80
    assert "top_values" not in stats["free"], "80 distinct values say nothing"


def test_a_temporal_column_is_not_enumerated_as_a_vocabulary():
    """A date histogram is not a label — the range is the fact."""
    rows = _rows(d=["2026-01-01", "2026-01-02", "2026-01-01"])
    stats = cs.compute_column_stats(rows, [_column("d", "DATE")], cap=200)
    assert stats["d"]["min"] == "2026-01-01" and stats["d"]["max"] == "2026-01-02"
    assert "top_values" not in stats["d"]


def test_a_primary_key_is_not_enumerated_as_a_vocabulary():
    """Keys are per-row by definition; their values are not business vocabulary."""
    rows = _rows(cid=[1, 2, 3])
    stats = cs.compute_column_stats(
        rows, [_column("cid", "INT", is_primary_key=True)], cap=200
    )
    assert stats["cid"]["distinct"] == 3
    assert "top_values" not in stats["cid"]
    assert cs.column_sample_values(stats["cid"]) is None


def test_a_low_cardinality_measure_is_still_enumerated():
    """An ERP tax-rate column holds 0 / 16 — that IS worth knowing."""
    rows = _rows(rate=[16.0] * 8 + [0.0] * 2)
    stats = cs.compute_column_stats(rows, [_column("rate", "DECIMAL(6,2)")], cap=200)
    assert stats["rate"]["distinct"] == 2
    assert cs.column_sample_values(stats["rate"]) == {"16.0": 8, "0.0": 2}


# ── 2. what the semantics classifier learns from the measurement ───────────


def test_a_free_text_column_is_not_offered_as_a_dimension():
    """Every sampled value distinct → no cohort to group by, whatever its name."""
    stats = {"rows_sampled": 60, "basis": "sample", "distinct": 60,
             "distinct_is_exact": False}
    kind, enum = cat.classify_column_semantics(
        _column("remark", "VARCHAR(200)", stats=stats)
    )
    assert kind == "free_text" and enum is None


def test_a_genuine_cohort_stays_a_dimension():
    stats = {"rows_sampled": 60, "basis": "sample", "distinct": 4,
             "distinct_is_exact": False,
             "top_values": [{"value": "A", "count": 30}, {"value": "B", "count": 20}]}
    kind, _ = cat.classify_column_semantics(_column("channel", "VARCHAR(8)", stats=stats))
    assert kind == "dimension", "4 distinct values in 60 rows is a real dimension"


def test_a_sample_too_small_to_prove_cardinality_keeps_the_name_based_answer():
    stats = {"rows_sampled": 5, "basis": "full", "distinct": 5,
             "distinct_is_exact": True}
    kind, _ = cat.classify_column_semantics(_column("name", "VARCHAR(20)", stats=stats))
    assert kind == "dimension", (
        "5 rows is not evidence that a 20-char name column has no cohorts"
    )


def test_a_provably_complete_enumeration_becomes_enum_values():
    """Whole object read + values fit → the dictionary is complete, not truncated."""
    stats = {
        "rows_sampled": 6, "basis": "full", "distinct": 3, "distinct_is_exact": True,
        "top_values": [
            {"value": "NEW", "count": 3},
            {"value": "PAID", "count": 2},
            {"value": "HOLD", "count": 1},
        ],
    }
    kind, enum = cat.classify_column_semantics(
        _column("status", "VARCHAR(10)", stats=stats)
    )
    assert kind == "dimension"
    assert enum == ["NEW", "PAID", "HOLD"]
    assert enum is not None and len(enum) == stats["distinct"], (
        "a dictionary must cover every value or not be called complete"
    )


def test_a_bounded_enumeration_is_never_promoted_to_enum_values():
    stats = {
        "rows_sampled": 200, "basis": "sample", "distinct": 3,
        "distinct_is_exact": False,
        "top_values": [{"value": "A", "count": 3}],
    }
    _, enum = cat.classify_column_semantics(
        _column("status", "VARCHAR(10)", stats=stats)
    )
    assert enum is None, "a sample can never prove an enumeration is complete"


# ── 3. the shapes downstream already reads ─────────────────────────────────


def test_column_scan_values_shape_is_value_to_count():
    stats = {
        "top_values": [
            {"value": "华东", "count": 12},
            {"value": "华南", "count": 7},
        ]
    }
    assert cs.column_sample_values(stats) == {"华东": 12, "华南": 7}
    assert cs.column_sample_values(None) is None
    assert cs.column_sample_values({"nulls": 0}) is None, "no values → None, not {}"


def test_table_sample_values_shape_is_column_to_values():
    stats = {
        "region": {"top_values": [{"value": "华东", "count": 12}]},
        "status": {"top_values": [{"value": "PAID", "count": 9}]},
        "amount": {"distinct": 900},
    }
    assert cs.table_sample_values(stats) == {"region": ["华东"], "status": ["PAID"]}
    assert cs.table_sample_values({}) is None


def test_the_persisted_table_shape_is_readable_by_domain_discovery():
    """Round-trip: what the indexer writes is what the cluster digest reads."""
    sample = {"status": ["NEW", "PAID"], "region": ["华东"]}
    digest_source = SimpleNamespace(
        description_en="订单", description_zh=None, sample_values=sample,
        table_role="fact", table_name="orders",
    )
    line = cat_domain_digest([digest_source])
    assert "status: NEW, PAID" in line
    assert "region: 华东" in line


def cat_domain_digest(members):
    from app.services.knowledge_graph.domain_discovery import _cluster_digest

    return _cluster_digest(members)


def test_the_prompt_labels_a_sampled_figure_as_sampled():
    sampled = cs.stats_prompt_suffix(
        {"rows_sampled": 200, "basis": "sample", "null_rate": 0.5,
         "distinct": 40, "distinct_is_exact": False}
    )
    assert "200 行抽样" in sampled and "≥40" in sampled and "nulls 50%" in sampled

    full = cs.stats_prompt_suffix(
        {"rows_sampled": 4, "basis": "full", "null_rate": 0.0,
         "distinct": 3, "distinct_is_exact": True,
         "numeric": True, "min": 0, "max": 99.25, "avg": 28.44,
         "min_is_exact": True, "max_is_exact": True}
    )
    assert "全表" in full and "≥" not in full
    assert cs.stats_prompt_suffix(None) == ""
    assert cs.stats_prompt_suffix({}) == ""


# ── 4. persistence: the measurement reaches the catalog ────────────────────


def _table_dict(**kw) -> dict:
    base = {
        "schema_name": "",
        "table_name": "orders",
        "table_type": "BASE TABLE",
        "row_count": 4,
        "row_count_source": "exact",
        "columns": [],
    }
    base.update(kw)
    return base


def test_statistics_are_persisted_per_column_and_per_table(db):
    table = _table_dict(
        columns=[
            _column(
                "status", "VARCHAR(10)",
                stats={"rows_sampled": 4, "basis": "full", "nulls": 0,
                       "null_rate": 0.0, "distinct": 2, "distinct_is_exact": True,
                       "top_values": [{"value": "PAID", "count": 2},
                                      {"value": "NEW", "count": 1}]},
            ),
            _column(
                "amount", "DECIMAL(10,2)",
                stats={"rows_sampled": 4, "basis": "full", "nulls": 0,
                       "null_rate": 0.0, "distinct": 3, "distinct_is_exact": True,
                       "numeric": True, "min": 0.0, "max": 9.5, "avg": 3.1},
            ),
            _column("id", "INT"),      # no stats collected
        ]
    )
    cat._persist_catalog(db, "kb-1", [table])

    meta = db.query(KBTableMeta).filter(KBTableMeta.kb_id == "kb-1").one()
    cols = {c.column_name: c for c in db.query(KBColumnMeta).all()}

    assert cols["amount"].stats_json["avg"] == 3.1
    assert cols["status"].stats_json["basis"] == "full"
    # the vocabulary bridge the answer-verification gate reads
    assert cols["status"].sample_values == {"PAID": 2, "NEW": 1}
    # the table-level shape domain_discovery reads
    assert meta.sample_values == {"status": ["PAID", "NEW"]}


def test_a_column_with_no_measurement_stores_none_not_zero(db):
    cat._persist_catalog(db, "kb-1", [_table_dict(columns=[_column("id", "INT")])])
    col = db.query(KBColumnMeta).one()
    assert col.stats_json is None, "not measured must not look like a measurement"
    assert col.sample_values is None
    meta = db.query(KBTableMeta).one()
    assert meta.sample_values is None


def test_a_reindex_refreshes_the_measurement(db):
    first = _table_dict(columns=[
        _column("status", "VARCHAR(10)",
                stats={"rows_sampled": 2, "basis": "full", "nulls": 0, "null_rate": 0.0,
                       "distinct": 1, "distinct_is_exact": True,
                       "top_values": [{"value": "NEW", "count": 2}]})
    ])
    cat._persist_catalog(db, "kb-1", [first])
    assert db.query(KBColumnMeta).one().sample_values == {"NEW": 2}

    second = _table_dict(columns=[
        _column("status", "VARCHAR(10)",
                stats={"rows_sampled": 5, "basis": "full", "nulls": 1, "null_rate": 0.2,
                       "distinct": 2, "distinct_is_exact": True,
                       "top_values": [{"value": "PAID", "count": 3},
                                      {"value": "NEW", "count": 1}]})
    ])
    cat._persist_catalog(db, "kb-1", [second])
    cols = db.query(KBColumnMeta).all()
    assert len(cols) == 1, "re-index upserts, it does not duplicate columns"
    assert cols[0].sample_values == {"PAID": 3, "NEW": 1}
    meta = db.query(KBTableMeta).one()
    assert meta.sample_values == {"status": ["PAID", "NEW"]}


def test_the_stats_pass_runs_for_objects_known_only_by_estimate(monkeypatch):
    """COUNT(*) timing out must not silently remove the only measurements we get."""
    calls: list[str] = []

    class _Estimated:
        dialect = "mysql"

        def describe_table(self, table):
            return [{"name": "a", "type": "int", "nullable": True, "default": None, "pk": False}]

        def table_facts(self, table):
            return {"est_rows": 400000}

        def get_indexes(self, table):
            return []

        def get_foreign_keys(self, table):
            return []

        def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
            calls.append(sql)
            if "COUNT(*)" in sql:
                raise RuntimeError("statement timeout")
            return [{"a": 1}, {"a": 2}]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr(cat, "get_connector", lambda kb: _Estimated())
    monkeypatch.setattr(cat.settings, "CATALOG_JOIN_EDGES_ENABLED", False)
    monkeypatch.setattr(cat.settings, "KG_COVERAGE_PROBE_ENABLED", False)

    table = cat._introspect_one_table(_kb_row(), "big_fact")
    assert table["row_count_source"] == "estimated"
    assert table["columns"][0]["stats"]["rows_sampled"] == 2
    assert any("SELECT" in s and "COUNT(*)" not in s and "`a`" in s for s in calls), (
        "the row sample must still be taken from a table we only know by estimate"
    )
