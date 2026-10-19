"""Duplicate-answer guard — collapse byte-identical halves of a final answer.

Measured live (2026-10-19, session db04120f, automation run): the persisted
assistant message contained the SAME full report twice, concatenated with NO
separator ("…accounts moved?## Last Week Sales Overview") — the user saw the
whole report rendered twice. Both halves normalized-identical (4,893/4,894
chars), so a mechanical duplicate, not a model re-wording.

The guard runs at final content assembly (after the multi-iteration join,
before persist/done) and collapses a duplicated tail regardless of WHICH
upstream path produced it — same pattern as the collapse guards. When it
fires it logs loudly (halves' lengths + boundary fingerprint) so the real
source identifies itself in the logs next time.
"""

from __future__ import annotations

from app.services.agent_loop.answer_dedupe import (
    collapse_duplicate_tail,
    is_duplicated_answer,
)


REPORT = (
    "## Last Week Sales Overview\n\n**Important data note up front:** the ERP "
    "sales view has no records after 2026-10-08.\n\nExecutive Summary\n\nThe "
    "most recent week booked 25.27M in revenue against 45.88M the week "
    "before — a 44.9% drop. That is a large, single-period contraction and "
    "the decisive story of this report: the business went from roughly "
    "45M/week to 25M/week at the point where the data currently stops. The "
    "decline is not explained by order fragmentation — average order value "
    "on the leading materials is consistent with the prior period's "
    "large-contract pattern. What the numbers point to is a narrowing of "
    "booked volume, with revenue concentrated in a handful of polyolefin "
    "intermediates: piperylene at 7.83M and dicyclopentadiene at 7.01M alone "
    "account for ~59% of the week's revenue.\n\nKey Numbers\n\nTotal revenue "
    "25,274,897.58 versus 45,880,864.91 prior week. Top material 间戊二烯 — "
    "7,832,000.00 (3 orders, 3 customers). #2 material 双环戊二烯 — "
    "7,008,200.00 (4 orders, 4 customers). Top-2 concentration ≈59% of "
    "weekly revenue.\n\nWant me to break the 25.27M down by customer now so "
    "you can see exactly which accounts moved?"
)


def test_collapses_direct_concatenated_duplicate():
    doubled = REPORT + REPORT  # the live failure shape: no separator
    out, fired = collapse_duplicate_tail(doubled)
    assert fired is True
    assert out == REPORT


def test_collapses_newline_separated_duplicate():
    doubled = REPORT + "\n\n" + REPORT
    out, fired = collapse_duplicate_tail(doubled)
    assert fired is True
    assert out == REPORT


def test_leaves_normal_content_alone():
    out, fired = collapse_duplicate_tail(REPORT)
    assert fired is False
    assert out == REPORT


def test_leaves_multi_section_content_alone():
    """A long answer whose second HALF is legitimately different text must
    never be touched — the guard only fires on near-identical halves."""
    other = REPORT.replace("25.27M", "44.9% drop week over week and a totally "
                           "different narrative body follows here")
    combined = REPORT + "\n\n" + other
    out, fired = collapse_duplicate_tail(combined)
    assert fired is False
    assert out == combined


def test_leaves_short_content_alone():
    """Below the minimum half length, accidental prefix repeats in short
    texts are untouched (guard is for full-answer duplication)."""
    short = "Hello. Hello."
    out, fired = collapse_duplicate_tail(short)
    assert fired is False
    assert out == short


def test_whitespace_normalization_matches_the_live_shape():
    """The live halves differed by ONE trailing newline and internal
    whitespace runs — normalization must absorb that."""
    doubled = REPORT + REPORT + "\n"
    out, fired = collapse_duplicate_tail(doubled)
    assert fired is True
    assert out.strip() == REPORT


def test_is_duplicated_answer_predicate():
    assert is_duplicated_answer(REPORT + REPORT) is True
    assert is_duplicated_answer(REPORT) is False
    assert is_duplicated_answer("") is False


def test_guard_wired_into_v3_final_assembly():
    """Source-level pin: the guard must run at final content assembly in
    the v3 loop (after the join/strips, before persist + done)."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "routers" / "agents.py"
    ).read_text()
    assert "collapse_duplicate_tail(" in src
    assert "duplicate-answer guard" in src


def test_more_than_two_copies_collapse_to_one():
    tripled = REPORT + REPORT + REPORT
    out, fired = collapse_duplicate_tail(tripled)
    assert fired is True
    assert out == REPORT


def test_preserves_leading_text_before_duplicate_pair():
    """A short genuine preamble followed by a doubled report keeps the
    preamble and ONE copy of the report."""
    preamble = "Here is the overview you asked for.\n\n"
    out, fired = collapse_duplicate_tail(preamble + REPORT + REPORT)
    assert fired is True
    assert out == preamble + REPORT
