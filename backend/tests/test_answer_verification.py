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
