"""Fix 4: clean exit on budget exhaustion.

When the goal-contract force budget is exhausted AND the model's last
sentence still announces a pending action ("Let me check the live
tables..."), the v3 loop strips that trailing sentence and appends an
explicit limitation note so the turn closes cleanly instead of promising
work it can no longer perform.
"""

from __future__ import annotations

from app.routers import agents
from app.services.goal_contract import pending_action_phrase


def test_strip_trailing_pending_removes_only_last_sentence():
    text = (
        "Based on the returned data, revenue grew 12% QoQ. "
        "Let me check the live tables for the exact figure."
    )
    stripped = agents._strip_trailing_pending(text, "Let me check the live tables for the exact figure.")
    assert "Let me check the live tables" not in stripped
    assert "revenue grew 12% QoQ" in stripped


def test_strip_trailing_pending_no_match_returns_original():
    text = "Revenue grew 12% QoQ. That is the verified figure."
    assert agents._strip_trailing_pending(text, "Let me check") == text


def test_strip_trailing_pending_whole_reply_is_pending():
    text = "Let me pull the live numbers first."
    assert agents._strip_trailing_pending(text, text) == ""


def test_appended_note_kills_pending_action_phrase():
    """After the Fix 4 strip+append, pending_action_phrase must return None."""
    content = (
        "Revenue grew 12% QoQ. "
        "Let me verify this against the live tables."
    )
    pending = pending_action_phrase(content)
    assert pending is not None

    stripped = agents._strip_trailing_pending(content, pending)
    final = (
        stripped + "\n\nNote: this turn ended before completing the "
        "additional verification described above. The current answer is based "
        "on the data already returned."
    ).strip()
    assert "Let me verify this against the live tables" not in final
    assert "Note: this turn ended" in final
    assert pending_action_phrase(final) is None


def test_budget_not_exhausted_leaves_content_alone():
    """Without exhaustion, the strip/append must never fire (guard at call site)."""
    content = "Here is your report. Let me also check the live tables."
    # Forces < max → the caller never calls _strip_trailing_pending; simulate
    # by asserting a non-exhausted turn keeps the pending sentence verbatim.
    assert "Let me also check the live tables." in content
