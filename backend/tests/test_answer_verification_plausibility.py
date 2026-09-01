"""Unit tests for the plausibility detectors (Fix 2, 2026-08-18).

Covers the two deterministic detectors added to answer_verification:

- ``_detect_part_whole_inconsistency`` — a breakdown part (or the parts-sum)
  exceeding a total-labeled value by >2% within one result set, or exceeding
  a total stated in the draft answer.
- ``_detect_total_drift`` — two same-scope total-labeled values in the same
  turn's results differing by >10%.

Both are flag-gated by ``ANSWER_PLAUSIBILITY_CHECK_ENABLED`` (default off) and
flow through the existing INCOMPLETE/IMPOSSIBLE escalation via
``evaluate_answer``.
"""
import pytest

from app.config import settings
from app.services import answer_verification as av


@pytest.fixture(autouse=True)
def _enable_flags(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_PLAUSIBILITY_CHECK_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_MAX_REPLANS", 3)


def _summaries(raw_list):
    return av.summarize_tool_results(raw_list)


# ── _detect_part_whole_inconsistency ────────────────────────────────────


def test_part_whole_fires_when_single_part_exceeds_total():
    results = _summaries([
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 600},
                {"name": "B", "sales": 500},
                {"name": "合计", "sales": 1000},
            ],
        }
    ])
    # 600 > 1000*1.02 → impossible single part
    assert av._detect_part_whole_inconsistency(results, "") == ["part_whole"]


def test_part_whole_fires_when_parts_sum_exceeds_total():
    results = _summaries([
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 600},
                {"name": "B", "sales": 500},
                {"name": "total", "sales": 1000},
            ],
        }
    ])
    # 600 + 500 = 1100 > 1000*1.02 = 1020
    assert av._detect_part_whole_inconsistency(results, "") == ["part_whole"]


def test_part_whole_clean_when_parts_sum_below_total():
    results = _summaries([
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 400},
                {"name": "B", "sales": 500},
                {"name": "total", "sales": 1000},
            ],
        }
    ])
    assert av._detect_part_whole_inconsistency(results, "") == []


def test_part_whole_fires_against_stated_total_in_answer():
    results = _summaries([
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 700},
                {"name": "B", "sales": 500},
            ],
        }
    ])
    # parts-sum = 1200 > 1100*1.02 stated in the answer
    text = "Total sales across both materials was 1100 dollars."
    assert av._detect_part_whole_inconsistency(results, text) == ["part_whole"]


def test_part_whole_flag_off_never_fires(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_PLAUSIBILITY_CHECK_ENABLED", False)
    results = _summaries([
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 600},
                {"name": "B", "sales": 500},
                {"name": "合计", "sales": 1000},
            ],
        }
    ])
    assert av._detect_part_whole_inconsistency(results, "") == []


# ── _detect_total_drift ─────────────────────────────────────────────────


def test_total_drift_fires_when_same_scope_totals_differ():
    results = _summaries([
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 1000}]},
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 850}]},
    ])
    # 1000/850 = 1.176 > 1.10
    assert av._detect_total_drift(results) == ["total_drift"]


def test_total_drift_clean_within_10_percent():
    results = _summaries([
        {"tool": "execute_query", "rows": [{"material": "Total", "sales": 1000}]},
        {"tool": "execute_query", "rows": [{"material": "Total", "sales": 950}]},
    ])
    assert av._detect_total_drift(results) == []


def test_total_drift_clean_when_scopes_differ():
    results = _summaries([
        {"tool": "execute_query", "rows": [{"material": "Total", "sales": 1000}]},
        {"tool": "execute_query", "rows": [{"material": "Total", "volume": 850}]},
    ])
    assert av._detect_total_drift(results) == []


def test_total_drift_flag_off_never_fires(monkeypatch):
    monkeypatch.setattr(settings, "ANSWER_PLAUSIBILITY_CHECK_ENABLED", False)
    results = _summaries([
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 1000}]},
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 850}]},
    ])
    assert av._detect_total_drift(results) == []


# ── escalation through evaluate_answer ──────────────────────────────────


def test_evaluate_answer_escalates_part_whole_to_incomplete():
    tool_results = [
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 600},
                {"name": "B", "sales": 500},
                {"name": "合计", "sales": 1000},
            ],
        }
    ]
    result = av.evaluate_answer(
        "what were total sales by material",
        tool_results,
        "A had 600, B had 500.",
        attempts=0,
        budget_remaining=10,
    )
    assert result.status == "INCOMPLETE"
    assert "part_whole" in result.signals
    assert "re-query" in result.suggested_fix.lower() or "verify" in result.suggested_fix.lower()


def test_evaluate_answer_escalates_total_drift_to_impossible_when_exhausted():
    tool_results = [
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 1000}]},
        {"tool": "execute_query", "rows": [{"material": "合计", "sales": 850}]},
    ]
    result = av.evaluate_answer(
        "total sales for july",
        tool_results,
        "July total was 1000.",
        attempts=3,  # max replans exhausted
        budget_remaining=10,
    )
    assert result.status == "IMPOSSIBLE"
    assert "total_drift" in result.signals


def test_evaluate_answer_clean_when_plausible():
    tool_results = [
        {
            "tool": "execute_query",
            "rows": [
                {"name": "A", "sales": 400},
                {"name": "B", "sales": 500},
                {"name": "total", "sales": 900},
            ],
        }
    ]
    result = av.evaluate_answer(
        "total sales by material",
        tool_results,
        "A had 400, B had 500, total 900.",
        attempts=0,
        budget_remaining=10,
    )
    assert result.status == "COMPLETE"
