"""Slice 2 surfaces — the measurement must reach the prompts and the index.

Collecting per-column statistics is worthless if nothing reads them. Four
surfaces consume the catalog, and each one used to show DDL only:

1. the SQL-authoring DDL slice (`schema_linker._format_slice`) — where the model
   decides which column to filter and which to aggregate,
2. the table SELECTOR's line (`schema_linker._toc_line`) — where the model
   decides which TABLE a question is about,
3. the embedded TABLE document (`catalog_indexer._table_doc_text`) — the
   retrieval target for table search,
4. the embedded COLUMN document (`catalog_indexer._column_doc_text`) — the
   retrieval target for column search.

Pinned here: each surface carries the MEASUREMENT and the VOCABULARY, each
figure keeps its provenance ("全表" vs "N 行抽样"), an unmeasured column adds
nothing at all (never a zero), and every surface stays bounded — these texts go
into prompts with token budgets and into a vector store, not into a report.
"""

from __future__ import annotations

import os
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

from app.services.knowledge_graph import catalog_indexer as cat
from app.services.knowledge_graph import column_stats as cs
from app.services.knowledge_graph import schema_linker as sl


# ── fixtures ───────────────────────────────────────────────────────────────

FULL_STATS = {
    "rows_sampled": 6, "basis": "full", "role": "string",
    "nulls": 0, "null_rate": 0.0, "distinct": 3, "distinct_is_exact": True,
    "top_values": [
        {"value": "华东", "count": 2},
        {"value": "华南", "count": 2},
        {"value": "华北", "count": 2},
    ],
}
SAMPLED_STATS = {
    "rows_sampled": 200, "basis": "sample", "role": "numeric",
    "nulls": 24, "null_rate": 0.12, "distinct": 64, "distinct_is_exact": False,
    "numeric": True, "min": 221.98, "max": 8641379.31, "avg": 96450.5,
    "min_is_exact": False, "max_is_exact": False, "zeros": 0,
}


def _col(name="region", data_type="varchar(20)", **kw) -> dict:
    base = {
        "name": name, "data_type": data_type, "is_nullable": True,
        "is_primary_key": False, "description_zh": "区域",
        "description_en": "region", "semantic_type": "dimension",
        "enum_values": ["华东", "华南"], "unit": None, "stats": None,
    }
    base.update(kw)
    return base


def _table(**kw) -> dict:
    base = {
        "table_name": "orders", "table_meta_id": "m1", "row_count": 85,
        "table_type": "BASE TABLE", "description_zh": "销售订单",
        "description_en": "sales orders", "columns": [_col()],
    }
    base.update(kw)
    return base


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


# ── 1. the SQL-authoring DDL slice ─────────────────────────────────────────


def test_the_ddl_slice_carries_the_measurement():
    text = sl._format_slice([_table(columns=[_col(stats=FULL_STATS)])], [], 800)
    assert "实测(全表)" in text
    assert "distinct 3" in text, "a complete read reports an exact cardinality"
    assert "≥" not in text


def test_a_sampled_figure_keeps_its_provenance_in_the_ddl_slice():
    text = sl._format_slice(
        [_table(columns=[_col("amount", "decimal(14,2)", is_nullable=False,
                              stats=SAMPLED_STATS)])],
        [], 800,
    )
    assert "200 行抽样" in text, "the author must know this is a bound, not a total"
    assert "nulls 12%" in text
    assert "distinct ≥64" in text
    assert "range ≥221.98 … 8641379.31" in text


def test_the_ddl_slice_adds_nothing_for_an_unmeasured_column():
    text = sl._format_slice([_table(columns=[_col(stats=None)])], [], 800)
    assert "实测" not in text
    assert "-- 区域" in text, "the description still renders as before"


def test_the_ddl_slice_still_respects_the_token_budget():
    """Measurements are not allowed to blow a budget the loader depends on."""
    tables = [
        _table(table_name=f"t{i}", table_meta_id=f"m{i}",
               columns=[_col("amount", "decimal(14,2)", stats=SAMPLED_STATS)])
        for i in range(40)
    ]
    text = sl._format_slice(tables, [], 400)
    assert sl._estimate_tokens(text) <= 400 + 200, "budget overrun"
    assert text.count("CREATE TABLE") < 40, "the cap still cuts a long list"


