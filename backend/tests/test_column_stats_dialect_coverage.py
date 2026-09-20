"""Cross-dialect coverage for catalog column statistics.

The statistics pass must work on ANY database a user binds, not on the one
warehouse it was developed against.  Two things decide that, and neither is
allowed to live in feature code:

1. **Which types may be read, and what a read means** — `db.dialect.type_role`,
   a data table keyed by dialect.  The dangerous cases are types whose name
   contains a generic token that means something ELSE in that vendor.  The
   worst is SQL Server: `timestamp` is ROWVERSION, an 8-byte binary, so a
   keyword-matching implementation reads it as a date.  Oracle `interval year to
   month` and PostgreSQL `interval` contain `int`.  PostgreSQL `integer[]`
   embeds its element type.  Each of those is pinned below for every dialect.

2. **The statement itself** — one bounded sample per object, built from the
   connecting dialect's own quoting and pagination (``LIMIT`` / ``TOP`` /
   ``FETCH FIRST``).  A dialect that cannot safely quote one column name must
   drop that column rather than drop the row cap: an uncapped SELECT of every
   column is a worse outcome than profiling one column less.

No MSSQL or Oracle SERVER was available to run against; what is pinned here is
the statement and the classification, which is everything that is per-dialect.
The connector contract (execute/paging) is exercised live on MySQL, PostgreSQL,
MariaDB and SQLite elsewhere.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.db import dialect as dl
from app.services.db.connector_factory import supported_db_types
from app.services.knowledge_graph import column_stats as cs


class _Conn:
    """Just enough connector for SQL construction and role resolution."""

    def __init__(self, dialect: str | None, rows=None):
        self.dialect = dialect
        self._rows = rows or []
        self.statements: list[str] = []

    def execute(self, sql, params=None, max_rows=1000, timeout_s=10):
        self.statements.append(sql)
        return self._rows


# ── 1. every dialect we support can answer ────────────────────────────────


def test_every_supported_dialect_has_a_profile():
    assert supported_db_types(), "no dialect registered at all"
    for db_type in supported_db_types():
        assert dl.maybe_profile(db_type) is not None, db_type


def test_the_generic_table_is_never_used_to_guess_a_dialect():
    """An unknown dialect must not silently borrow another vendor's types."""
    profile = dl.maybe_profile("mongodb")
    assert profile is None
    assert dl.type_role("mongodb", "int") == "numeric", (
        "unknown dialect still gets the generic ANSI-ish table, not a guess"
    )
    assert dl.type_role("mongodb", "some_vendor_exotic") == "other", (
        "and anything it cannot name stays countable-only"
    )


# ── 2. binary / large-object types, per dialect ────────────────────────────


BINARY_BY_DIALECT = {
    "mysql": [
        "blob", "tinyblob", "mediumblob", "longblob", "varbinary(255)",
        "geometry", "point", "multipoint", "polygon", "geometrycollection",
    ],
    "mariadb": ["blob", "longblob", "varbinary(64)"],
    "postgresql": ["bytea", "geometry", "geography"],
    "postgres": ["bytea", "bytea"],
    "mssql": [
        "varbinary(max)", "binary(16)", "image", "rowversion",
        "timestamp", "hierarchyid", "xml", "geometry", "geography",
    ],
    "sqlserver": ["varbinary(8000)", "image", "timestamp"],
    "oracle": [
        "blob", "bfile", "raw(16)", "long raw", "xmltype", "sdo_geometry",
    ],
    "sqlite": ["blob"],
}


@pytest.mark.parametrize(
    "db_type,data_type",
    [(d, t) for d, types in BINARY_BY_DIALECT.items() for t in types],
)
def test_binary_types_are_never_read(db_type, data_type):
    assert dl.type_role(db_type, data_type) == "binary", (
        f"{db_type}.{data_type} must never be SELECTed"
    )
    assert cs.is_profilable(data_type, db_type) is False


def test_a_column_that_must_not_be_read_is_left_out_of_the_statement():
    columns = [
        {"column_name": "id", "data_type": "int"},
        {"column_name": "payload", "data_type": "longblob"},
        {"column_name": "shape", "data_type": "geometry"},
    ]
    conn = _Conn("mysql", rows=[{"id": 1}])
    cs.profile_table_columns(conn, "t", columns)
    assert len(conn.statements) == 1, "still exactly one sample per object"
    sql = conn.statements[0]
    assert "`id`" in sql
    assert "payload" not in sql and "shape" not in sql


