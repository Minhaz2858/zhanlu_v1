"""Figure provenance — the guard that checks the PROSE, not the SQL.

Structural gap this covers (2026-09-18): every SQL-side check stops at the rows.
`_check_hallucination_guardrail` only caught "a data question was answered with
NO tool call"; once `ask_data_agent` ran, any figure in the prose was accepted.
So a comparison the SQL never computed, a total nobody summed, or a unit swap
could reach the user with no signal.

These tests pin two things at once: the check catches invented figures, and it
does NOT flag correct ones (a checker that cries wolf is worse than none).
"""

from __future__ import annotations

import json

from app.services.db.answer_grounding import (
    check_answer_grounding,
    collect_tool_payloads,
    extract_figures,
    grounded_numbers,
)

ROWS = [
    {"FDOCUMENTSTATUS": "C", "n": 176},
    {"FDOCUMENTSTATUS": "D", "n": 8},
]
PAYLOAD = json.dumps({"success": True, "sql": "SELECT ...", "rows": ROWS, "row_count": 2})

# 176 + 8 = 184 (a sum), mean 92, ratios 95.65% / 4.35%.
SUMS_EXPECTED = [184.0, 92.0, 176.0, 8.0]


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_thousands_separators_and_currency_are_one_figure():
    figs = extract_figures("Revenue was ¥1,234,567 last month.")
    assert [f.value for f in figs] == [1234567.0]


def test_chinese_scale_suffixes_are_normalised():
    assert [f.value for f in extract_figures("约 3.46亿 元")] == [346000000.0]
    assert [f.value for f in extract_figures("12万条")] == [120000.0]


def test_percent_is_flagged_as_percent():
    figs = extract_figures("revenue is up 12% month over month")
    assert len(figs) == 1
    assert figs[0].is_percent is True
    assert figs[0].value == 12.0


def test_years_are_not_data_claims():
    figs = extract_figures("In 2026 the total was 3,200 orders.")
    assert [f.value for f in figs] == [3200.0]


def test_ordinals_and_small_bare_integers_are_not_data_claims():
    assert extract_figures("1. Revenue grew\n2. Margin held") == []
    assert extract_figures("We recommend 3 actions and 2 follow-ups.") == []
    assert extract_figures("see steps 4 and 5") == []


def test_bare_integer_at_or_above_the_threshold_is_a_claim():
    assert [f.value for f in extract_figures("168 contracts closed.")] == [168.0]


def test_figure_context_is_captured_for_the_feedback_text():
    figs = extract_figures("Revenue is up 12% month over month.")
    assert "up 12% month over month" in figs[0].context


# ---------------------------------------------------------------------------
# What counts as grounded
# ---------------------------------------------------------------------------

def test_grounded_numbers_includes_cells_and_column_aggregates():
    grounded = grounded_numbers([PAYLOAD])
    for value in SUMS_EXPECTED:
        assert value in grounded, f"{value} should be grounded"


def test_grounded_numbers_survives_a_non_json_payload():
    grounded = grounded_numbers(["tool returned 1,250 rows in 340 ms"])
    assert 1250.0 in grounded and 340.0 in grounded


def test_collect_tool_payloads_reads_only_tool_messages():
    messages = [
        {"role": "system", "content": "ignore me"},
        {"role": "assistant", "content": "not a tool result"},
        {"role": "tool", "content": PAYLOAD},
    ]
    assert collect_tool_payloads(messages) == [PAYLOAD]
    assert collect_tool_payloads(None) == []


# ---------------------------------------------------------------------------
# The check
# ---------------------------------------------------------------------------

def test_a_cell_value_is_grounded():
    assert check_answer_grounding("Contract count: 176", [PAYLOAD]).ok


def test_a_total_the_sql_never_summed_is_grounded():
    """"Total 184" over rows of 176 and 8 is CORRECT — must not be flagged."""
    assert check_answer_grounding("Total contracts: 184", [PAYLOAD]).ok


def test_an_average_of_a_column_is_grounded():
    assert check_answer_grounding("Average per status: 92", [PAYLOAD]).ok


def test_rounding_through_a_scale_suffix_is_grounded():
    payload = json.dumps({"rows": [{"revenue": 345678912}]})
    assert check_answer_grounding("Revenue ≈3.46亿", [payload]).ok


def test_a_ratio_derived_percentage_is_grounded():
    assert check_answer_grounding("Status C is 95.7% of contracts", [PAYLOAD]).ok


def test_an_invented_comparison_is_flagged():
    report = check_answer_grounding("Revenue is up 12% month over month", [PAYLOAD])
    assert not report.ok
    assert [f.raw for f in report.unverified] == ["12%"]
    assert "12%" in report.feedback()
    assert "do not" in report.feedback()


def test_an_invented_total_is_flagged():
    report = check_answer_grounding("Total contracts: 3,400", [PAYLOAD])
    assert not report.ok
    assert report.unverified[0].value == 3400.0


def test_numbers_from_the_user_question_may_be_echoed():
    report = check_answer_grounding(
        "The top 500 customers account for most volume",
        [PAYLOAD],
        question="show the top 500 customers by volume",
    )
    assert report.ok


def test_without_the_question_the_same_figure_is_flagged():
    report = check_answer_grounding(
        "The top 500 customers account for most volume", [PAYLOAD],
    )
    assert not report.ok


def test_empty_or_available_free_answers_are_untouched():
    assert check_answer_grounding("", [PAYLOAD]).ok
    assert check_answer_grounding(None, [PAYLOAD]).ok
    assert check_answer_grounding("No figures here.", []).ok


def test_report_counts_what_it_examined():
    report = check_answer_grounding("Revenue up 12% and 3,400 contracts", [PAYLOAD])
    assert report.figures_checked == 2
    assert len(report.unverified) == 2
    assert report.grounded_size > 0


def test_feedback_truncates_a_long_violation_list():
    answer = " ".join(f"{n},000" for n in range(50, 60))
    report = check_answer_grounding(answer, [PAYLOAD])
    assert len(report.unverified) >= 6
    assert "+" in report.feedback() and "more" in report.feedback()