def test_a_wide_table_is_never_dropped_for_its_measurements():
    """Live-caught: measured annotations doubled a 26-column ERP view's chunk,
    and the old loop break-ed on the first over-budget candidate — leaving the
    SQL author a slice with a header and NO DDL. The detail gives way first."""
    wide = _table(
        columns=[_col(f"c{i}", "decimal(14,2)", stats=SAMPLED_STATS) for i in range(40)]
    )
    text = sl._format_slice([wide], [], 200)      # far too small for 40 columns
    assert "CREATE TABLE orders" in text, "the author must always receive DDL"
    assert "c0" in text and "c39" in text, "every column is still declared"
    assert "实测" not in text, "the measurement is what gives way, not the table"


def test_a_later_table_that_does_not_fit_is_still_dropped():
    """The first-table guarantee must not turn into an unbounded slice."""
    first = _table(table_name="a", table_meta_id="m1")
    second = _table(
        table_name="b", table_meta_id="m2",
        columns=[_col(f"c{i}", "decimal(14,2)", stats=SAMPLED_STATS) for i in range(40)],
    )
    text = sl._format_slice([first, second], [], 150)
    assert "CREATE TABLE a" in text
    assert "CREATE TABLE b" not in text


def test_the_value_dictionary_gives_way_before_the_measurement():
    """Middle rung of the degradation: the long annotation goes first."""
    wide = _table(
        columns=[
            _col(f"c{i}", "varchar(64)", enum_values=[f"value{i}{j}" for j in range(12)],
                 stats=FULL_STATS)
            for i in range(12)
        ]
    )
    stats_only = sl._render_table_chunk(wide, detail="stats")
    # A budget that fits the measurement but not the value dictionary.
    text = sl._format_slice([wide], [], sl._estimate_tokens(stats_only) + 40)
    assert "实测(全表)" in text, "the distribution is the last thing to give way"
    assert "values:" not in text, "the value dictionary is the first"


def test_a_26_column_measured_table_keeps_its_measurement_at_the_default_budget():
    """The live ERP shape: at the old 800-token default the measurements were
    stripped off the exact view the question was about. At the shipped default
    (read back from the signature, so this test tracks the real value) they
    survive in full."""
    import inspect

    wide = _table(
        columns=[
            _col(f"c{i}", "decimal(14,2)", is_nullable=(i % 3 == 0), stats=SAMPLED_STATS)
            for i in range(26)
        ]
    )
    default_budget = inspect.signature(
        sl.get_selected_tables_ddl
    ).parameters["token_budget"].default
    assert default_budget >= 2000, "the measured detail must fit a wide ERP view"
    text = sl._format_slice([wide], [], default_budget)
    assert "CREATE TABLE orders" in text
    assert text.count("实测(200 行抽样)") == 26, "every column keeps its figures"
    assert "nulls 12%" in text and "range ≥221.98 … 8641379.31" in text


# ── 2. the table selector's line ───────────────────────────────────────────


def test_the_selector_line_carries_bounded_vocabulary():
    line = sl._toc_line({
        "table_name": "erp_v_sale_orderentry", "description": "销售订单明细",
        "row_count": 14589, "table_role": "fact",
        "sample_values": {
            "material_name": ["混三甲苯", "碳五石油树脂", "双环戊二烯", "异戊二烯", "他"],
            "region": ["华东", "华南", "华北", "华中"],
            "status": ["paid", "pending"],
            "amount": ["1", "2"],
        },
    })
    assert line.startswith("  erp_v_sale_orderentry"), "the name stays first"
    assert "values " in line
    assert "混三甲苯" in line, "the customer's own words must reach the selector"
    # bounded: 3 columns, 4 values each, 60 chars for the whole hint
    hint = next(p for p in line.split(", ") if p.startswith("values "))
    assert hint.count(":") <= 3
    assert "他" not in hint, "a 5th value is beyond the cap"
    assert len(hint) <= 60
    assert "amount" not in hint, "a 4th column is beyond the cap"


def test_the_selector_line_omits_the_hint_when_nothing_was_measured():
    line = sl._toc_line({
        "table_name": "orders", "description": "orders", "row_count": 6,
        "sample_values": None,
    })
    assert "values" not in line


