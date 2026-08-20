"""D5 (2026-08-20): deterministic arithmetic-consistency gate.

Extracts stated arithmetic claims from the draft answer and flags mismatches
>2%. Regression cases from the user trace: "5,565 of 11,028 delivered, leaving
6,183 outstanding" (11,028 − 5,565 = 5,463 ≠ 6,183) and "121.31 tons ≈ 2.3
months" (121.31 / 382 ≈ 0.32 ≠ 2.3).
"""

from app.config import settings
from app.services.answer_verification import (
    _detect_arithmetic_inconsistency,
    evaluate_answer,
)


# ── subtraction: "A of B ... leaving C" ─────────────────────────────────────


def test_arithmetic_subtraction_inconsistency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    signals = _detect_arithmetic_inconsistency(
        "5,565 of 11,028 delivered, leaving 6,183 outstanding."
    )
    assert signals == ["arithmetic_inconsistency"]


def test_arithmetic_subtraction_consistent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency(
            "5,565 of 11,028 delivered, leaving 5,463 outstanding."
        )
        == []
    )


# ── ratio: "X tons ≈ N months of cover" ─────────────────────────────────────


def test_arithmetic_ratio_inconsistency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    signals = _detect_arithmetic_inconsistency(
        "This represents 121.31 tons ≈ 2.3 months of cover at the current "
        "382 tons/month run rate."
    )
    assert signals == ["arithmetic_inconsistency"]


def test_arithmetic_ratio_consistent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency(
            "This represents 121.31 tons ≈ 0.32 months of cover at the "
            "current 382 tons/month run rate."
        )
        == []
    )


def test_arithmetic_ratio_skipped_without_rate(monkeypatch) -> None:
    """Without a stated run rate the coverage-ratio claim cannot be verified."""
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency(
            "This represents 121.31 tons ≈ 2.3 months of cover."
        )
        == []
    )


# ── percentage: "A% of B is C" ──────────────────────────────────────────────


def test_arithmetic_percentage_inconsistency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency("That is 35% of 200 = 80 units.")
        == ["arithmetic_inconsistency"]
    )


def test_arithmetic_percentage_consistent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency("That is 35% of 200 = 70 units.")
        == []
    )


# ── addition: "A + B = C" and "A and B total C" ─────────────────────────────


def test_arithmetic_addition_inconsistency(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency("The total is 120 + 30 = 200 units.")
        == ["arithmetic_inconsistency"]
    )


def test_arithmetic_addition_consistent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency("The total is 120 + 30 = 150 units.")
        == []
    )


def test_arithmetic_total_consistent(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    assert (
        _detect_arithmetic_inconsistency(
            "North and South together total 2,300 tons."
        )
        == []
    )


# ── gating + integration ────────────────────────────────────────────────────


def test_arithmetic_flag_gated_off(monkeypatch) -> None:
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", False)
    assert (
        _detect_arithmetic_inconsistency(
            "5,565 of 11,028 delivered, leaving 6,183 outstanding."
        )
        == []
    )


def test_evaluate_answer_wires_arithmetic_signal(monkeypatch) -> None:
    """End-to-end through the gate: a bad arithmetic claim -> INCOMPLETE with
    the ``arithmetic_inconsistency`` signal and a recompute nudge."""
    monkeypatch.setattr(settings, "ARITHMETIC_CONSISTENCY_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_REPLAN_ENABLED", True)
    monkeypatch.setattr(settings, "SELF_EVAL_LLM_GATE_ENABLED", False)
    tool_results = [
        {
            "tool": "execute_query",
            "columns": ["product", "delivered"],
            "rows": [["A", 5565], ["B", 5463]],
            "row_count": 2,
            "empty": False,
            "text": "",
        }
    ]
    res = evaluate_answer(
        "How much was delivered?",
        tool_results,
        "5,565 of 11,028 delivered, leaving 6,183 outstanding.",
        attempts=0,
        budget_remaining=100,
    )
    assert res.status == "INCOMPLETE"
    assert "arithmetic_inconsistency" in res.signals
    assert "recompute" in res.suggested_fix.lower()
