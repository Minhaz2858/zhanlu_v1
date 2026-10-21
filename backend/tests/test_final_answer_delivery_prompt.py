"""Final Answer Delivery Contract — prompt wiring tests (2026-10-20).

Live failure that motivated the contract (2026-10-19/20, Data analysis agent):
- "Finalizing your answer" stalled through post-answer gates (figure-grounding
  re-synthesis, refinement budget, deferred synthesis) while the user waited.
- The final bubble surfaced a raw 50-row CSV-like table + "View SQL query"
  instead of a synthesized narrative; a degenerate 1-record result was
  narrated with pipeline vocabulary ("WHERE clause", column names).

The contract block (_FINAL_ANSWER_DELIVERY_BLOCK) must be injected into every
db-bound agent's system prompt when FINAL_ANSWER_DELIVERY_ENABLED is on, and
must be absent when the flag is off (rollback path). Non-db-bound agents are
unaffected.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.agent_prompts import (
    _FINAL_ANSWER_DELIVERY_BLOCK,
    _agent_is_db_bound,
    get_system_prompt,
)


class _App:
    """Minimal AgentApp stand-in (plain attrs, no auto-MagicMock attrs)."""

    def __init__(self, knowledge_bases=None, skills=None):
        self.knowledge_bases = knowledge_bases or []
        self.skills = skills or []
        self.tool_config = {}
        self.org_id = None


# Marker phrases that must survive any future rewording — they ARE the contract.
_MARKERS = [
    "FINAL ANSWER DELIVERY CONTRACT",
    "FINALIZATION TIME BUDGET",
    "ONE final synthesis pass",
    "NEVER end the turn with only a data table",
    "NEVER expose pipeline internals",
    "HIDDEN BY DEFAULT, ACCESSIBLE ON REQUEST",
    "View SQL",
    "SEPARATE THE ANSWER FROM ITS SOURCES",
    "One-line headline",
    "Sources line",
]


def _db_bound_app() -> _App:
    return _App(knowledge_bases=["kb-1"])


class TestBlockContent:
    def test_block_contains_all_contract_markers(self):
        for marker in _MARKERS:
            assert marker in _FINAL_ANSWER_DELIVERY_BLOCK, (
                f"contract marker missing from block: {marker!r}"
            )

    def test_block_forbids_degenerate_result_narration(self):
        assert "degenerate" in _FINAL_ANSWER_DELIVERY_BLOCK
        assert "Pulling the data" in _FINAL_ANSWER_DELIVERY_BLOCK

    def test_block_caps_inline_preview(self):
        # ≤ 5 rows preview allowed; everything else collapsed/attached.
        assert "5 rows" in _FINAL_ANSWER_DELIVERY_BLOCK


class TestInjection:
    def test_db_bound_agent_prompt_contains_contract(self):
        prompt = get_system_prompt("general_assistant", _db_bound_app())
        assert "FINAL ANSWER DELIVERY CONTRACT" in prompt

    def test_flag_off_removes_contract(self):
        with patch("app.services.agent_prompts.settings") as mock_settings:
            mock_settings.FINAL_ANSWER_DELIVERY_ENABLED = False
            # Neighbor flags the prompt builder may consult.
            mock_settings.SELF_EVAL_REPLAN_ENABLED = False
            mock_settings.SCHEMA_GRAPH_ENABLED = False
            mock_settings.COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED = False
            mock_settings.ENTITY_MASTER_FILTER_ENABLED = False
            mock_settings.ENTERPRISE_PIPELINE_ENABLED = False
            mock_settings.GOAL_CONTRACT_ENABLED = False
            mock_settings.DELIVERABLE_PHASE_LOCK_ENABLED = False
            prompt = get_system_prompt("general_assistant", _db_bound_app())
        assert "FINAL ANSWER DELIVERY CONTRACT" not in prompt

    def test_non_db_bound_agent_does_not_get_contract(self):
        # No KBs, no DB skill, no DB tools → not db-bound → contract absent.
        app = _App()
        assert not _agent_is_db_bound("some_agent", app)
        prompt = get_system_prompt("some_agent", app)
        assert "FINAL ANSWER DELIVERY CONTRACT" not in prompt

    def test_db_bound_user_agent_with_db_skill_gets_contract(self):
        app = _App(skills=["Database Query"])
        assert _agent_is_db_bound("user_agent_x", app)
        prompt = get_system_prompt("user_agent_x", app)
        assert "FINAL ANSWER DELIVERY CONTRACT" in prompt