def test_the_compact_selector_line_carries_a_tighter_hint():
    """The compact form keeps its size promise — it is not vocabulary-free.

    It used to be: only the ranked-candidate lines carried a hint. But the
    "rest of the catalog" block is where two ERP objects named alike sit
    (erp_t_sale_orderentry / erp_v_sale_orderentry), and the values that occur
    in them are the only thing that separates them, so the compact line gets a
    deliberately smaller hint instead of none.
    """
    row = {
        "table_name": "orders", "description": "o", "row_count": 6,
        "sample_values": {"region": ["华东", "华南", "华北", "华中"]},
    }
    full_line = sl._toc_line(row)
    short_line = sl._toc_line(row, compact=True)
    assert "华东" in short_line
    hint = next(p for p in short_line.split(", ") if p.startswith("values "))
    assert hint.count(":") == 1, "one column on a compact line"
    assert len(hint) <= 30, "the compact size promise holds"
    assert len(short_line) < len(full_line)


def test_the_toc_carries_the_measured_vocabulary_from_the_catalog(db, monkeypatch):
    kb = _kb_row(db)
    meta = _table_row(db, kb, table_name="erp_v_sale_orderentry")
    meta.sample_values = {"material_name": ["混三甲苯", "碳五石油树脂"]}
    db.commit()

    # Freshness probing would reach for the datasource; this test is about the
    # catalog fields, not about a live probe.
    from app.services.knowledge_graph import freshness
    monkeypatch.setattr(freshness, "annotate_tables_parallel", lambda *a, **k: None)
    monkeypatch.setattr(sl, "_resolve_table_allowlist_for_kb", lambda *a, **k: None)

    toc = sl.build_full_toc(db, [kb.id])
    assert toc and toc[0]["sample_values"] == {
        "material_name": ["混三甲苯", "碳五石油树脂"]
    }
    assert "混三甲苯" in sl._toc_line(toc[0])


# ── 3. the embedded documents (retrieval targets) ──────────────────────────


def test_the_embedded_table_document_carries_meaning_provenance_and_vocabulary():
    text = cat._table_doc_text(
        _table(
            row_count=14589, row_count_source="estimated",
            table_comment="销售订单明细",
            columns=[_col("material_name", "varchar(64)", stats=FULL_STATS)],
        )
    )
    assert text.startswith("[TABLE] orders")
    assert "销售订单" in text and "sales orders" in text
    assert "Rows: 14589 (估算)" in text, "an estimate is never shown as a count"
    assert "Comment: 销售订单明细" in text, "the source's own words are usable evidence"
    assert "Values — material_name: 华东, 华南, 华北" in text


def test_the_embedded_table_document_says_实测_for_an_exact_count():
    text = cat._table_doc_text(_table(row_count=6, row_count_source="exact"))
    assert "Rows: 6 (实测)" in text


def test_the_embedded_column_document_carries_the_measurement():
    text = cat._column_doc_text(
        "erp_t_pur_poorder",
        _col("FAMOUNT", "decimal(14,2)", comment="含税金额", stats=SAMPLED_STATS),
    )
    assert text.startswith("[COLUMN] erp_t_pur_poorder.FAMOUNT [decimal(14,2)]")
    assert "Comment: 含税金额" in text
    assert "Measured: 实测(200 行抽样):" in text
    assert "nulls 12%" in text and "distinct ≥64" in text


def test_an_unmeasured_column_document_advertises_nothing():
    text = cat._column_doc_text("orders", _col(stats=None, value_samples=None))
    assert "Measured:" not in text and "Values:" not in text
    assert "orders.region" in text, "the document itself still exists"


def test_the_column_document_falls_back_to_the_distinct_probe_samples():
    text = cat._column_doc_text(
        "orders", _col("status", "varchar(10)", value_samples=["paid", "pending"])
    )
    assert "Values: paid, pending" in text


def test_the_embedded_documents_stay_bounded():
    """A document is a retrieval target, not a data dump."""
    wide = _table(
        columns=[
            _col(f"c{i}", "varchar(64)", stats={
                "rows_sampled": 200, "basis": "sample", "null_rate": 0.0,
                "distinct": 5, "distinct_is_exact": False,
                "top_values": [{"value": f"v{i}-{j}" * 12, "count": 1} for j in range(6)],
            })
            for i in range(60)
        ]
    )
    text = cat._table_doc_text(wide)
    values_line = text.split("Values — ")[1]
    assert values_line.count(";") + 1 <= cat._EMBED_VOCAB_COLUMNS
    assert all(len(part) < 200 for part in values_line.split("; "))

    per_column = cat._column_doc_text(
        "orders", _col("c", "varchar(64)", stats=wide["columns"][0]["stats"])
    )
    assert len(per_column) < 500, "one column document must stay small"


