"""Contract tests for the CLARIFY CARD PROTOCOL prompt block.

Pins that every interactive main-agent prompt (general assistant, power user,
generic fallback, and user-created agents assembled via
assemble_user_agent_prompt) now teaches the [[CLARIFY]] / [[CLARIFY_BATCH]]
card format — the ONLY format MessageBubble renders as clickable option cards
in the main chat. Guards against regressing to :::options (rendered only by
AgentMarkdown in builder/skill chats) and against contaminating the
agent_builder prompt (whose own chat UI legitimately uses :::options).
"""

from types import SimpleNamespace

from app.services import agent_prompts as ap

CARD_BLOCK = ap._CLARIFY_CARD_PROTOCOL_BLOCK


def _stub_agent_app(**overrides):
    base = {
        "name": "my_agent",
        "description": "A test agent.",
        "prompt_identity": "You are a test agent.",
        "prompt_boundary": "Do not do harmful things.",
        "prompt_reasoning": "Reason step by step.",
        "prompt_tools": "Use tools when needed.",
        "prompt_output": "Be concise.",
        "capabilities": ["web_search"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# The shared block itself
# ---------------------------------------------------------------------------

def test_block_teaches_both_card_formats():
    assert "[[CLARIFY]]" in CARD_BLOCK
    assert "[[CLARIFY_BATCH]]" in CARD_BLOCK
    assert '"prompt"' in CARD_BLOCK
    assert '"options"' in CARD_BLOCK
    assert "2-4 options" in CARD_BLOCK


def test_block_teaches_answer_binding_and_stop():
    assert "ANSWER-BINDING" in CARD_BLOCK
    assert "do NOT continue working or call more tools" in CARD_BLOCK


# ---------------------------------------------------------------------------
# Every interactive main-agent prompt carries the protocol
# ---------------------------------------------------------------------------

def test_general_assistant_prompt_carries_protocol():
    assert CARD_BLOCK in ap.GENERAL_ASSISTANT_SYSTEM_PROMPT


def test_power_user_prompt_carries_protocol():
    assert CARD_BLOCK in ap.POWER_USER_SYSTEM_PROMPT


def test_generic_prompt_carries_protocol():
    assert CARD_BLOCK in ap.GENERIC_AGENT_SYSTEM_PROMPT


def test_generic_prompt_no_longer_teaches_options():
    # The stale instruction told agents to use :::options, which the main
    # chat does NOT render as a card — the root cause of "agent never asks
    # with a card".
    assert ":::options block format with 2-4 concrete choices" not in ap.GENERIC_AGENT_SYSTEM_PROMPT
    assert "NOT rendered as a card" in ap.GENERIC_AGENT_SYSTEM_PROMPT


def test_user_created_agent_prompt_carries_protocol():
    prompt = ap.assemble_user_agent_prompt(_stub_agent_app())
    assert "[[CLARIFY]]" in prompt
    assert CARD_BLOCK in prompt


def test_user_agent_without_layers_still_falls_back_to_generic_protocol():
    # A user agent row with empty layers resolves to GENERIC via the runtime;
    # that fallback prompt itself must carry the protocol.
    assert CARD_BLOCK in ap.GENERIC_AGENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Guard rails: agent_builder keeps :::options; automation keeps [[CLARIFY]]
# ---------------------------------------------------------------------------

def test_agent_builder_still_uses_options_for_its_own_chat():
    # The Agent Builder chat renders :::options via AgentMarkdown — do NOT
    # overwrite its format with the [[CLARIFY]]-only rule.
    assert ":::options" in ap.AGENT_BUILDER_SYSTEM_PROMPT
    assert "[[CLARIFY]]" not in ap.AGENT_BUILDER_SYSTEM_PROMPT


def test_automation_agent_keeps_its_own_clarify_protocol():
    assert "[[CLARIFY]]" in ap.AUTOMATION_AGENT_SYSTEM_PROMPT
    assert "the ONLY format the chat UI renders as clickable option cards" in ap.AUTOMATION_AGENT_SYSTEM_PROMPT


def test_skill_agent_unchanged():
    # Skill-agent chat also renders :::options via AgentMarkdown.
    assert ":::options" in ap.SKILL_AGENT_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Regression: the agent must STOP after asking (suspend default ON) and the
# DATA answer-verification gate must not misfire on non-data turns.
# ---------------------------------------------------------------------------

def test_clarify_suspends_turn_default_on():
    # config.py default flipped ON 2026-09-02: after a clarify the v3 loop
    # breaks and waits for the user's answer instead of guessing and burning
    # the budget (the "created your automation" + bogus disclosure turn).
    from app.config import settings
    assert settings.CLARIFY_SUSPENDS_TURN_ENABLED is True


def test_gate_exempts_non_data_turns():
    from app.routers.agents import _turn_ran_data_tool
    # Data-row tools -> gate runs.
    assert _turn_ran_data_tool([{"name": "ask_data_agent"}]) is True
    assert _turn_ran_data_tool([{"name": "execute_query"}]) is True
    assert _turn_ran_data_tool([{"tool_name": "describe_schema"}]) is True
    # CRUD / discovery / deliverable turns -> gate skips (the misfire class:
    # automation creation ran list_knowledge_bases + AutomationTask.create and
    # got "requested dimensions not found in the data: send" appended).
    assert _turn_ran_data_tool([{"name": "AutomationTask.create"}]) is False
    assert _turn_ran_data_tool([{"name": "create_automation"}]) is False
    assert _turn_ran_data_tool([{"name": "list_knowledge_bases"}]) is False
    assert _turn_ran_data_tool([{"name": "save_flow"}]) is False
    assert _turn_ran_data_tool([{"name": "create_artifact"}]) is False
    assert _turn_ran_data_tool([]) is False
    assert _turn_ran_data_tool(None) is False


def test_clarify_tool_suspend_emits_deterministic_card():
    # Source contract: when the model asks via the clarify TOOL, the v3
    # finalize site must append a [[CLARIFY]] card (never trust prose) so the
    # chat always renders clickable options after a suspended turn.
    import pathlib
    agents_src = pathlib.Path(__file__).resolve().parents[1] / "app" / "routers" / "agents.py"
    src = agents_src.read_text(encoding="utf-8")
    assert '"[[CLARIFY" not in assistant_content' in src
    assert "appended [[CLARIFY]] card to final content" in src
    assert "suspending turn awaiting user answer" in src
