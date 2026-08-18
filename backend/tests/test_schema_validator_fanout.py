"""Tests for the NL2SQL JOIN fan-out guard (Fix 3, 2026-08-18).

Three layers:

1. ``_has_fanout_risk`` — pure structural heuristic: a measure SUM over a
   JOIN with no pre-aggregating CTE/subquery on either side → warning.
2. ``validate_against_schema`` — the ``warnings`` key is present in the return
   dict and never flips ``is_valid`` (errors-vs-warnings semantics).
3. ``_correct_sql_with_validator_feedback`` — warnings trigger the one-shot
   correction only when ``NL2SQL_FANOUT_GUARD_ENABLED``; hard errors always do.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlglot import parse_one

from app.config import settings
from app.services.nl2sql.schema_validator import (
    _has_fanout_risk,
    validate_against_schema,
)
from app.services.db.nl_answer_service import NLAnswerService


# ── pure heuristic: _has_fanout_risk ─────────────────────────────────────


def test_raw_measure_sum_join_warns():
    parsed = parse_one(
        "SELECT f.material_id, SUM(f.amount) FROM fact f "
        "JOIN snapshot s ON f.material_id = s.material_id "
        "GROUP BY f.material_id"
    )
    warning = _has_fanout_risk(parsed)
    assert warning is not None
    assert "fan-out" in warning


def test_cte_preaggregation_no_warning():
    parsed = parse_one(
        "WITH pre AS (SELECT material_id, SUM(amount) AS total FROM fact "
        "GROUP BY material_id) "
        "SELECT f.material_id, pre.total FROM fact f "
        "JOIN pre ON f.material_id = pre.material_id"
    )
    assert _has_fanout_risk(parsed) is None


def test_subquery_preaggregation_no_warning():
    parsed = parse_one(
        "SELECT a.material_id, a.total FROM fact a JOIN "
        "(SELECT material_id, SUM(amount) AS total FROM fact GROUP BY material_id) b "
        "ON a.material_id = b.material_id"
    )
    assert _has_fanout_risk(parsed) is None


def test_sum_over_non_measure_column_no_warning():
    parsed = parse_one(
        "SELECT f.material_id, SUM(f.material_id) FROM fact f "
        "JOIN snapshot s ON f.material_id = s.material_id "
        "GROUP BY f.material_id"
    )
    assert _has_fanout_risk(parsed) is None


def test_single_table_sum_no_warning():
    parsed = parse_one(
        "SELECT material_id, SUM(amount) FROM fact GROUP BY material_id"
    )
    assert _has_fanout_risk(parsed) is None


# ── validate_against_schema: warnings key + errors-vs-warnings semantics ──


def _make_db(kb=None):
    db = MagicMock()
    q = MagicMock()
    q.filter.return_value.first.return_value = kb
    db.query.return_value = q
    return db


def _kb(db_type="mysql"):
    kb = MagicMock()
    kb.id = "kb1"
    kb.db_type = db_type
    return kb


def _describe(table_columns):
    svc = MagicMock()

    def _desc(kb_id, table):
        return {"columns": [{"name": c} for c in table_columns.get(table, [])]}

    svc.describe_table.side_effect = _desc
    return svc


def test_validate_warning_does_not_flip_is_valid():
    db = _make_db(kb=_kb())
    svc = _describe({
        "fact": ["material_id", "amount"],
        "snapshot": ["material_id", "snap_date"],
    })
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT f.material_id, SUM(f.amount) FROM fact f "
            "JOIN snapshot s ON f.material_id = s.material_id GROUP BY f.material_id",
            "kb1", db,
        )
    assert result["is_valid"] is True
    assert result["errors"] == []
    assert any("fan-out" in w for w in result.get("warnings", []))


def test_validate_cte_preagg_no_warning():
    db = _make_db(kb=_kb())
    svc = _describe({
        "fact": ["material_id", "amount"],
    })
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "WITH pre AS (SELECT material_id, SUM(amount) AS total FROM fact "
            "GROUP BY material_id) SELECT material_id, total FROM pre",
            "kb1", db,
        )
    assert result["is_valid"] is True
    assert result.get("warnings", []) == []


def test_validate_errors_still_invalidate_with_warnings_key():
    db = _make_db(kb=_kb())
    svc = _describe({
        "fact": ["material_id", "amount"],
        "snapshot": ["material_id", "snap_date"],
    })
    with patch("app.services.nl2sql.schema_validator.SchemaService", return_value=svc):
        result = validate_against_schema(
            "SELECT f.material_id, SUM(f.bogus_col) FROM fact f "
            "JOIN snapshot s ON f.material_id = s.material_id GROUP BY f.material_id",
            "kb1", db,
        )
    assert result["is_valid"] is False
    assert any("bogus_col" in e for e in result["errors"])
    # warnings key is always present (may be empty)
    assert "warnings" in result


# ── wiring: _correct_sql_with_validator_feedback ─────────────────────────


def _make_service():
    svc = NLAnswerService.__new__(NLAnswerService)
    svc._db = MagicMock()
    return svc


def _vres(errors=None, warnings=None):
    return {
        "is_valid": not errors,
        "errors": errors or [],
        "warnings": warnings or [],
        "available_columns": {"fact": ["material_id", "amount"]},
    }


@pytest.fixture(autouse=True)
def _enable_flags(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", True)


@pytest.mark.asyncio
async def test_correction_triggered_by_warnings_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", True)
    service = _make_service()
    with patch.object(
        NLAnswerService, "_text_to_sql_with_correction", new=AsyncMock(return_value="FIXED SQL")
    ) as m, patch(
        "app.services.nl2sql.schema_validator.validate_against_schema",
        return_value=_vres(warnings=["potential fan-out ..."]),
    ):
        sql, corrected = await service._correct_sql_with_validator_feedback(
            "RAW SQL", "total sales", "kb1", {"tables": {}},
            schema_text="ctx", project_id=None, endpoint=None,
        )
    assert corrected is True
    assert sql == "FIXED SQL"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_correction_skipped_by_warnings_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", False)
    service = _make_service()
    with patch.object(
        NLAnswerService, "_text_to_sql_with_correction", new=AsyncMock(return_value="FIXED SQL")
    ) as m, patch(
        "app.services.nl2sql.schema_validator.validate_against_schema",
        return_value=_vres(warnings=["potential fan-out ..."]),
    ):
        sql, corrected = await service._correct_sql_with_validator_feedback(
            "RAW SQL", "total sales", "kb1", {"tables": {}},
            schema_text="ctx", project_id=None, endpoint=None,
        )
    assert corrected is False
    assert sql == "RAW SQL"
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_triggered_by_errors_regardless_of_flag(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", False)
    service = _make_service()
    with patch.object(
        NLAnswerService, "_text_to_sql_with_correction", new=AsyncMock(return_value="FIXED SQL")
    ) as m, patch(
        "app.services.nl2sql.schema_validator.validate_against_schema",
        return_value=_vres(errors=["column 'x' not found"]),
    ):
        sql, corrected = await service._correct_sql_with_validator_feedback(
            "RAW SQL", "total sales", "kb1", {"tables": {}},
            schema_text="ctx", project_id=None, endpoint=None,
        )
    assert corrected is True
    assert sql == "FIXED SQL"
    m.assert_awaited_once()


@pytest.mark.asyncio
async def test_correction_skipped_when_validator_disabled(monkeypatch):
    monkeypatch.setattr(settings, "SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False)
    monkeypatch.setattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", True)
    service = _make_service()
    with patch.object(
        NLAnswerService, "_text_to_sql_with_correction", new=AsyncMock()
    ) as m:
        sql, corrected = await service._correct_sql_with_validator_feedback(
            "RAW SQL", "total sales", "kb1", {"tables": {}},
            schema_text="ctx", project_id=None, endpoint=None,
        )
    assert corrected is False
    assert sql == "RAW SQL"
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_correction_not_replaced_when_identical(monkeypatch):
    monkeypatch.setattr(settings, "NL2SQL_FANOUT_GUARD_ENABLED", True)
    service = _make_service()
    with patch.object(
        NLAnswerService, "_text_to_sql_with_correction", new=AsyncMock(return_value="RAW SQL")
    ) as m, patch(
        "app.services.nl2sql.schema_validator.validate_against_schema",
        return_value=_vres(warnings=["potential fan-out ..."]),
    ):
        sql, corrected = await service._correct_sql_with_validator_feedback(
            "RAW SQL", "total sales", "kb1", {"tables": {}},
            schema_text="ctx", project_id=None, endpoint=None,
        )
    assert corrected is False
    assert sql == "RAW SQL"
    m.assert_awaited_once()
