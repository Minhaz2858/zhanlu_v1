"""Unit tests for the universal answer-verification gate.

Covers: deterministic detectors (metadata-only, empty-results, degenerate
values, lexical dimension coverage, placeholder text), the hybrid
deterministic+LLM evaluator (TOTAL on LLM failure), cap-at-3 / finish-line
IMPOSSIBLE behavior, nudge/disclosure builders, and the zero-hardcoding rule
(no domain table/column names anywhere in the module).
"""
import re
from pathlib import Path

import pytest

from app.config import settings
from app.services import answer_verification as av


@pytest.fixture(autouse=True)
def _enable_verification_flag(monkeypatch):
    """Verification gate is flag-gated; tests exercise the flag-on path.

    LLM strict-inspector is off by default (deterministic-only) so unit tests
    never hit the real LLM; the dedicated LLM tests opt in explicitly.
    """
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    monkeypatch.setattr(settings, "SELF_EVAL_MAX_REPLANS", 3)


# ── summarize_tool_result ──────────────────────────────────────────────


def test_summarize_tool_result_dict_rows():
    raw = {
        "tool": "execute_query",
        "columns": ["FNAME", "price", "volume"],
        "rows": [{"FNAME": "PVC", "price": 3.2, "volume": 100}],
    }
    out = av.summarize_tool_result(raw)
    assert out["tool"] == "execute_query"
    assert out["columns"] == ["FNAME", "price", "volume"]
    assert out["row_count"] == 1
    assert out["empty"] is False


def test_summarize_tool_result_empty_rows():
    out = av.summarize_tool_result({"tool": "execute_query", "columns": ["a"], "rows": []})
    assert out["row_count"] == 0
    assert out["empty"] is True


def test_summarize_tool_result_list_rows_infers_columns():
    out = av.summarize_tool_result({"rows": [["PVC", 3.2]], "columns": ["name", "price"]})
    assert out["row_count"] == 1


def test_summarize_tool_result_truncates_rows_and_text():
    raw = {
        "rows": [{"x": i} for i in range(50)],
        "text": "x" * 5000,
    }
    out = av.summarize_tool_result(raw)
    assert len(out["rows"]) == 5
    assert len(out["text"]) <= 1200


# ── Detectors: metadata-only ───────────────────────────────────────────


