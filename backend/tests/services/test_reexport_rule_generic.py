"""Generic re-export rule regression tests.

Architecture rule (2026-08-29, user-directed): NO keyword-based intent
routers. The agent decides re-export vs new-analysis from conversation
context — the system prompt's RE-EXPORT HARD RULE + the T17 SESSION STATE
block (last execution_id) are the ONLY mechanism. The old T15 keyword
classifier (app/services/intent/export_intent.py) was deleted after it
hijacked new-topic turns ("give me a supply chain snapshot for last 30
days in html" → re-shipped the previous artifact, conv 945c7cf2).

These tests guard against re-introducing the keyword router.
"""

import importlib
import inspect
import pathlib

import pytest

from app.services.agent_prompts import _build_default_skills_block, get_system_prompt

AGENTS_PY = pathlib.Path(
    inspect.getfile(__import__("app.routers.agents"))
)


def test_generic_reexport_rule_present_in_system_prompt():
    """The LLM-decided re-export rule must be in the default skills block."""
    prompt = get_system_prompt("general_assistant")
    assert "RE-EXPORT / RE-FORMAT (HARD RULE)" in prompt
    assert "create_artifact(source_execution_id=" in prompt


def test_new_analysis_is_not_reexport_clause_present():
    """The semantic guard: a NEW topic is never a re-export, even with a
    format word. This is the exact failure mode of the deleted keyword
    router — the prompt must state it semantically (no keyword lists)."""
    block = _build_default_skills_block()
    assert "A request for a NEW analysis is NOT a re-export" in block
    assert "New topic or new time scope" in block
    # The two contrasting examples must both be in the clause.
    assert "supply chain" in block and "revenue" in block
    # The reference-to-prior-artifact phrasing must be in the clause.
    assert "the same" in block and "that report" in block


def test_no_keyword_router_in_agents_py():
    """The agent loop must contain ZERO references to the deleted router."""
    src = AGENTS_PY.read_text(encoding="utf-8")
    assert "detect_export_intent" not in src
    assert "export_intent" not in src
    assert "INTENT_ROUTER_ENABLED" not in src


def test_export_intent_module_removed():
    """The keyword classifier module must not exist."""
    with pytest.raises((ImportError, ModuleNotFoundError)):
        importlib.import_module("app.services.intent.export_intent")
