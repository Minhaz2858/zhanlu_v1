"""Catalog description completeness — blank columns get a second, narrower pass.

Evidence this covers (live KB ``demo_warehouse_prod``, 2026-09-18):

    select count(*) filter (where description_en = '') from kb_column_meta;  -> 1361 / 2678

and the four status columns of ``erp_t_crm_contract`` were all among them:

    FCLOSESTATUS    dimension  ["A","B"]                 description ''
    FDOCUMENTSTATUS dimension  ["C","D","A","Z","B"]     description ''

With no recorded meaning, "contract execution status" resolved to a different
column on two consecutive runs (FCLOSESTATUS, then FDOCUMENTSTATUS). Blank is
not "unknown meaning" — the batch prompt tells the model to write 未知 for that;
blank means the model's JSON omitted the column, which an 8-table x 60-column
ERP batch does routinely. These tests pin the repair pass.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.knowledge_graph import catalog_indexer as ci


def _col(name, zh="", en=""):
    return {
        "column_name": name,
        "data_type": "varchar",
        "is_primary_key": False,
        "is_nullable": True,
        "description_zh": zh,
        "description_en": en,
    }


def _table(name, cols, zh="表说明", en="table description"):
    return {
        "table_name": name,
        "table_type": "TABLE",
        "row_count": 10,
        "columns": cols,
        "description_zh": zh,
        "description_en": en,
    }


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch):
    # Patch the INSTANCE, not the class — pydantic Settings class-level patches
    # are a silent no-op.
    monkeypatch.setattr(ci.settings, "CATALOG_DESC_BACKFILL_ENABLED", True, raising=False)
    monkeypatch.setattr(ci.settings, "CATALOG_DESC_BACKFILL_MAX_TABLES", 60, raising=False)


# ---------------------------------------------------------------------------
# Target detection
# ---------------------------------------------------------------------------

def test_blank_columns_are_targets_and_described_ones_are_not():
    complete = _table("ok_table", [_col("FID", "主键", "primary key")])
    blank_col = _table("erp_t_crm_contract", [
        _col("FCLOSESTATUS"),
        _col("FDOCUMENTSTATUS"),
        _col("FBILLNO", "单据编号", "bill number"),
    ])

    targets = ci._missing_description_targets([complete, blank_col])
    assert len(targets) == 1
    table, cols, table_blank = targets[0]
    assert table["table_name"] == "erp_t_crm_contract"
    assert [c["column_name"] for c in cols] == ["FCLOSESTATUS", "FDOCUMENTSTATUS"]
    assert table_blank is False


def test_a_table_whose_own_description_is_blank_is_a_target_even_without_columns():
    t = _table("erp_unknown", [_col("FID", "主键", "primary key")], zh="", en="")
    targets = ci._missing_description_targets([t])
    assert len(targets) == 1
    _, cols, table_blank = targets[0]
    assert cols == []
    assert table_blank is True


def test_whitespace_only_description_counts_as_blank():
    t = _table("t", [_col("A", "   ", "\n")])
    assert len(ci._missing_description_targets([t])) == 1


# ---------------------------------------------------------------------------
# The repair pass itself
# ---------------------------------------------------------------------------

def test_backfill_writes_the_missing_descriptions_in_place(monkeypatch):
    """Column dicts are shared by reference — the originals must be filled."""
    blank = _col("FDOCUMENTSTATUS")
    described = _col("FBILLNO", "单据编号", "bill number")
    table = _table("erp_t_crm_contract", [blank, described])

    import app.services.llm_service as llm_service

    calls = []

    async def _fake(messages, **kwargs):
        calls.append(messages[-1]["content"])
        return {
            "data": {
                "tables": [{
                    "table_name": "erp_t_crm_contract",
                    "description_zh": "",
                    "description_en": "",
                    "columns": [
                        {"column_name": "FDOCUMENTSTATUS",
                         "description_zh": "单据状态（审核状态）",
                         "description_en": "document status (approval state)"},
                    ],
                }]
            }
        }

    monkeypatch.setattr(llm_service, "call_llm", _fake)

    asyncio.run(ci._backfill_missing_descriptions([table]))

    assert blank["description_zh"] == "单据状态（审核状态）"
    assert blank["description_en"] == "document status (approval state)"
    assert described["description_zh"] == "单据编号"       # untouched
    assert len(calls) == 1
    # Only the blank column was re-asked.
    assert "FDOCUMENTSTATUS" in calls[0]
    assert "FBILLNO" not in calls[0]


def test_backfill_copies_back_a_table_level_description(monkeypatch):
    table = _table("erp_t_crm_contract",
                   [_col("FCLOSESTATUS")], zh="", en="")
    import app.services.llm_service as llm_service

    async def _fake(messages, **kwargs):
        return {
            "data": {
                "tables": [{
                    "table_name": "erp_t_crm_contract",
                    "description_zh": "销售合同主表",
                    "description_en": "sales contract master",
                    "columns": [
                        {"column_name": "FCLOSESTATUS",
                         "description_zh": "关闭状态", "description_en": "close status"},
                    ],
                }]
            }
        }

    monkeypatch.setattr(llm_service, "call_llm", _fake)
    asyncio.run(ci._backfill_missing_descriptions([table]))

    assert table["description_zh"] == "销售合同主表"
    assert table["description_en"] == "sales contract master"
    assert table["columns"][0]["description_en"] == "close status"


def test_table_description_only_repair_does_not_touch_columns(monkeypatch):
    """A table whose columns are all described but whose own description is blank.

    The first version passed an empty column list, the model answered with
    nothing, and ten tables stayed incomplete on the live KB. The repair now
    hands over the column names as context and asks for the table alone.
    """
    described = _col("FID", "主键", "primary key")
    table = _table("erp_t_crm_contractentry_f", [described], zh="", en="")
    import app.services.llm_service as llm_service

    seen = []

    async def _fake(messages, **kwargs):
        seen.append(messages[-1]["content"])
        return {
            "data": {
                "tables": [{
                    "table_name": "erp_t_crm_contractentry_f",
                    "description_zh": "销售合同明细（财务信息）",
                    "description_en": "sales contract lines (financial)",
                    "columns": [],          # nothing about columns, by instruction
                }]
            }
        }

    monkeypatch.setattr(llm_service, "call_llm", _fake)
    asyncio.run(ci._backfill_missing_descriptions([table]))

    assert table["description_zh"] == "销售合同明细（财务信息）"
    assert table["description_en"] == "sales contract lines (financial)"
    assert described["description_zh"] == "主键"     # untouched
    assert "FID" in seen[0]                            # names given as context


def test_table_description_only_repair_keeps_columns_when_model_volunteers_them(monkeypatch):
    """Belt and braces: the columns path must not run for a table-only repair."""
    import app.services.llm_service as llm_service

    described = _col("FID", "主键", "primary key")
    table = _table("t", [described], zh="", en="")

    async def _fake(messages, **kwargs):
        return {
            "data": {
                "tables": [{
                    "table_name": "t",
                    "description_zh": "表", "description_en": "table",
                    "columns": [{"column_name": "FID", "description_zh": "覆盖",
                                 "description_en": "overwritten"}],
                }]
            }
        }

    monkeypatch.setattr(llm_service, "call_llm", _fake)
    asyncio.run(ci._backfill_missing_descriptions([table]))

    assert described["description_en"] == "primary key"


def test_complete_catalog_makes_no_llm_call(monkeypatch):
    import app.services.llm_service as llm_service

    async def _explode(*a, **k):
        raise AssertionError("no call expected when every description is present")

    monkeypatch.setattr(llm_service, "call_llm", _explode)
    table = _table("erp_t_crm_contract", [_col("FID", "主键", "primary key")])
    asyncio.run(ci._backfill_missing_descriptions([table]))  # must not raise


def test_backfill_is_capped(monkeypatch):
    import app.services.llm_service as llm_service

    monkeypatch.setattr(ci.settings, "CATALOG_DESC_BACKFILL_MAX_TABLES", 1, raising=False)
    seen = []

    async def _fake(messages, **kwargs):
        seen.append(messages[-1]["content"])
        return {"data": {"tables": [{"table_name": "x", "description_zh": "d",
                                     "description_en": "d", "columns": []}]}}

    monkeypatch.setattr(llm_service, "call_llm", _fake)
    tables = [_table(f"t{i}", [_col("C")]) for i in range(3)]
    asyncio.run(ci._backfill_missing_descriptions(tables))

    assert len(seen) == 1


@pytest.mark.parametrize("flag,budget", [(False, 60), (True, 0)])
def test_backfill_can_be_switched_off(monkeypatch, flag, budget):
    import app.services.llm_service as llm_service

    monkeypatch.setattr(ci.settings, "CATALOG_DESC_BACKFILL_ENABLED", flag, raising=False)
    monkeypatch.setattr(ci.settings, "CATALOG_DESC_BACKFILL_MAX_TABLES", budget, raising=False)

    async def _explode(*a, **k):
        raise AssertionError("disabled backfill must not call the model")

    monkeypatch.setattr(llm_service, "call_llm", _explode)
    asyncio.run(ci._backfill_missing_descriptions([_table("t", [_col("C")])]))


def test_one_failed_backfill_does_not_abort_the_others(monkeypatch):
    import app.services.llm_service as llm_service

    good = _table("good", [_col("A")])
    bad = _table("bad", [_col("B")])

    async def _fake(messages, **kwargs):
        prompt = messages[-1]["content"]
        if "表名: bad" in prompt:
            raise RuntimeError("upstream 500")
        return {
            "data": {
                "tables": [{
                    "table_name": "good",
                    "description_zh": "d", "description_en": "d",
                    "columns": [{"column_name": "A", "description_zh": "甲",
                                 "description_en": "alpha"}],
                }]
            }
        }

    monkeypatch.setattr(llm_service, "call_llm", _fake)
    asyncio.run(ci._backfill_missing_descriptions([good, bad]))

    assert good["columns"][0]["description_en"] == "alpha"
    assert bad["columns"][0]["description_en"] == ""   # blank, not silently "unknown"