# ── 4. the whole path: catalog rows -> the SQL author's prompt ─────────────


def _kb_row(db) -> KnowledgeBase:
    kb = KnowledgeBase(
        id=str(uuid.uuid4()), name="Warehouse", source_kind="database",
        db_type="mysql", status="active", catalog_status="ready",
    )
    db.add(kb)
    db.flush()
    return kb


def _table_row(db, kb, table_name="orders") -> KBTableMeta:
    meta = KBTableMeta(
        id=str(uuid.uuid4()), kb_id=kb.id, schema_name="",
        table_name=table_name, table_type="BASE TABLE", row_count=14589,
        row_count_source="estimated", table_role="fact",
        description_zh="销售订单明细",
    )
    db.add(meta)
    db.flush()
    return meta


def test_the_sql_author_sees_the_measurement_through_the_real_loader(db, monkeypatch):
    """The money path: persisted stats -> get_selected_tables_ddl text."""
    kb = _kb_row(db)
    meta = _table_row(db, kb)
    db.add_all([
        KBColumnMeta(
            id=str(uuid.uuid4()), table_meta_id=meta.id, column_name="FAMOUNT",
            ordinal=1, data_type="decimal(14,2)", is_nullable=True,
            is_primary_key=False, description_zh="含税金额",
            semantic_type="measure", stats_json=SAMPLED_STATS,
        ),
        KBColumnMeta(
            id=str(uuid.uuid4()), table_meta_id=meta.id, column_name="material_name",
            ordinal=2, data_type="varchar(64)", is_nullable=True,
            is_primary_key=False, description_zh="物料名称",
            semantic_type="dimension", stats_json=FULL_STATS,
            enum_values=["混三甲苯", "碳五石油树脂"],
        ),
        KBColumnMeta(
            id=str(uuid.uuid4()), table_meta_id=meta.id, column_name="unmeasured",
            ordinal=3, data_type="int", is_nullable=True,
            is_primary_key=False, description_zh="未测量",
            semantic_type="unknown", stats_json=None,
        ),
    ])
    db.commit()
    monkeypatch.setattr(sl, "_resolve_table_allowlist_for_kb", lambda *a, **k: None)

    ddl = sl.get_selected_tables_ddl(db, [kb.id], ["orders"])

    # measured columns carry their figures…
    assert "FAMOUNT" in ddl and "实测(200 行抽样)" in ddl and "nulls 12%" in ddl
    assert "material_name" in ddl and "实测(全表)" in ddl
    assert "values: 混三甲苯, 碳五石油树脂" in ddl, "the enum dictionary still renders"
    # …an unmeasured column contributes no figure at all, not a zero
    unmeasured_line = next(
        line for line in ddl.splitlines() if "unmeasured" in line
    )
    assert "实测" not in unmeasured_line
    assert "0%" not in unmeasured_line and "nulls" not in unmeasured_line


def test_the_loader_still_works_when_no_stats_were_collected(db, monkeypatch):
    """An old catalog row (pre-slice-2) must not break the prompt."""
    kb = _kb_row(db)
    meta = _table_row(db, kb)
    db.add(KBColumnMeta(
        id=str(uuid.uuid4()), table_meta_id=meta.id, column_name="id",
        ordinal=1, data_type="int", is_nullable=False, is_primary_key=True,
        description_zh="主键", semantic_type="identifier", stats_json=None,
    ))
    db.commit()
    monkeypatch.setattr(sl, "_resolve_table_allowlist_for_kb", lambda *a, **k: None)
    ddl = sl.get_selected_tables_ddl(db, [kb.id], ["orders"])
    assert "CREATE TABLE orders" in ddl and "实测" not in ddl


def test_vocabulary_hint_is_empty_for_a_table_without_measurements():
    assert cs.vocabulary_hint(None) == ""
    assert cs.vocabulary_hint({}) == ""
    assert cs.vocabulary_hint({"region": []}) == ""
    assert cs.vocabulary_hint({"region": ["华东"]}) == "values region: 华东"