# ── 3. the type-name traps ────────────────────────────────────────────────


def test_mssql_timestamp_is_rowversion_not_a_time():
    """The single most damaging mapping: it looks right and is wrong."""
    assert dl.type_role("mssql", "timestamp") == "binary"
    assert dl.type_role("sqlserver", "timestamp") == "binary"
    # …and the same name IS temporal everywhere else.
    for db_type in ("mysql", "mariadb", "postgres", "postgresql", "oracle", "sqlite"):
        assert dl.type_role(db_type, "timestamp") == "temporal", db_type
        assert dl.type_role(db_type, "timestamp with time zone") == "temporal"


def test_interval_is_never_mistaken_for_an_integer():
    """'interval' contains 'int' — substring matching gets this wrong by default."""
    assert dl.type_role("postgres", "interval") == "other"
    assert dl.type_role("oracle", "interval year to month") == "other"
    assert dl.type_role(None, "interval") == "other", "generic table too"


def test_array_types_are_not_comparable_scalars():
    for data_type in ("integer[]", "text[]", "numeric(10,2)[]", "character varying[]"):
        assert dl.type_role("postgres", data_type) == "other", data_type


def test_a_longer_token_always_beats_the_token_it_contains():
    assert dl.type_role("oracle", "long raw") == "binary", "not 'long' → large"
    assert dl.type_role("oracle", "long") == "large"
    assert dl.type_role("oracle", "raw(2000)") == "binary"
    assert dl.type_role("mssql", "varbinary(max)") == "binary"
    assert dl.type_role("mysql", "longtext") == "large", "not text → string"
    assert dl.type_role("mysql", "mediumtext") == "string"
    assert dl.type_role("mysql", "tinytext") == "string"
    assert dl.type_role("postgres", "jsonb") == "other"


def test_large_text_is_readable_but_is_never_a_vocabulary():
    rows = [{"note": "a" * 4000}, {"note": "b"}, {"note": "b"}]
    stats = cs.compute_column_stats(
        rows, [{"column_name": "note", "data_type": "longtext"}], cap=200,
        dialect="mysql",
    )
    assert stats["note"]["role"] == "large"
    assert stats["note"]["max_len"] == 4000, "its size is the useful fact"
    assert "top_values" not in stats["note"], "a document is not a vocabulary"
    # …while the ordinary text sizes stay a vocabulary.
    for data_type in ("mediumtext", "tinytext", "text", "varchar(20)"):
        s = cs.compute_column_stats(
            rows, [{"column_name": "note", "data_type": data_type}], cap=200,
            dialect="mysql",
        )
        assert "top_values" in s["note"], data_type


def test_a_bit_column_is_countable_but_not_comparable():
    for db_type in ("mysql", "mssql"):
        assert dl.type_role(db_type, "bit") == "other"
    # The driver hands BIT back as raw bytes; a byte string is not a label.
    stats = cs.compute_column_stats(
        [{"f": b"\x01"}, {"f": b"\x00"}],
        [{"column_name": "f", "data_type": "bit"}], cap=200, dialect="mssql",
    )
    assert stats["f"]["role"] == "other"
    assert stats["f"]["distinct"] == 2, "countable"
    assert "top_values" not in stats["f"], "not comparable"


def test_the_role_is_recorded_so_the_decision_is_auditable():
    stats = cs.compute_column_stats(
        [{"a": 1}], [{"column_name": "a", "data_type": "number"}],
        cap=200, dialect="oracle",
    )
    assert stats["a"]["role"] == "numeric", "Oracle NUMBER, not MySQL INT"


# ── 4. the statement, per dialect ─────────────────────────────────────────


def test_each_dialect_gets_its_own_quoting_and_pagination():
    cases = {
        "mysql": "LIMIT 5",
        "mariadb": "LIMIT 5",
        "postgresql": "LIMIT 5",
        "sqlite": "LIMIT 5",
        "mssql": "TOP 5",
        "oracle": "FETCH FIRST 5 ROWS ONLY",
    }
    for db_type, expected in cases.items():
        sql = cs.sample_rows_sql(_Conn(db_type), "orders", ["amount", "qty"], 5)
        assert expected in sql, (db_type, sql)
        assert sql.count("amount") == 1 and sql.count("qty") == 1, sql
    # quoting is the dialect's, not ours
    assert "`amount`" in cs.sample_rows_sql(_Conn("mysql"), "orders", ["amount"], 5)
    assert '"amount"' in cs.sample_rows_sql(_Conn("postgresql"), "orders", ["amount"], 5)
    assert "[amount]" in cs.sample_rows_sql(_Conn("mssql"), "orders", ["amount"], 5)
    # no LIMIT on a dialect that has none
    assert "LIMIT" not in cs.sample_rows_sql(_Conn("oracle"), "orders", ["amount"], 5)
    assert "LIMIT" not in cs.sample_rows_sql(_Conn("mssql"), "orders", ["amount"], 5)


