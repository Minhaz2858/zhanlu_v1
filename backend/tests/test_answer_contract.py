"""Deterministic question↔SQL contract: the failure the other guards miss.

Live context (2026-09-18, aipdp_data_warehouse_prod): "Present the contract
execution status for last month using charts" was answered by a query the
database rejected, then by one that picked an arbitrary status column. The
guards on the data path all react to FAILURE — 0 rows, degenerate rows, a
driver error, a structural validation error. A query that *succeeds* while
answering a different question (all-time instead of last month; one scalar
instead of a breakdown) is executed, narrated, and shown as authoritative.

That is what this module detects. Every rule fires only on a pattern that is
unambiguously wrong, so a clean verdict means "no contradiction found", never
"verified".
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.db.answer_contract import (
    check_answer_contract,
    question_period,
    sql_has_date_predicate,
    sql_is_grouped,
)

LAST_MONTH_SQL = (
    "SELECT FDOCUMENTSTATUS, COUNT(*) AS n FROM erp_t_crm_contract "
    "WHERE FDATE >= '2026-08-01' AND FDATE < '2026-09-01' "
    "GROUP BY FDOCUMENTSTATUS"
)


# ---------------------------------------------------------------------------
# Period resolution — one parser, borrowed from the shared resolver
# ---------------------------------------------------------------------------

def test_relative_period_is_resolved():
    got = question_period("contract execution status for last month", date(2026, 9, 18))
    assert got is not None
    window, _ = got
    assert window[0] == date(2026, 8, 1)
    assert window[1] == date(2026, 8, 31)


def test_explicit_month_is_resolved():
    got = question_period("sales in 2026-07 by customer", date(2026, 9, 18))
    assert got is not None
    window, _ = got
    assert window == (date(2026, 7, 1), date(2026, 7, 31))


def test_no_period_means_no_constraint():
    assert question_period("list all customers", date(2026, 9, 18)) is None


def test_december_month_rolls_the_year():
    got = question_period("revenue 2025-12", date(2026, 1, 5))
    assert got is not None
    window, _ = got
    assert window == (date(2025, 12, 1), date(2025, 12, 31))


# ---------------------------------------------------------------------------
# Period rule
# ---------------------------------------------------------------------------

def test_period_named_but_sql_unfiltered_is_a_violation():
    report = check_answer_contract(
        "contract execution status for last month",
        "SELECT FDOCUMENTSTATUS, COUNT(*) AS n FROM erp_t_crm_contract GROUP BY FDOCUMENTSTATUS",
        [{"FDOCUMENTSTATUS": "C", "n": 177}],
    )
    assert report.violations == ["period_not_applied"]
    assert report.ok is False
    assert "2026-08" in report.detail["window"][0]


def test_period_named_and_sql_filtered_is_clean():
    report = check_answer_contract(
        "contract execution status for last month", LAST_MONTH_SQL,
        [{"FDOCUMENTSTATUS": "C", "n": 176}],
    )
    assert report.ok


def test_between_and_year_functions_count_as_date_predicates():
    assert sql_has_date_predicate("SELECT 1 FROM t WHERE d BETWEEN '2026-08-01' AND '2026-08-31'")
    assert sql_has_date_predicate("SELECT 1 FROM t WHERE YEAR(d) = 2026")
    assert sql_has_date_predicate("SELECT 1 FROM t WHERE d >= '2026-08-01'")
    assert not sql_has_date_predicate("SELECT COUNT(*) FROM t")
    assert not sql_has_date_predicate("SELECT * FROM t WHERE status = 'C'")


# The exact SQL the live data agent produced on 2026-09-18 for
# "Present the contract execution status for last month using charts." — a
# CORRECT query whose date filter is a function call, not a literal. The first
# version of this checker flagged it as unfiltered: a false positive that would
# have made every correct answer look wrong.
LIVE_SQL = (
    "SELECT\n  FID,\n  FNAME,\n  FDOCUMENTSTATUS,\n  FDATE,\n  FCLOSESTATUS,\n  FCANCELSTATUS\n"
    "FROM erp_t_crm_contract\n"
    "WHERE FDATE >= DATE_FORMAT(CURRENT_DATE - INTERVAL 1 MONTH, '%Y-%m-01')\n"
    "  AND FDATE < DATE_FORMAT(CURRENT_DATE, '%Y-%m-01')\n"
    "ORDER BY FDATE"
)
LIVE_QUESTION = "Present the contract execution status for last month using charts."


def test_relative_window_written_as_sql_functions_is_not_a_violation():
    report = check_answer_contract(LIVE_QUESTION, LIVE_SQL, [{"FID": 122079}])
    assert "period_not_applied" not in report.violations, report.violations
    # The same query DOES miss the requested grain: a chart request answered
    # with 177 individual contract rows and no GROUP BY.
    assert "grain_mismatch" in report.violations


@pytest.mark.parametrize("sql", [
    "SELECT 1 FROM t WHERE d >= DATE_FORMAT(CURRENT_DATE - INTERVAL 1 MONTH, '%Y-%m-01')",
    "SELECT 1 FROM t WHERE d >= CURDATE() - INTERVAL 30 DAY",
    "SELECT 1 FROM t WHERE d >= DATE_SUB(NOW(), INTERVAL 7 DAY)",
    "SELECT 1 FROM t WHERE DATE_FORMAT(d,'%Y-%m') = '2026-08'",
    "SELECT 1 FROM t WHERE d <= CURRENT_DATE",
])
def test_function_based_date_predicates_are_recognised(sql):
    assert sql_has_date_predicate(sql), sql


def test_question_without_period_is_not_constrained():
    report = check_answer_contract(
        "how many contracts are there", "SELECT COUNT(*) AS n FROM erp_t_crm_contract",
        [{"n": 177}],
    )
    assert report.ok


# ---------------------------------------------------------------------------
# Grain rule
# ---------------------------------------------------------------------------

def test_breakdown_requested_but_scalar_returned_is_a_violation():
    report = check_answer_contract(
        "sales by customer for last month",
        "SELECT SUM(FALLAMOUNT) AS total FROM erp_t_sal_orderentry WHERE FDATE >= '2026-08-01'",
        [{"total": 12345}],
    )
    assert "grain_mismatch" in report.violations


def test_breakdown_requested_and_grouped_is_clean():
    report = check_answer_contract(
        "sales by customer for last month",
        "SELECT FCUSTOMERID, SUM(FALLAMOUNT) t FROM erp_t_sal_orderentry "
        "WHERE FDATE >= '2026-08-01' GROUP BY FCUSTOMERID",
        [{"FCUSTOMERID": 1, "t": 10}],
    )
    assert report.ok


def test_chinese_breakdown_cues_are_recognised():
    for q in ("按客户统计上月销量", "各供应商的采购金额", "价格走势趋势"):
        assert check_answer_contract(
            q, "SELECT SUM(x) AS total FROM t", [{"total": 1}]
        ).violations, q


def test_explicit_aggregate_request_is_not_a_grain_mismatch():
    """'total ... by' is ambiguous; an explicit aggregate cue suppresses the rule."""
    report = check_answer_contract(
        "total sales for last month",
        "SELECT SUM(FALLAMOUNT) AS total FROM t WHERE FDATE >= '2026-08-01'",
        [{"total": 5}],
    )
    assert report.ok


def test_group_by_detection():
    assert sql_is_grouped("SELECT a FROM t GROUP BY a")
    assert sql_is_grouped("SELECT a FROM t group   by a")
    assert not sql_is_grouped("SELECT a FROM t")


# ---------------------------------------------------------------------------
# Truncation rule (only when the caller knows the cap)
# ---------------------------------------------------------------------------

def test_truncation_needs_the_cap_so_it_stays_quiet_by_default():
    rows = [{"i": i} for i in range(1000)]
    assert check_answer_contract("list orders", "SELECT * FROM t", rows).ok
    report = check_answer_contract("list orders", "SELECT * FROM t", rows, row_cap=1000)
    assert report.violations == ["result_truncated"]


# ---------------------------------------------------------------------------
# Feedback text the SQL model can act on
# ---------------------------------------------------------------------------

def test_feedback_names_the_problem():
    report = check_answer_contract(
        "contract execution status for last month",
        "SELECT COUNT(*) FROM erp_t_crm_contract", [{"n": 1}],
    )
    feedback = report.feedback()
    assert "date filter" in feedback
    assert "2026-08-01" in feedback


def test_multiple_violations_are_reported_together():
    report = check_answer_contract(
        "monthly sales trend by customer for last month",
        "SELECT COUNT(*) AS n FROM erp_t_sal_orderentry", [{"n": 3}],
    )
    assert set(report.violations) >= {"period_not_applied", "grain_mismatch"}


@pytest.mark.parametrize("question,sql,expected", [
    ("contract status for last week", "SELECT COUNT(*) FROM t WHERE d >= '2026-09-11'", []),
    ("top 10 customers by revenue", "SELECT c, SUM(v) s FROM t GROUP BY c ORDER BY s DESC", []),
])
def test_clean_cases_stay_clean(question, sql, expected):
    assert check_answer_contract(question, sql, [{"x": 1}]).violations == expected


# ---------------------------------------------------------------------------
# Truncation, reported by the executor rather than inferred from a count
# ---------------------------------------------------------------------------

def test_executor_truncation_flag_is_honoured():
    report = check_answer_contract("list orders", "SELECT * FROM t", [{"i": 1}], truncated=True)
    assert report.violations == ["result_truncated"]
    assert "row cap" in report.feedback()


def test_untruncated_result_reports_nothing():
    assert check_answer_contract("list orders", "SELECT * FROM t", [{"i": 1}]).ok


# ---------------------------------------------------------------------------
# Prescriptive feedback — the retry only lands if it names WHAT to group by
# ---------------------------------------------------------------------------

def test_grain_feedback_names_the_dimensions_to_group_by():
    report = check_answer_contract(
        "contract status breakdown for last month",
        "SELECT COUNT(*) FROM erp_t_crm_contract WHERE FDATE >= '2026-08-01'",
        [{"n": 177}],
    )
    assert report.violations == ["grain_mismatch"]
    assert "e.g. GROUP BY" not in report.feedback()   # no dimensions → no concrete hint
    named = report.feedback(["FDOCUMENTSTATUS", "FCLOSESTATUS"])
    assert "GROUP BY FDOCUMENTSTATUS, FCLOSESTATUS" in named


def test_dimension_candidates_prefers_catalog_semantic_typing():
    from app.services.db.answer_contract import dimension_candidates

    schema_info = {"tables": [{"table_name": "t", "columns": [
        {"name": "FID", "semantic_type": "identifier"},
        {"name": "FALLAMOUNT", "semantic_type": "measure"},
        {"name": "FDOCUMENTSTATUS", "semantic_type": "dimension",
         "enum_values": ["C", "D"]},
        {"name": "FCUSTOMERID", "semantic_type": "dimension"},
    ]}]}
    got = dimension_candidates(schema_info)
    assert got[0] == "FDOCUMENTSTATUS"     # dimension + enum values scores highest
    assert "FALLAMOUNT" not in got         # a measure is not a grouping key
    assert "FCUSTOMERID" in got


def test_dimension_candidates_tolerates_a_missing_catalog():
    from app.services.db.answer_contract import dimension_candidates

    assert dimension_candidates(None) == []
    assert dimension_candidates({}) == []
