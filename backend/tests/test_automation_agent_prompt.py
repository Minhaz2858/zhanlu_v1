"""Prompt contract tests for the rewritten AUTOMATION_AGENT_SYSTEM_PROMPT.

Verifies:
  - The ``[[CLARIFY]]`` JSON single-select protocol is mandated.
  - The legacy ``:::options`` protocol is NOT used for disambiguation.
  - The answer-binding rule is present (a data-source answer MUST be
    bound via create_automation/update_automation, never treated as a
    report request).
  - The no-report/deliverable boundary is present.
"""
import os, sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _prompt():
    from app.services.agent_prompts import AUTOMATION_AGENT_SYSTEM_PROMPT
    return AUTOMATION_AGENT_SYSTEM_PROMPT


def test_clarify_protocol_present():
    """The prompt must reference the ``[[CLARIFY]]`` marker — the only
    disambiguation format MessageBubble.parseContent renders as clickable
    option cards."""
    p = _prompt()
    assert "[[CLARIFY]]" in p
    assert "[[END]]" in p


def test_options_protocol_not_used_for_disambiguation():
    """The prompt must NOT instruct the agent to use ``:::options`` for
    disambiguation. The ``:::options`` format is not rendered by the chat
    UI and was the root cause of the free-text data-source answer bug."""
    p = _prompt()
    # The prompt may mention :::options in a "do NOT use" context, but
    # must not instruct the agent to USE it. Check the CLARIFY PROTOCOL
    # and QUESTION RULES sections.
    clarify_section = p[p.index("CLARIFY PROTOCOL"):] if "CLARIFY PROTOCOL" in p else ""
    assert clarify_section, "CLARIFY PROTOCOL section not found"
    assert "NOT free text, NOT `:::options`" in clarify_section

    question_rules = p[p.index("QUESTION RULES"):] if "QUESTION RULES" in p else ""
    assert question_rules, "QUESTION RULES section not found"
    assert "Never use `:::options`" in question_rules


def test_answer_binding_rule_present():
    """The prompt must contain the ANSWER-BINDING RULE: a message
    answering a pending clarify (e.g. a data-source name) MUST be treated
    as a configuration answer — call create_automation/update_automation
    with data_source_id, never as a report request."""
    p = _prompt()
    assert "ANSWER-BINDING RULE" in p
    assert "data_source_id" in p
    assert "update_automation" in p
    # The rule must explicitly say the answer is a CONFIGURATION ANSWER
    # (not a report request).
    assert "CONFIGURATION ANSWER" in p


def test_no_report_boundary_present():
    """The prompt must contain the NO-REPORT BOUNDARY: the agent does NOT
    generate reports, dashboards, charts, or data analyses."""
    p = _prompt()
    assert "NO-REPORT BOUNDARY" in p
    assert "OUT OF SCOPE" in p
    assert "report" in p.lower()


def test_clarify_example_uses_json_shape():
    """The bottom example must show a ``[[CLARIFY]]`` JSON block (not a
    ``:::options`` block)."""
    p = _prompt()
    # The example block at the end of the prompt must contain a JSON
    # object with "prompt" and "options" keys inside [[CLARIFY]] markers.
    example_idx = p.rfind("[[CLARIFY]]")
    assert example_idx != -1, "No [[CLARIFY]] example block found"
    example = p[example_idx:]
    assert '"prompt"' in example
    assert '"options"' in example
    assert '"label"' in example
