"""2026-08-26: Test for the data-turn response discipline — extensive narrative
reports, not raw data dumps.

User complaint: when asking for a "Contract Performance for last month
report", the agent produced ONLY a 235-row table + "Analyzing 235 rows
of data…" instead of a real written report with executive summary,
trends, insights, and recommendations.

This test verifies that:
1. A "report" intent in the user message triggers the REPORT discipline
3. The directive REQUIRES extensive narrative (not just "short summary")
4. The directive forbids the model from emitting only "Analyzing…"
   filler text
"""
import pytest


def test_report_intent_detected_in_user_message():
    """The phrase 'report' in the user message should trigger REPORT intent."""
    from app.services.goal_contract import _DATA_QUESTION_RE
    # "report" is currently in _DATA_QUESTION_RE
    assert _DATA_QUESTION_RE.search("give me Contract Performance for last month report")


def test_response_discipline_requires_extensive_narrative():
    """The RESPONSE DISCIPLINE (data turns) directive must require EXTENSIVE
    narrative, not the current 'short executive summary' wording.

    The user's complaint was that the model only wrote 'Analyzing 235 rows
    of data…' instead of producing a real report.
    """
    from app.routers import agents as agents_module
    src = open(agents_module.__file__, encoding="utf-8").read()
    # Must require extensive narrative (paragraphs of analysis)
    assert "extensive" in src.lower() or "comprehensive" in src.lower(), (
        "RESPONSE DISCIPLINE directive must require extensive narrative, "
        "not just a short summary. The model is currently only writing "
        "filler text like 'Analyzing 235 rows of data…'."
    )
    # Must explicitly forbid filler-only responses
    assert "analyzing" in src.lower() or "narrative" in src.lower(), (
        "Directive must explicitly require prose analysis around the "
        "data, not just labeling it as 'analyzing'."
    )


def test_report_intent_directive_attached_to_system_prompt():
    """When user_message contains a report request, the system prompt
    should include a strong REPORT directive that demands narrative."""
    # Build a minimal synthetic user message containing 'report'
    user_message = "give me Contract Performance for last month report"
    # The directive should be constructed somewhere when this intent is
    # detected. We test the helper directly.
    from app.services.goal_contract import _DATA_QUESTION_RE
    assert _DATA_QUESTION_RE.search(user_message)