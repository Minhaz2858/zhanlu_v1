"""NL2SQL runtime repair — a query the DATABASE rejected must come back fixed.

The failure this covers (2026-09-18, live): "Present the contract execution
status for last month using charts" produced

    (1054, "Unknown column 'state' in 'field list'")

The model invented `state`, MySQL rejected the query, and the turn ended by
asking the user to name the real column. The database had already said exactly
what was wrong — nobody fed that back. These tests pin the repair loop.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services.db import nl_answer_service as nls
from app.services.db.nl_answer_service import (
    NLAnswerService,
    _columns_by_table,
    _is_resolvable_db_error,
    _missing_identifier_from_error,
    _referenced_tables,
)

MYSQL_1054 = '(1054, "Unknown column \'state\' in \'field list\'")'
MYSQL_1146 = "(1146, \"Table 'erp.t_crm_contract' doesn't exist\")"
PG_42703 = 'column "state" does not exist'


# ---------------------------------------------------------------------------
# Error classification: only naming/syntax failures are the model's to fix.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "error,expected",
    [
        (MYSQL_1054, True),
        (MYSQL_1146, True),
        (PG_42703, True),
        ("Invalid column name 'state'.", True),
        ("no such column: state", True),
        ("(1064, \"You have an error in your SQL syntax near 'FROM'\")", True),
        ("(1045, \"Access denied for user 'ro'@'host'\")", False),
        ("(1205, 'Lock wait timeout exceeded')", False),
        ("(2003, \"Can't connect to MySQL server on '10.0.0.1'\")", False),
        ("Query timed out after 10 seconds", False),
        ("", False),
    ],
)
def test_error_classification(error, expected):
    assert _is_resolvable_db_error(error) is expected


@pytest.mark.parametrize(
    "error,expected",
    [
        (MYSQL_1054, "state"),
        (MYSQL_1146, "erp.t_crm_contract"),
        (PG_42703, "state"),
        ("Invalid column name 'auditstatus'.", "auditstatus"),
        ("no such column: contractno", "contractno"),
        ("(1045, \"Access denied\")", None),
    ],
)
def test_missing_identifier_extraction(error, expected):
    assert _missing_identifier_from_error(error) == expected


def test_referenced_tables_normalizes_qualifiers():
    sql = "SELECT COUNT(*) FROM erp_t_crm_contract c JOIN `erp`.`t_crm_line` l ON l.id=c.id"
    tables = _referenced_tables(sql)
    assert any(t.lower().endswith("erp_t_crm_contract") for t in tables)
    assert all("`" not in t for t in tables)


def test_columns_by_table_reads_describe_all_shape():
    schema_info = {
        "tables": [
            {"table": "erp_t_crm_contract", "columns": [{"name": "contractid"}, {"name": "auditstatus"}]},
            {"table": "empty", "columns": []},
        ]
    }
    assert _columns_by_table(schema_info) == {
        "erp_t_crm_contract": ["contractid", "auditstatus"]
    }


# ---------------------------------------------------------------------------
# The repair prompt carries the ground truth
# ---------------------------------------------------------------------------

class _FakeQuery:
    """execute() rejects anything naming `state`, accepts the repaired SQL."""

    def __init__(self, db=None):
        self.calls: list[str] = []

    def execute(self, kb_id, sql):
        self.calls.append(sql)
        if "state" in sql:
            raise Exception(MYSQL_1054)
        return {"rows": [{"auditstatus": "signed", "n": 3}], "sql": sql}


class _FakeSchema:
    def __init__(self, db=None):
        pass

    def describe_table(self, kb_id, table):
        return {"columns": [{"name": "contractid"}, {"name": "auditstatus"}]}


def _prepare(monkeypatch, replies: list[str]):
    """Wire a bare NLAnswerService whose only real actor is the repair loop."""
    for flag, value in (
        ("KG_FRESHNESS_SHORTCIRCUIT_ENABLED", False),
        ("SCHEMA_LINKING_ENABLED", False),
        ("SEMANTIC_CATALOG_ENABLED", False),
        ("SCHEMA_GRAPH_SQL_VALIDATOR_ENABLED", False),
    ):
        assert hasattr(settings, flag), f"{flag} must exist on Settings"
        monkeypatch.setattr(settings, flag, value)

    # STATIC_QUERY_ROUTING_ENABLED is read with a getattr default and is not a
    # declared field — disable the resolver itself instead of the flag.
    from app.services.db import query_router
    monkeypatch.setattr(query_router, "resolve_static_route", lambda *a, **k: None, raising=False)

    monkeypatch.setattr(nls, "QueryService", _FakeQuery)
    monkeypatch.setattr(nls, "SchemaService", _FakeSchema)
    monkeypatch.setattr(nls, "_shadow_validate_and_log", lambda *a, **k: None)

    # The readiness gate is pinned by its own suite (tests/test_source_readiness*);
    # these tests isolate the REPAIR loop, and this bare harness has no KB row at
    # all (db=None), so the gate would refuse every one of them before the repair
    # could run. Stub the verdict as ready — the source is not what is under test.
    from app.services.db import readiness as _readiness

    class _Ready:
        allowed = True
        limited = False
        state = "nl2sql_ready"
        code = "ready"
        disclosure = ""
        detail = ""
        source_id = ""

    monkeypatch.setattr(
        _readiness, "check_source", lambda db, kb_id: (_Ready(), None)
    )

    svc = NLAnswerService(db=None)

    async def _schema_fallback(kb_id, question):
        return (
            {"tables": [{"table": "erp_t_crm_contract", "columns": [
                {"name": "contractid", "type": "varchar", "nullable": False, "pk": True},
                {"name": "auditstatus", "type": "varchar", "nullable": True, "pk": False},
            ]}]},
            "TABLE erp_t_crm_contract ( contractid varchar, auditstatus varchar )",
        )

    monkeypatch.setattr(svc, "_build_m_schema_fallback", _schema_fallback)

    async def _text_to_sql(question, schema_info, schema_text=None, project_id=None, endpoint=None, extra_guidance=None):
        return replies.pop(0) if replies else None

    monkeypatch.setattr(svc, "_text_to_sql", _text_to_sql)
    return svc


def test_answer_repairs_unknown_column_and_returns_rows(monkeypatch):
    svc = _prepare(monkeypatch, [
        "SELECT auditstatus, COUNT(*) AS n FROM erp_t_crm_contract GROUP BY state",
        "SELECT auditstatus, COUNT(*) AS n FROM erp_t_crm_contract GROUP BY auditstatus",
    ])
    captured: dict = {}

    async def _capture_correction(question, schema_info, schema_text=None, project_id=None,
                                  errors=None, available_columns=None,
                                  available_suggestions=None, endpoint=None):
        captured["errors"] = errors
        captured["columns"] = available_columns
        return "SELECT auditstatus, COUNT(*) AS n FROM erp_t_crm_contract GROUP BY auditstatus"

    monkeypatch.setattr(svc, "_text_to_sql_with_correction", _capture_correction)

    result = asyncio.run(svc.answer("kb-erp", "contract execution status last month", skip_narrate=True))

    assert result["success"] is True, result
    assert result["rows"] == [{"auditstatus": "signed", "n": 3}]
    assert "state" not in result["sql"]
    assert "auditstatus" in result["sql"]
    assert result["repaired_from_error"] == MYSQL_1054

    # The repair prompt must carry the driver's own words and the real columns.
    assert captured["errors"] and MYSQL_1054 in captured["errors"][0]
    assert any("state" in e and "does not exist" in e for e in captured["errors"])
    assert captured["columns"]["erp_t_crm_contract"] == ["contractid", "auditstatus"]


def test_answer_does_not_loop_on_non_naming_failures(monkeypatch):
    svc = _prepare(monkeypatch, ["SELECT COUNT(*) AS n FROM erp_t_crm_contract"])

    class _DeniedQuery(_FakeQuery):
        def execute(self, kb_id, sql):
            self.calls.append(sql)
            raise Exception("(1045, \"Access denied for user 'ro'@'host'\")")

    monkeypatch.setattr(nls, "QueryService", _DeniedQuery)

    async def _never_called(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("repair must not run for a permission failure")

    monkeypatch.setattr(svc, "_text_to_sql_with_correction", _never_called)

    result = asyncio.run(svc.answer("kb-erp", "revenue", skip_narrate=True))
    assert result["success"] is False
    assert "Access denied" in result["error"]
    assert result["sql_repaired"] is False


def test_repair_gives_up_gracefully_when_model_repeats_itself(monkeypatch):
    svc = _prepare(monkeypatch, [
        "SELECT COUNT(*) AS n FROM erp_t_crm_contract GROUP BY state",
    ])

    async def _same_sql(question, schema_info, schema_text=None, project_id=None,
                        errors=None, available_columns=None,
                        available_suggestions=None, endpoint=None):
        return "SELECT COUNT(*) AS n FROM erp_t_crm_contract GROUP BY state"

    monkeypatch.setattr(svc, "_text_to_sql_with_correction", _same_sql)

    result = asyncio.run(svc.answer("kb-erp", "contract status", skip_narrate=True))
    assert result["success"] is False
    assert "Unknown column" in result["error"]
    assert result["sql_repaired"] is False


def test_repair_falls_back_to_live_introspection_when_schema_is_thin(monkeypatch):
    svc = _prepare(monkeypatch, [
        "SELECT COUNT(*) AS n FROM erp_t_crm_contract GROUP BY state",
    ])

    async def _schema_fallback(kb_id, question):
        return {"tables": []}, ""

    monkeypatch.setattr(svc, "_build_m_schema_fallback", _schema_fallback)

    captured: dict = {}

    async def _capture(question, schema_info, schema_text=None, project_id=None,
                       errors=None, available_columns=None,
                       available_suggestions=None, endpoint=None):
        captured["columns"] = available_columns
        return "SELECT COUNT(*) AS n FROM erp_t_crm_contract"

    monkeypatch.setattr(svc, "_text_to_sql_with_correction", _capture)

    result = asyncio.run(svc.answer("kb-erp", "contract status", skip_narrate=True))
    assert result["success"] is True, result
    # describe_table() supplied the columns because the TOC selection was empty.
    assert captured["columns"]["erp_t_crm_contract"] == ["contractid", "auditstatus"]