def test_metadata_only_detected_when_columns_without_rows():
    result = av.evaluate_answer(
        "what is the price of materials?",
        [{"tool": "execute_query", "columns": ["FNAME", "price"], "rows": []}],
        "I found the schema with price and FNAME columns.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "metadata" in result.signals


# ── Detectors: empty-results ───────────────────────────────────────────


def test_empty_results_detected_when_all_tools_return_nothing():
    result = av.evaluate_answer(
        "show monthly sales volume",
        [{"tool": "execute_query", "rows": [], "columns": None}],
        "I queried the database.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "empty" in result.signals


# ── Detectors: degenerate values ───────────────────────────────────────


def test_degenerate_values_detected_when_all_nulls_or_zero():
    result = av.evaluate_answer(
        "what are the prices?",
        [{"tool": "execute_query", "columns": ["price", "volume"],
          "rows": [{"price": None, "volume": 0}, {"price": "", "volume": 0}]}],
        "The prices are listed above.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "degenerate" in result.signals


# ── Detectors: placeholder text ────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "I found 42,993 materials across 6 tables.",
        "I had trouble putting it all together.",
        "I gathered some information but had trouble putting it together.",
    ],
)
def test_placeholder_text_detected(text):
    result = av.evaluate_answer(
        "what is the price of materials?",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": [{"FNAME": "PVC"}]}],
        text,
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "placeholder" in result.signals


# ── Detectors: lexical dimension coverage ──────────────────────────────


def test_missing_requested_dimension_detected():
    result = av.evaluate_answer(
        "show me the prices and volumes for each material",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": [{"FNAME": "PVC"}]}],
        "Here are the materials: PVC.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "coverage" in result.signals
    assert any("price" in g.lower() for g in result.gaps)


def test_requested_dimensions_present_are_not_flagged():
    result = av.evaluate_answer(
        "show me the prices and volumes for each material",
        [{"tool": "execute_query", "columns": ["FNAME", "price", "volume"],
          "rows": [{"FNAME": "PVC", "price": 3.2, "volume": 100}]}],
        "PVC: price 3.2, volume 100.",
        attempts=0,
        budget_remaining=30,
    )
    assert "coverage" not in result.signals


def test_dimension_present_in_assistant_text_counts_covered():
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": [{"FNAME": "PVC"}]}],
        "The price of PVC is 3.2 USD.",
        attempts=0,
        budget_remaining=30,
    )
    assert "coverage" not in result.signals


# ── Detectors: blank dimension columns ─────────────────────────────────


def test_blank_dimension_detected_when_name_column_all_blank():
    result = av.evaluate_answer(
        "show me last month sales data",
        [{"tool": "execute_query",
          "columns": ["FCUSTMATNAME", "amount"],
          "rows": [{"FCUSTMATNAME": "", "amount": 100},
                   {"FCUSTMATNAME": None, "amount": 200},
                   {"FCUSTMATNAME": "   ", "amount": 300}]}],
        "Here are the sales figures by product.",
        attempts=0, budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "blank_dimension" in result.signals
    assert any("FCUSTMATNAME" in g for g in result.gaps)


def test_blank_dimension_not_detected_when_name_column_has_values():
    result = av.evaluate_answer(
        "show me sales data",
        [{"tool": "execute_query",
          "columns": ["FCUSTMATNAME", "amount"],
          "rows": [{"FCUSTMATNAME": "PVC", "amount": 100},
                   {"FCUSTMATNAME": "C5", "amount": 200}]}],
        "PVC: 100, C5: 200.",
        attempts=0, budget_remaining=30,
    )
    assert "blank_dimension" not in result.signals


def test_blank_dimension_not_detected_on_numeric_only_columns():
    result = av.evaluate_answer(
        "what are the amounts?",
        [{"tool": "execute_query",
          "columns": ["amount", "volume"],
          "rows": [{"amount": None, "volume": 0},
                   {"amount": "", "volume": 0}]}],
        "Amounts listed above.",
        attempts=0, budget_remaining=30,
    )
    assert "blank_dimension" not in result.signals


def test_blank_dimension_not_detected_on_empty_rows():
    result = av.evaluate_answer(
        "what are the product names?",
        [{"tool": "execute_query", "columns": ["FCUSTMATNAME", "amount"], "rows": []}],
        "No rows returned.",
        attempts=0, budget_remaining=30,
    )
    assert "blank_dimension" not in result.signals


def test_blank_dimension_not_detected_when_secondary_name_column_blank():
    """Only the FIRST name-role column is the primary dimension; a later
    blank name column must not fire (avoids over-triggering)."""
    result = av.evaluate_answer(
        "show me sales data",
        [{"tool": "execute_query",
          "columns": ["FCUSTMATNAME", "FNAME", "amount"],
          "rows": [{"FCUSTMATNAME": "PVC", "FNAME": "", "amount": 100},
                   {"FCUSTMATNAME": "C5", "FNAME": None, "amount": 200}]}],
        "PVC: 100, C5: 200.",
        attempts=0, budget_remaining=30,
    )
    assert "blank_dimension" not in result.signals


def test_blank_dimension_nudge_suggests_master_join():
    result = av.evaluate_answer(
        "show me last month sales data",
        [{"tool": "execute_query",
          "columns": ["FCUSTMATNAME", "amount"],
          "rows": [{"FCUSTMATNAME": "", "amount": 100}]}],
        "Sales figures by product.",
        attempts=0, budget_remaining=30,
    )
    nudge = av.build_replan_nudge(result)
    assert "entity master" in nudge.lower() or "master" in nudge.lower()
    assert "join" in nudge.lower()


def test_blank_dimension_detected_without_columns_key():
    """Real QueryService payloads carry rows dicts but NO 'columns' key (main
    loop + data-agent sub-loop forward the raw payload). The detector must
    derive columns from rows[0].keys() or it is dead code."""
    result = av.evaluate_answer(
        "show me last month sales data",
        [{"tool": "execute_query",
          "rows": [{"FCUSTMATNAME": "", "amount": 100},
                   {"FCUSTMATNAME": None, "amount": 200}]}],
        "Here are the sales figures.",
        attempts=0, budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "blank_dimension" in result.signals


def test_blank_dimension_not_detected_numeric_without_columns_key():
    result = av.evaluate_answer(
        "what are the amounts?",
        [{"tool": "execute_query",
          "rows": [{"amount": None, "volume": 0}]}],
        "Amounts listed above.",
        attempts=0, budget_remaining=30,
    )
    assert "blank_dimension" not in result.signals


# ── Gate semantics: complete answers ───────────────────────────────────


def test_complete_answer_returns_complete():
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME", "price"],
          "rows": [{"FNAME": "PVC", "price": 3.2}]}],
        "The price of PVC is 3.2 USD.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "COMPLETE"


def test_no_tool_results_returns_complete():
    result = av.evaluate_answer(
        "hello", [], "Hi there!", attempts=0, budget_remaining=30,
    )
    assert result.status == "COMPLETE"


# ── Gate semantics: cap at max replans → IMPOSSIBLE ────────────────────


def test_attempts_capped_becomes_impossible():
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": []}],
        "I found the schema.",
        attempts=3,
        budget_remaining=30,
    )
    assert result.status == "IMPOSSIBLE"


def test_finish_line_budget_remaining_forces_impossible():
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": []}],
        "I found the schema.",
        attempts=0,
        budget_remaining=1,
    )
    assert result.status == "IMPOSSIBLE"