def test_an_unquotable_column_is_dropped_without_losing_the_row_cap():
    """One unnameable column must not downgrade the whole statement."""
    sql = cs.sample_rows_sql(
        _Conn("oracle"), "orders", ["amount", "订单号", "qty"], 5
    )
    assert "订单号" not in sql, "Oracle cannot be asked for this name safely"
    assert "amount" in sql and "qty" in sql, "the rest is still profiled"
    assert "FETCH FIRST 5 ROWS ONLY" in sql, "the cap is what must not be lost"


def test_a_fully_unquotable_object_yields_nothing_rather_than_an_uncapped_select():
    conn = _Conn("oracle", rows=[{"x": 1}])
    assert cs.sample_rows_sql(conn, "orders", ["订单号"], 5) == ""
    assert cs.profile_table_columns(
        conn, "orders", [{"column_name": "订单号", "data_type": "varchar2(20)"}]
    ) == {}
    assert conn.statements == [], "no statement at all is the correct answer here"


def test_a_column_omitted_from_the_statement_is_not_reported_as_null():
    """Absence of evidence must never become a measurement."""
    conn = _Conn("mysql", rows=[{"id": 1}, {"id": 2}])
    stats = cs.profile_table_columns(
        conn,
        "t",
        [
            {"column_name": "id", "data_type": "int"},
            {"column_name": "blob_col", "data_type": "longblob"},
        ],
    )
    assert "blob_col" not in stats
    assert stats["id"]["null_rate"] == 0.0


def test_uppercase_column_names_still_resolve():
    """Oracle reports identifiers upper-cased however they were asked for."""
    conn = _Conn("oracle", rows=[{"FSTATUS": "PAID"}, {"FSTATUS": "PAID"}])
    stats = cs.profile_table_columns(
        conn, "orders", [{"column_name": "fstatus", "data_type": "varchar2(10)"}]
    )
    assert stats["fstatus"]["distinct"] == 1
    assert cs.column_sample_values(stats["fstatus"]) == {"PAID": 2}


def test_the_same_object_profiles_the_same_way_whatever_the_dialect():
    """End-to-end shape check across dialects on identical data."""
    columns = [
        {"column_name": "region", "data_type": "varchar(20)"},
        {"column_name": "amount", "data_type": "numeric(12,2)"},
        {"column_name": "booked_at", "data_type": "timestamp"},
        {"column_name": "x", "data_type": "some_exotic_type"},
    ]
    rows = [
        {"region": "华东", "amount": 1.5, "booked_at": "2026-01-01T00:00:00", "x": None},
        {"region": "华南", "amount": 2.5, "booked_at": "2026-01-02T00:00:00", "x": None},
    ]
    for db_type in ("mysql", "mariadb", "postgresql", "mssql", "oracle", "sqlite"):
        data_type_by_name = {c["column_name"]: c["data_type"] for c in columns}
        # MSSQL's 'timestamp' is ROWVERSION, so a temporal column is spelled
        # datetime2 there — the point is that the pipeline behaves the same.
        if db_type == "mssql":
            data_type_by_name["booked_at"] = "datetime2"
            data_type_by_name["amount"] = "decimal(12,2)"
        stats = cs.compute_column_stats(
            rows,
            [
                {"column_name": n, "data_type": t}
                for n, t in data_type_by_name.items()
            ],
            cap=200,
            dialect=db_type,
        )
        assert stats["region"]["top_values"] == [
            {"value": "华东", "count": 1}, {"value": "华南", "count": 1},
        ], db_type
        assert stats["amount"]["role"] == "numeric" and stats["amount"]["max"] == 2.5, db_type
        assert stats["booked_at"]["role"] == "temporal"
        assert stats["booked_at"]["min"] == "2026-01-01T00:00:00", db_type
        assert stats["x"]["role"] == "other"
        assert stats["x"]["null_rate"] == 1.0, "all-NULL is measured, not absent"
        assert "top_values" not in stats["x"]
