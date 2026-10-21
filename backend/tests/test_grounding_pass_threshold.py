"""Figure-grounding pass threshold — "Finalizing your answer" latency fix.

Live failure (2026-10-19/20, conv e1db0d0d): a rich weekly-sales report stated
106 figures; 104 were verifiably grounded (98.1%) but the gate required ZERO
unverified, so 2 legitimate derived figures (a computed share, a rounded
ex-tax total) triggered a FULL re-synthesis ("refinement 1/1") — the user
stared at "Finalizing your answer" for 30-60 extra seconds and the rewrite
was discarded anyway when the refinement budget exhausted.

Contract pinned here:
- a near-perfect report (residual <= ANSWER_GROUNDING_MAX_UNVERIFIED_ABS, or
  ratio <= ANSWER_GROUNDING_MAX_UNVERIFIED_RATIO) PASSES with a loud log —
  no rewrite, no latency hit;
- a materially ungrounded answer still triggers the nudge;
- with the thresholds zeroed the old strict behaviour is restored exactly.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from app.routers.agents import _check_figure_grounding


def _payload(n_figures: int) -> str:
    """One tool result whose first row carries n distinct grounded values."""
    row = {f"col_{i}": 1000 + i for i in range(n_figures)}
    return json.dumps({"success": True, "rows": [row], "row_count": 1})


def _answer(grounded: int, unverified: int) -> str:
    parts = [f"{1000 + i:,} units" for i in range(grounded)]
    # Unverified figures the tool results simply do not contain.
    parts += [f"up {12 + i}% month over month" for i in range(unverified)]
    return ". ".join(parts)


def _check(answer: str, payload: str):
    messages = [
        {"role": "user", "content": "give me last week sales overview"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": payload},
        {"role": "assistant", "content": answer},
    ]
    return _check_figure_grounding(
        "give me last week sales overview",
        answer,
        messages,
        guardrail_retries=0,
    )


class TestPassThreshold:
    def test_near_perfect_report_passes_without_rewrite(self):
        """THE live failure shape: 2 of 106 unverified (98.1% verified)."""
        result = _check(_answer(104, 2), _payload(104))
        assert result.action == "none", result.message

    def test_small_residual_within_absolute_threshold_passes(self):
        result = _check(_answer(20, 3), _payload(20))
        assert result.action == "none"

    def test_small_ratio_passes_even_above_absolute_cap(self):
        # 5 unverified of 400 figures = 1.25% <= 2% ratio — passes despite
        # exceeding the absolute cap of 3.
        result = _check(_answer(395, 5), _payload(395))
        assert result.action == "none"

    def test_materially_ungrounded_answer_still_nudges(self):
        # 10 of 20 unverified = 50% — far beyond both thresholds.
        result = _check(_answer(10, 10), _payload(10))
        assert result.action == "nudge"
        assert "FIGURE CHECK FAILED" in result.message

    def test_fully_grounded_answer_passes_unchanged(self):
        result = _check(_answer(50, 0), _payload(50))
        assert result.action == "none"


class TestStrictMode:
    def test_zero_thresholds_restore_the_old_strict_behaviour(self):
        with patch("app.routers.agents.settings") as mock_settings:
            mock_settings.ANSWER_GROUNDING_ENABLED = True
            mock_settings.ANSWER_GROUNDING_TOLERANCE = 0.005
            mock_settings.ANSWER_GROUNDING_MAX_UNVERIFIED_ABS = 0
            mock_settings.ANSWER_GROUNDING_MAX_UNVERIFIED_RATIO = 0.0
            result = _check(_answer(104, 2), _payload(104))
        # Strict mode: even 2/106 unverified triggers the nudge again.
        assert result.action == "nudge"