# ── Hybrid evaluator: LLM at the synthesis boundary ────────────────────


def test_llm_complete_verdict_is_used_when_deterministic_is_clean(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", True)
    monkeypatch.setattr(av, "_run_llm_eval", lambda *a, **k: ("COMPLETE", [], ""))
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME", "price"],
          "rows": [{"FNAME": "PVC", "price": 3.2}]}],
        "PVC price is 3.2.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "COMPLETE"
    assert result.source == "llm"


def test_llm_incomplete_verdict_overrides_clean_deterministic(monkeypatch):
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", True)
    monkeypatch.setattr(
        av, "_run_llm_eval",
        lambda *a, **k: ("INCOMPLETE", ["missing volume data"], "query volume column"),
    )
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME", "price"],
          "rows": [{"FNAME": "PVC", "price": 3.2}]}],
        "PVC price is 3.2.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "missing volume data" in result.gaps
    assert result.source == "llm"


def test_llm_failure_falls_back_to_deterministic(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")

    monkeypatch.setattr(av, "_run_llm_eval", boom)
    result = av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME", "price"],
          "rows": [{"FNAME": "PVC", "price": 3.2}]}],
        "PVC price is 3.2.",
        attempts=0,
        budget_remaining=30,
    )
    # TOTAL: never raises; deterministic verdict (complete) survives.
    assert result.status == "COMPLETE"
    assert result.source == "deterministic"


def test_llm_skipped_when_deterministic_signal_fired(monkeypatch):
    called = []

    def recorder(*a, **k):
        called.append(True)
        return ("COMPLETE", [], "")

    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", True)
    monkeypatch.setattr(av, "_run_llm_eval", recorder)
    av.evaluate_answer(
        "what is the price of PVC?",
        [{"tool": "execute_query", "columns": ["FNAME"], "rows": []}],
        "I found the schema.",
        attempts=0,
        budget_remaining=30,
    )
    assert called == []


# ── TOTAL invariant ────────────────────────────────────────────────────


def test_evaluate_answer_is_total_on_garbage_input():
    result = av.evaluate_answer("", [{"bogus": object()}], None)
    assert result.status == "COMPLETE"


# ── Nudge / disclosure builders ────────────────────────────────────────


def test_build_replan_nudge_returns_text_for_incomplete():
    result = av.VerificationResult(
        status="INCOMPLETE",
        gaps=["price data missing"],
        suggested_fix="try querying the price column",
        signals=["coverage"],
    )
    nudge = av.build_replan_nudge(result)
    assert "price data missing" in nudge
    assert "try querying the price column" in nudge


def test_build_replan_nudge_empty_for_complete():
    assert av.build_replan_nudge(av.VerificationResult(status="COMPLETE")) == ""


def test_build_gap_disclosure_for_impossible():
    result = av.VerificationResult(
        status="IMPOSSIBLE",
        gaps=["volume data not available"],
        suggested_fix="rephrase your request",
        signals=["empty"],
    )
    disclosure = av.build_gap_disclosure(result)
    assert "volume data not available" in disclosure
    assert "rephrase your request" in disclosure


def test_build_gap_disclosure_empty_for_complete():
    assert av.build_gap_disclosure(av.VerificationResult(status="COMPLETE")) == ""


# ── pending_action downgrade (2026-08-25) ──────────────────────────────
# When the only signal is `pending_action` (model ends with "Let me...")
# AND the agent has produced substantive prose AND has data, downgrade
# the result to COMPLETE. The trailing promise phrase is stripped
# post-loop by agents.py:12746-12762, so re-iterating to force a new
# answer is unnecessary and only causes the "collapse" UX.


