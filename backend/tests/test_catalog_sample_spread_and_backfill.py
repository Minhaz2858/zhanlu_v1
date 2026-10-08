"""The four gaps closed after slice 2b — every one of them a live failure.

Each section pins a defect that was observed on real data, not a hypothetical:

A. **The sample was one arbitrary page.** ``LIMIT 200`` on the 14,589-row
   ``v_sale_order`` returned 2018-06-01…2018-09-25 of a PLANDATE range
   that actually runs to 2026-09-19, so everything the customer sold after that
   first page was invisible to the vocabulary the selector and the embedder
   read.  The sample is now a HEAD + TAIL page, de-duplicated.
B. **``varbinary(0)`` was treated as a payload.** MySQL types a VIEW column
   whose expression it cannot resolve that way; the three live ones are NULL in
   all 14,589 rows.  Refusing to read them meant the prompt showed three
   apparently usable columns with no annotation, instead of "nulls 100%".
C. **Nothing backfilled.** A catalog indexed before the statistics era renders
   nothing extra until it is re-collected, and there was no path that did that
   without a human running a script.
D. **The compact selector line had no vocabulary.** Two ERP objects are
   routinely named alike (``erp_t_sale_orderentry`` / ``v_sale_order``);
   only the values that occur in them separate them.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  register all models
from app.models.knowledge_base import KnowledgeBase
from app.models.knowledge_catalog import KBColumnMeta, KBTableMeta

from app.services.db.dialect import type_role
from app.services.db.sqlite import SQLiteConnector
from app.services.knowledge_graph import catalog_triggers as ct
from app.services.knowledge_graph import column_stats as cs
from app.services.knowledge_graph import schema_linker as sl


# ── helpers ────────────────────────────────────────────────────────────────


def _column(name: str, data_type: str, **kw) -> dict:
    return {"column_name": name, "data_type": data_type, **kw}


@pytest.fixture
def big_table(tmp_path):
    """500 rows where the interesting vocabulary lives ONLY in the tail.

    Rows 1-450 sell P<n>; the last 50 sell 混三甲苯.  One ordered page of 100
    therefore cannot see it — which is exactly the live defect.
    """
    path = str(tmp_path / "spread.sqlite")
    raw = sqlite3.connect(path)
    raw.execute(
        "CREATE TABLE big (id INTEGER PRIMARY KEY, product TEXT, amount REAL,"
        " sold_on TEXT)"
    )
    raw.executemany(
        "INSERT INTO big (id, product, amount, sold_on) VALUES (?,?,?,?)",
        [
            (i, f"P{(i % 3) + 1}", float(i), f"2024-{((i - 1) % 12) + 1:02d}-01")
            for i in range(1, 451)
        ]
        + [
            (i, "混三甲苯", float(i), "2026-09-19")
            for i in range(451, 501)
        ],
    )
    raw.commit()
    raw.close()
    with SQLiteConnector(SimpleNamespace(api_url=path, file_url=None)) as conn:
        yield conn


COLUMNS = [
    _column("id", "INTEGER", is_primary_key=True),
    _column("product", "varchar(32)"),
    _column("amount", "decimal(12,2)"),
    _column("sold_on", "datetime"),
]


def _top_values(stats: dict, column: str) -> list[str]:
    entry = (stats.get(column) or {}).get("top_values") or []
    return [str(item.get("value")) for item in entry if isinstance(item, dict)]


# ══ A. the spread sample ═══════════════════════════════════════════════════


def test_the_order_key_is_the_primary_key_first_then_a_temporal_column():
    assert cs.sample_order_key(COLUMNS) == "id"
    assert cs.sample_order_key(
        [_column("product", "varchar(32)"), _column("sold_on", "datetime")]
    ) == "sold_on"
    # Nothing orderable — the sample stays one page, exactly as before.
    assert cs.sample_order_key([_column("product", "varchar(32)")]) is None
    assert cs.sample_order_key([]) is None


def test_the_tail_page_is_ordered_descending(big_table):
    fwd = cs.sample_rows_sql(big_table, "big", ["id"], 100, order_by="id")
    back = cs.sample_rows_sql(
        big_table, "big", ["id"], 100, order_by="id", descending=True
    )
    assert 'ORDER BY "id"' in fwd and "DESC" not in fwd
    assert 'ORDER BY "id" DESC' in back


def test_a_single_page_would_miss_the_vocabulary(big_table):
    """The old behaviour, shown to be lossy on the same real table."""
    sql = cs.sample_rows_sql(big_table, "big", ["product"], 100)
    rows = big_table.execute(sql, max_rows=100, timeout_s=10)
    one_page = cs.compute_column_stats(
        rows, [_column("product", "varchar(32)")], cap=100, dialect="sqlite"
    )
    assert "混三甲苯" not in _top_values(one_page, "product"), (
        "fixture is wrong: the vocabulary is supposed to be tail-only"
    )


def test_the_spread_sample_sees_both_ends(big_table):
    stats = cs.profile_table_columns(big_table, "big", COLUMNS, sample_rows=100)
    product = stats["product"]
    assert product["rows_sampled"] == 200, "a head page and a tail page"
    assert product["basis"] == "sample"
    assert "混三甲苯" in _top_values(stats, "product"), (
        "the customer's own word, from the tail the old sample never read"
    )
    # The temporal range now covers the newest rows, not just the oldest.
    assert stats["sold_on"]["max"] == "2026-09-19"
    assert stats["id"]["min"] == 1.0


def test_a_table_smaller_than_one_page_is_still_a_COMPLETE_read(tmp_path):
    """Overlap detection is what keeps this an exact read.

    Both pages come back with the same 85 rows, and merging them would double
    every count. The pages are only merged when they are provably disjoint, so
    a small table is read once — and is therefore COMPLETE, not sampled.
    """
    path = str(tmp_path / "small.sqlite")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE tiny (id INTEGER PRIMARY KEY, product TEXT)")
    raw.executemany(
        "INSERT INTO tiny (id, product) VALUES (?,?)",
        [(i, "混三甲苯" if i > 80 else "P1") for i in range(1, 86)],
    )
    raw.commit()
    raw.close()
    with SQLiteConnector(SimpleNamespace(api_url=path, file_url=None)) as conn:
        stats = cs.profile_table_columns(
            conn, "tiny",
            [_column("id", "INTEGER", is_primary_key=True),
             _column("product", "varchar(8)")],
            sample_rows=200,
        )
    assert stats["product"]["rows_sampled"] == 85, "both pages, counted once"
    assert stats["product"]["basis"] == "full"
    assert stats["product"]["distinct_is_exact"] is True


def test_an_unorderable_table_still_profiles_from_one_page(tmp_path):
    """No key at all must not cost the statistics (fail-soft, not fail-dead)."""
    path = str(tmp_path / "nokey.sqlite")
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE nokey (product TEXT)")
    raw.executemany("INSERT INTO nokey (product) VALUES (?)", [("混三甲苯",)] * 5)
    raw.commit()
    raw.close()
    with SQLiteConnector(SimpleNamespace(api_url=path, file_url=None)) as conn:
        stats = cs.profile_table_columns(
            conn, "nokey", [_column("product", "varchar(8)")]
        )
    assert stats["product"]["basis"] == "full"
    assert _top_values(stats, "product") == ["混三甲苯"]


# ══ B. varbinary(0) is a declaration, not a payload ════════════════════════


def test_a_zero_length_binary_is_readable_and_a_real_blob_is_not():
    # MySQL's "unresolved VIEW expression" marker — readable, never enumerated.
    assert type_role("mysql", "varbinary(0)") == "other"
    assert type_role("mysql", "binary(0)") == "other"
    assert type_role("mysql", "VARBINARY( 0 )") == "other"
    # Real payload: still binary, still never SELECTed.
    assert type_role("mysql", "varbinary(255)") == "binary"
    assert type_role("mysql", "binary(16)") == "binary"
    assert type_role("mysql", "blob") == "binary"
    assert type_role("postgres", "bytea") == "binary"


def test_a_json_null_is_not_a_measurement(db):
    """The ORM stores a Python None as the JSON literal ``null``.

    ``stats_json IS NOT NULL`` is therefore TRUE for a column that was never
    measured, and ONE such row was enough to make a whole catalog look
    "already measured" — so the backfill would skip exactly the catalogs it
    exists for. The probe has to look past the literal.
    """
    kb = _kb(db)
    _table_with(db, kb, stats=None)
    db.execute(text("UPDATE kb_column_meta SET stats_json = 'null'"))
    db.commit()
    assert ct.catalog_missing_statistics(db, kb.id) is True


def test_a_null_only_varbinary_column_reports_nulls_instead_of_nothing(tmp_path):
    """Live shape: the column is NULL in every row, and that IS the answer."""
    path = str(tmp_path / "vb.sqlite")
    raw = sqlite3.connect(path)
    # SQLite takes any declared type, so the live MySQL declaration is reused
    # verbatim.
    raw.execute(
        "CREATE TABLE orders (id INTEGER PRIMARY KEY,"
        " instock_qty varbinary(0), payload varbinary(255))"
    )
    raw.execute("INSERT INTO orders (id, instock_qty, payload) VALUES (1, NULL, x'00')")
    raw.commit()
    raw.close()
    with SQLiteConnector(SimpleNamespace(api_url=path, file_url=None)) as conn:
        stats = cs.profile_table_columns(
            conn, "orders",
            [_column("id", "INTEGER", is_primary_key=True),
             _column("instock_qty", "varbinary(0)"),
             _column("payload", "varbinary(255)")],
        )
    assert "instock_qty" in stats, "a dead varbinary(0) column must be reported"
    assert stats["instock_qty"]["nulls"] == stats["instock_qty"]["rows_sampled"]
    rendered = cs.stats_annotation(stats["instock_qty"])
    assert "nulls 100%" in rendered, "what the SQL author needs to avoid the column"
    assert "payload" not in stats, "a real blob is still never read"


# ══ C. the backfill: no catalog is left without its measurements ═══════════


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


def _kb(db, *, db_type="mysql", **kw) -> KnowledgeBase:
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), name="Warehouse", source_kind="database",
        db_type=db_type, status="active", catalog_status="ready", **kw
    )
    db.add(kb)
    db.flush()
    return kb


def _table_with(db, kb, *, stats: dict | None, row_count: int = 14589) -> KBTableMeta:
    meta = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb.id, schema_name="",
        table_name="v_sale_order", table_type="VIEW",
        row_count=row_count,
    )
    db.add(meta)
    db.flush()
    db.add(KBColumnMeta(
        id=str(uuid.uuid4()), table_meta_id=meta.id, column_name="material_name",
        ordinal=1, data_type="varchar(64)", is_nullable=True, is_primary_key=False,
        stats_json=stats,
    ))
    db.commit()
    return meta


def test_a_catalog_with_no_measurements_is_detected(db):
    kb = _kb(db)
    _table_with(db, kb, stats=None)
    assert ct.catalog_missing_statistics(db, kb.id) is True


def test_a_catalog_with_measurements_is_left_alone(db):
    kb = _kb(db)
    _table_with(db, kb, stats={"rows_sampled": 200, "basis": "sample"})
    assert ct.catalog_missing_statistics(db, kb.id) is False


def test_an_empty_schema_is_not_treated_as_a_gap(db):
    """Nothing to measure is not the same as never having measured."""
    kb = _kb(db)
    _table_with(db, kb, stats=None, row_count=0)
    assert ct.catalog_missing_statistics(db, kb.id) is False


def test_the_backfill_re_indexes_only_the_stats_less_catalogs(db, monkeypatch):
    stale = _kb(db)
    _table_with(db, stale, stats=None)
    fresh = _kb(db)
    _table_with(db, fresh, stats={"rows_sampled": 200, "basis": "sample"})
    unsupported = _kb(db, db_type="mongodb")
    _table_with(db, unsupported, stats=None)

    spawned: list[str] = []
    monkeypatch.setattr(
        ct, "maybe_reindex_catalog_bg", lambda kb: spawned.append(str(kb.id))
    )
    from app.config import settings
    monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", True)

    ids = ct.backfill_catalog_statistics(db)

    assert spawned == [stale.id], "only the stale, catalogable KB"
    assert ids == [stale.id]
    assert fresh.id not in spawned and unsupported.id not in spawned


def test_the_backfill_is_a_no_op_when_the_catalog_feature_is_off(db, monkeypatch):
    kb = _kb(db)
    _table_with(db, kb, stats=None)
    spawned: list[str] = []
    monkeypatch.setattr(
        ct, "maybe_reindex_catalog_bg", lambda k: spawned.append(str(k.id))
    )
    from app.config import settings
    monkeypatch.setattr(settings, "SEMANTIC_CATALOG_ENABLED", False)
    assert ct.backfill_catalog_statistics(db) == []
    assert spawned == []


# ══ D. the compact selector line ═══════════════════════════════════════════


def test_the_compact_selector_line_carries_a_tighter_vocabulary_hint():
    row = {
        "table_name": "v_sale_order", "description": "销售订单明细",
        "row_count": 14589, "table_role": "fact",
        "sample_values": {
            "material_name": ["混三甲苯", "碳五石油树脂", "双环戊二烯", "电子元件"],
            "region": ["华东", "华南", "华北", "华中"],
        },
    }
    full = sl._toc_line(row)
    compact = sl._toc_line(row, compact=True)

    assert "混三甲苯" in compact, "the rest-of-catalog block needs the vocabulary too"
    hint = next(p for p in compact.split(", ") if p.startswith("values "))
    assert hint.count(":") == 1, "one column on a compact line"
    assert len(hint) <= 30, "the compact size promise still holds"
    assert "华东" in full and len([p for p in full.split(", ") if p.startswith("values ")]) == 1
    assert len(compact) < len(full)


def test_the_compact_line_stays_quiet_without_measurements():
    compact = sl._toc_line(
        {"table_name": "orders", "description": "o", "row_count": 6,
         "sample_values": None},
        compact=True,
    )
    assert "values" not in compact
