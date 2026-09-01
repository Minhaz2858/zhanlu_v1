"""R4 regression test — Agent Builder system prompt discipline.

The user reported (screenshot 2026-07-13) that the agent_builder:
  1. Asked TWO clarifying questions in a single turn ("Do you have a
     specific database/schema?" + "what output format?")
  2. Looped on `list_tools` / `skills` calls 7+ times and never
     finished
  3. The user wants a "check list" — i.e. every clarifying question
     must end with a `:::options` block, and there should be a
     Decision Summary review step before the agent is created

This test pins down the new system-prompt rules in
``backend/app/services/agent_prompts.py``:

  - "ONE question per turn" is mandated in the prompt
  - The save-directly fast path is documented
  - The skill-discovery budget (1 list_tools, 1 list_market_agents,
    3 skills(load)) is documented
  - The Decision Summary review block (`:::decision-summary` with a
    JSON payload) is documented
"""
import os
import re

_PROMPTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "agent_prompts.py"
)
_PRINCIPLES_PATH = os.path.join(
    os.path.dirname(__file__), "..", "system_skills", "agent-builder-principles.md"
)


def _load_prompts():
    with open(_PROMPTS_PATH) as f:
        return f.read()


def _load_principles():
    with open(_PRINCIPLES_PATH) as f:
        return f.read()


def test_one_question_per_turn_in_system_prompt():
    """The agent_builder prompt must mandate ONE question per turn."""
    src = _load_prompts()
    assert re.search(r"ONE QUESTION PER TURN", src, re.IGNORECASE), (
        "AGENT_BUILDER_SYSTEM_PROMPT must include the literal phrase "
        "'ONE QUESTION PER TURN' so the LLM treats it as a hard rule."
    )
    assert re.search(r"NEVER chain a second question|Also\s*—", src), (
        "AGENT_BUILDER_SYSTEM_PROMPT must explicitly forbid chaining a "
        "second question like 'Also — what output format?'."
    )


def test_save_directly_fast_path_in_system_prompt():
    """The prompt must include the save-directly fast path rule."""
    src = _load_prompts()
    assert re.search(r"SAVE-DIRECTLY FAST PATH", src), (
        "AGENT_BUILDER_SYSTEM_PROMPT must include the SAVE-DIRECTLY "
        "FAST PATH section that tells the LLM to skip clarification "
        "when the user says 'save directly' / 'build it now' / etc."
    )
    for word in ("save", "directly", "create", "build it"):
        assert word.lower() in src.lower(), (
            f"Save-directly fast path must mention trigger word {word!r}"
        )


def test_skill_discovery_budget_in_system_prompt():
    """The prompt must include a bounded skill-discovery budget."""
    src = _load_prompts()
    assert re.search(r"SKILL-DISCOVERY BUDGET", src), (
        "AGENT_BUILDER_SYSTEM_PROMPT must include the SKILL-DISCOVERY "
        "BUDGET section so the LLM knows when to stop searching."
    )
    assert re.search(r"list_tools.*at most ONCE|at most ONCE.*list_tools", src, re.DOTALL), (
        "Budget must call out 'list_tools at most ONCE'."
    )
    assert re.search(r"skills\(action=load.*THREE|THREE times", src), (
        "Budget must call out 'skills(action=load, ...) at most THREE times'."
    )


def test_decision_summary_block_documented_in_system_prompt():
    """The prompt must document the `:::decision-summary` block format."""
    src = _load_prompts()
    assert ":::decision-summary" in src, (
        "AGENT_BUILDER_SYSTEM_PROMPT must include the literal "
        "`:::decision-summary` block syntax so the LLM emits it correctly."
    )
    # The required JSON keys
    for key in ("name", "description", "capabilities", "model", "agent_type", "skills"):
        assert key in src, (
            f"Decision-summary example payload must include key {key!r}."
        )


def test_always_use_options_checklist_in_system_prompt():
    """The prompt must mandate `:::options` for every clarifying question."""
    src = _load_prompts()
    assert re.search(r"ALWAYS USE A\s*`?:::options`?\s*CHECKLIST", src, re.IGNORECASE), (
        "AGENT_BUILDER_SYSTEM_PROMPT must include the rule 'ALWAYS USE A "
        ":::options CHECKLIST' so every clarifying question is a tap-pickable list."
    )


def test_principles_md_mirrors_new_rules():
    """The hidden system skill file must also be updated."""
    src = _load_principles()
    assert re.search(r"ONE question per turn", src), (
        "agent-builder-principles.md section 4 must include the literal "
        "'ONE question per turn' rule."
    )
    assert re.search(r"save directly|Save-directly", src, re.IGNORECASE), (
        "agent-builder-principles.md must include the save-directly fast path."
    )
    assert re.search(r"list_tools at most ONCE|list_tools.*ONCE", src, re.DOTALL), (
        "agent-builder-principles.md must include the bounded list_tools budget."
    )