def test_pending_action_only_downgrades_to_complete_with_data_and_prose(monkeypatch):
    """Substantive prose + data + pending_action phrase → COMPLETE."""
    monkeypatch.setattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
    long_prose = (
        "Sales summary for last month: PVC was the top seller with 1,200 units, "
        "followed by C5 with 800 units and C9 with 450 units. Customer mix "
        "remained stable, with the top three partners accounting for 60% of "
        "shipment volume. Average contract price held steady at the prior "
        "month's level, indicating stable demand across the portfolio and no "
        "material shift in customer behavior. We can monitor the daily "
        "shipment cadence to confirm the trend holds for the current month."
    )
    assert len(long_prose) >= 200
    result = av.evaluate_answer(
        "give me last month sales report",
        [
            {
                "tool": "execute_query",
                "columns": ["FNAME", "units"],
                "rows": [
                    {"FNAME": "PVC", "units": 1200},
                    {"FNAME": "C5", "units": 800},
                    {"FNAME": "C9", "units": 450},
                ],
            }
        ],
        long_prose + " Let me verify these numbers against the database.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "COMPLETE", (
        f"expected COMPLETE for data-sufficient pending_action-only signal, "
        f"got {result.status} with signals={result.signals}"
    )
    assert "pending_action_downgraded" in result.signals


def test_pending_action_only_does_not_downgrade_short_prose(monkeypatch):
    """Short prose + data + pending_action phrase → still INCOMPLETE.

    The downgrade requires >= SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE chars
    of substantive prose. Short prose doesn't qualify.
    """
    monkeypatch.setattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
    result = av.evaluate_answer(
        "give me last month sales report",
        [
            {
                "tool": "execute_query",
                "columns": ["FNAME", "amount"],
                "rows": [{"FNAME": "PVC", "amount": 1200}],
            }
        ],
        "Top seller: PVC. Let me check details.",
        attempts=0,
        budget_remaining=30,
    )
    assert result.status == "INCOMPLETE"
    assert "pending_action" in result.signals


def test_pending_action_only_does_not_downgrade_without_data(monkeypatch):
    """No data + substantive prose + pending_action phrase → still INCOMPLETE.

    Without data, the answer is empty; the downgrade should not fire.
    """
    monkeypatch.setattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
    long_prose = (
        "Based on my analysis, the sales report shows a significant increase "
        "in volume across all product categories during the last month. "
        "The trend analysis indicates a 12% rise compared to the prior month, "
        "with PVC and C5 leading the growth. Customer concentration remained "
        "stable, with the top 3 partners representing 60% of total volume."
        " Let me verify these numbers against the database."
    )
    assert len(long_prose) >= 200
    result = av.evaluate_answer(
        "give me last month sales report",
        [{"tool": "execute_query", "columns": ["x"], "rows": []}],  # empty
        long_prose,
        attempts=0,
        budget_remaining=30,
    )
    # Could fire empty + pending_action. Must NOT downgrade to COMPLETE.
    assert result.status in ("INCOMPLETE", "IMPOSSIBLE")
    assert "pending_action" in result.signals


def test_pending_action_does_not_downgrade_when_other_signals_fire(monkeypatch):
    """Multi-signal (pending_action + coverage) → still INCOMPLETE.

    The downgrade only fires when pending_action is the SOLE signal.
    If other detectors also fire (coverage, empty, degenerate), the
    answer is genuinely incomplete and a nudge is justified.
    """
    monkeypatch.setattr(settings, "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE", 200)
    long_prose = (
        "I have some data but I'm missing the Q3 column. Let me re-query "
        "to get the full breakdown with the daily aggregation you asked for. "
        "The data so far shows PVC leading the volume chart at 1,200 units, "
        "but I cannot present the complete Q3 daily breakdown without the "
        "missing columns. Let me re-query to get the full breakdown now."
    )
    assert len(long_prose) >= 200
    result = av.evaluate_answer(
        "give me last month Q3 sales report with daily breakdown",
        [
            {
                "tool": "execute_query",
                "columns": ["FNAME", "amount"],  # missing "Q3" / daily columns
                "rows": [{"FNAME": "PVC", "amount": 1200}],
            }
        ],
        long_prose,
        attempts=0,
        budget_remaining=30,
    )
    # At minimum pending_action fires; if coverage also fires, downgrade
    # must NOT activate. Either way, result must be INCOMPLETE (not COMPLETE).
    assert result.status in ("INCOMPLETE", "IMPOSSIBLE")
    assert "pending_action_downgraded" not in result.signals


# ── Zero-hardcoding rule ───────────────────────────────────────────────


_FORBIDDEN_DOMAIN_TOKENS = [
    "erp_", "FNAME", "c5_resin", "material_name", "shipment_grade",
    "contract_price", "partner_name", "inventory", "pvc_resin",
    "e5ac337b", "aipdp",
]


@pytest.mark.parametrize("token", _FORBIDDEN_DOMAIN_TOKENS)
def test_module_has_no_hardcoded_domain_names(token):
    src = Path(av.__file__).read_text(encoding="utf-8")
    assert token not in src, f"domain token {token!r} leaked into answer_verification.py"
