"""Post-loop internal-reference hygiene (Bug 3 fix).

The final bubble must not reference internal loop iterations the user never
saw: "the discrepancy", "you're right", "as I mentioned earlier", "let me
re-query…", "I'll double-check…". ``_strip_internal_references``
deterministically drops such TRAILING sentences from the accumulated
post-loop content (no LLM involved).
"""

from __future__ import annotations

from app.routers.agents import _strip_internal_references


def test_clean_answer_unchanged():
    text = (
        "Supply chain data for the last 30 days: total shipments were 12,400 "
        "units across 38 orders. Peak volume hit 860 units on Aug 3. "
        "The top partner was Ningbo Logistics."
    )
    assert _strip_internal_references(text) == text


def test_trailing_requery_promise_stripped():
    # Exact shape from the "promise-as-final-answer" trace.
    text = (
        "Based on the live tables, July revenue was ¥4.2M. "
        "Let me re-query against the live, current tables to verify."
    )
    out = _strip_internal_references(text)
    assert "Let me re-query" not in out
    assert out.strip().endswith("¥4.2M.")
    assert "Based on the live tables" in out


def test_youre_right_discrepancy_stripped():
    text = (
        "Here are the corrected numbers. "
        "You're right, the discrepancy comes from the unit mismatch."
    )
    out = _strip_internal_references(text)
    assert "discrepancy" not in out
    assert out.strip().endswith("Here are the corrected numbers.")


def test_as_i_mentioned_earlier_stripped():
    text = (
        "Q3 shipments totaled 18,900 units. "
        "As I mentioned earlier, the drop is seasonal."
    )
    out = _strip_internal_references(text)
    assert "As I mentioned earlier" not in out
    assert out.strip().endswith("units.")


def test_double_check_residue_stripped():
    text = (
        "The forecast is 12% growth. "
        "I'll double-check the aggregation before finalizing."
    )
    out = _strip_internal_references(text)
    assert "double-check" not in out
    assert out.strip().endswith("growth.")


def test_multiple_trailing_internal_refs_all_stripped():
    text = (
        "Final answer: gross margin was 21.4%. "
        "Let me verify the margin column. As I said earlier, I'll get back to you."
    )
    out = _strip_internal_references(text)
    assert out.strip().endswith("21.4%.")
    assert "verify" not in out
    assert "get back" not in out


def test_middle_sentence_with_let_me_preserved():
    # Only TRAILING sentences are stripped; a leading "Let me note…" that is
    # part of a substantive answer is preserved.
    text = (
        "Let me note that the numbers are preliminary. "
        "The final total is 9,210 units."
    )
    out = _strip_internal_references(text)
    assert "preliminary" in out
    assert out.strip().endswith("units.")


def test_dangling_connector_trimmed():
    text = (
        "Revenue grew 6% quarter-over-quarter. "
        "You're right, the earlier figure was stale, so"
    )
    out = _strip_internal_references(text)
    assert not out.rstrip().endswith("so")
    assert out.strip().endswith("quarter-over-quarter.")


def test_all_internal_returns_empty():
    """Contract change (2026-08-21): when EVERY sentence is internal-
    reference narration, return "" — the post-loop empty-bubble guarantee
    replaces it with a real fallback message. The old behavior (return the
    original narration as a "remnant") leaked promise text into the final
    bubble in the failing traces."""
    text = "Let me re-query the tables and verify the discrepancy."
    assert _strip_internal_references(text) == ""


def test_empty_and_whitespace_input():
    assert _strip_internal_references("") == ""
    assert _strip_internal_references("   ") == "   "


def test_chinese_sentence_boundary():
    text = "七月出货总量为18,900件。让我再核实一下数据差异。"
    out = _strip_internal_references(text)
    assert "核实" not in out
    assert out.strip().endswith("件。")
