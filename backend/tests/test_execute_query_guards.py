"""execute_query is no longer an unguarded path to the warehouse.

Before this, the tool told the model "the LLM is trusted to write appropriate
SQL" and returned whatever came back: no faithfulness signal at all. An
unfiltered period or a truncated read looked exactly like a correct answer.
These tests pin the deterministic warnings that now ride along with the rows.
"""

from __future__ import annotations

from app.services.tool_handlers.db_tools import _post_execution_warnings

QUESTION = "contract execution status for last month"


def _result(rows, truncated=False):
    return {"rows": rows, "truncated": truncated, "sql": "SELECT 1"}


def test_empty_result_is_flagged():
    warnings = _post_execution_warnings({}, _result([]), "SELECT COUNT(*) FROM t")
    assert any("no rows returned" in w for w in warnings)


def test_truncation_is_flagged_with_the_row_count():
    warnings = _post_execution_warnings(
        {}, _result([{"i": 1}] * 10, truncated=True), "SELECT * FROM t",
    )
    assert any("truncated at 10 rows" in w for w in warnings)


def test_intent_triggers_the_contract_check():
    sql = "SELECT FDOCUMENTSTATUS, COUNT(*) n FROM erp_t_crm_contract GROUP BY FDOCUMENTSTATUS"
    result = _result([{"FDOCUMENTSTATUS": "C", "n": 177}])
    warnings = _post_execution_warnings({"question": QUESTION}, result, sql)
    assert any("verification" in w or "period" in w or "date filter" in w for w in warnings)
    assert result["contract_violations"] == ["period_not_applied"]


def test_no_intent_means_no_contract_check_but_cheap_checks_still_run():
    result = _result([])
    warnings = _post_execution_warnings({}, result, "SELECT 1 FROM t")
    assert any("no rows returned" in w for w in warnings)
    assert "contract_violations" not in result


def test_a_correct_query_gets_no_contract_complaint():
    sql = ("SELECT FDOCUMENTSTATUS, COUNT(*) n FROM erp_t_crm_contract "
           "WHERE FDATE >= '2026-08-01' GROUP BY FDOCUMENTSTATUS")
    result = _result([{"FDOCUMENTSTATUS": "C", "n": 176}])
    warnings = _post_execution_warnings({"question": QUESTION}, result, sql)
    assert warnings == []
    assert "contract_violations" not in result


def test_a_guard_failure_never_breaks_the_tool_result():
    """Even with an unusable payload the helper returns, it does not raise."""
    assert isinstance(_post_execution_warnings({"question": "x"}, {}, "SELECT 1"), list)
    assert isinstance(_post_execution_warnings({}, {"rows": None}, ""), list)
