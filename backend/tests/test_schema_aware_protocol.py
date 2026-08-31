"""Tests for the generic Schema-Aware Query Protocol.

Verifies:
- `_agent_is_db_bound()` detects every db-bound case (known system agents,
  user agents with bound knowledge bases, the "Database Query" skill, or an
  explicit tool_config) without hardcoding table/column names.
- `get_system_prompt()` appends `_SCHEMA_AWARE_PROTOCOL_BLOCK` to ALL
  db-bound agents when `SCHEMA_GRAPH_ENABLED` is on, and to nobody else.
- The protocol block includes the medium-confidence EXTENSION OFFER rule
  (rule 9) so agents offer related-table joins instead of silently joining
  or ignoring them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services.agent_prompts import (
    _SCHEMA_AWARE_PROTOCOL_BLOCK,
    _agent_is_db_bound,
    get_system_prompt,
    settings,
)

# Known system agents that ship with DB tools in their default toolset.
DB_BOUND_SYSTEM_AGENTS = [
    "data_agent",
    "general_assistant",
    "automation_agent",
    "power_user",
]


def _make_agent_app(**overrides) -> MagicMock:
    """Minimal user-created agent_app shaped like the real AgentApp record.

    AgentApp columns (app/models/agent_app.py) are all nullable or defaulted,
    so every attribute the prompt assembler reads must default to None/[] here —
    otherwise MagicMock's auto-created attributes (truthy MagicMocks) would
    poison the 5-layer prompt assembly.
    """
    app = MagicMock()
    app.name = "Test Agent"
    app.description = None
    app.capabilities = []
    app.system_prompt_override = None
    app.prompt_identity = None
    app.prompt_boundary = None
    app.prompt_reasoning = None
    app.prompt_tools = None
    app.prompt_output = None
    app.skills = []
    app.tools = []
    app.tool_config = {}
    app.knowledge_bases = []
    for key, value in overrides.items():
        setattr(app, key, value)
    return app


class TestAgentIsDbBound:
    def test_known_system_agents_are_db_bound(self):
        for agent_name in DB_BOUND_SYSTEM_AGENTS:
            assert _agent_is_db_bound(agent_name) is True, (
                f"{agent_name} ships DB tools and must be detected as db-bound"
            )

    def test_non_db_system_agent_is_not_db_bound(self):
        assert _agent_is_db_bound("agent_builder") is False
        assert _agent_is_db_bound("report_agent") is False
        assert _agent_is_db_bound("nonexistent_agent_xyz") is False

    def test_user_agent_with_knowledge_base_is_db_bound(self):
        app = _make_agent_app(knowledge_bases=["sales_kb"])
        assert _agent_is_db_bound("custom_agent_123", agent_app=app) is True

    def test_user_agent_with_database_query_skill_is_db_bound(self):
        app = _make_agent_app(skills=["Database Query"])
        assert _agent_is_db_bound("custom_agent_123", agent_app=app) is True

    def test_user_agent_with_tool_config_is_db_bound(self):
        app = _make_agent_app(
            tool_config={"enabled_tools": ["execute_query", "web_search"]}
        )
        assert _agent_is_db_bound("custom_agent_123", agent_app=app) is True

    def test_plain_user_agent_is_not_db_bound(self):
        app = _make_agent_app()
        assert _agent_is_db_bound("custom_agent_123", agent_app=app) is False


class TestProtocolInjection:
    """get_system_prompt must append the protocol block to every db-bound
    agent when SCHEMA_GRAPH_ENABLED is on, and to nobody else."""

    def test_data_agent_gets_protocol_when_flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
        prompt = get_system_prompt("data_agent")
        assert "SCHEMA-AWARE QUERY PROTOCOL" in prompt

    def test_all_db_bound_system_agents_get_protocol(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
        for agent_name in DB_BOUND_SYSTEM_AGENTS:
            prompt = get_system_prompt(agent_name)
            assert "SCHEMA-AWARE QUERY PROTOCOL" in prompt, (
                f"{agent_name} is db-bound but got no schema protocol"
            )

    def test_user_agent_with_database_query_skill_gets_protocol(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
        app = _make_agent_app(skills=["Database Query"])
        prompt = get_system_prompt("custom_agent_123", agent_app=app)
        assert "SCHEMA-AWARE QUERY PROTOCOL" in prompt

    def test_no_protocol_when_flag_off(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", False)
        prompt = get_system_prompt("data_agent")
        assert "SCHEMA-AWARE QUERY PROTOCOL" not in prompt

    def test_no_protocol_for_non_db_agent(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
        prompt = get_system_prompt("report_agent")
        assert "SCHEMA-AWARE QUERY PROTOCOL" not in prompt

    def test_no_protocol_for_plain_user_agent(self, monkeypatch):
        monkeypatch.setattr(settings, "SCHEMA_GRAPH_ENABLED", True)
        app = _make_agent_app()
        prompt = get_system_prompt("custom_agent_123", agent_app=app)
        assert "SCHEMA-AWARE QUERY PROTOCOL" not in prompt


class TestExtensionOfferRule:
    """Rule 9: medium-confidence edges (0.5-0.8) → agent must offer, not
    silently join and not ignore."""

    def test_extension_offer_rule_is_present(self):
        lower = _SCHEMA_AWARE_PROTOCOL_BLOCK.lower()
        assert "extension offer" in lower, (
            "Protocol must contain the medium-confidence extension-offer rule"
        )
        assert "0.5" in lower and "0.8" in lower, (
            "Extension offer must target the 0.5-0.8 confidence band"
        )

    def test_extension_offer_does_not_cover_high_confidence_edges(self):
        # High-confidence edges (FK / VALUE_OVERLAP >= 0.8) are auto-joined
        # silently per rule 3 — the offer must NOT apply to them.
        block = _SCHEMA_AWARE_PROTOCOL_BLOCK.lower()
        assert "auto-joined silently" in block

    def test_extension_offer_skipped_in_unattended_runs(self):
        block = _SCHEMA_AWARE_PROTOCOL_BLOCK.lower()
        assert "unattended" in block or "scheduled" in block, (
            "Automation/unattended runs must skip the interactive offer"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
