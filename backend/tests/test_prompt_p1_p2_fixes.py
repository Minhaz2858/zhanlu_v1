"""Tests for P1/P2 prompt-engineering fixes (IBM audit, 2026-10-08).

P1-4: few-shot examples in synthesis / TaskSpec / automation prompts.
P1-5: NL2SQL planner requests json_object mode (fail-soft).
P1-6: numbered reasoning protocols in synthesis + FSM response prompts.
P2-7: versioned block registry.
P2-9: flag-gated reflexion pass.
"""
import json
from unittest.mock import AsyncMock

import pytest

from app.config import settings


# ---------------------------------------------------------------- P1-4

def test_synthesis_prompt_has_fewshot_and_protocol():
    from app.services.synexia.report_synthesis import _SYNTHESIS_SYSTEM_PROMPT
    assert "REASONING PROTOCOL" in _SYNTHESIS_SYSTEM_PROMPT
    assert "Step 1:" in _SYNTHESIS_SYSTEM_PROMPT
    assert "Step 5:" in _SYNTHESIS_SYSTEM_PROMPT
    assert "CONTRASTIVE EXAMPLES" in _SYNTHESIS_SYSTEM_PROMPT
    assert "GOOD (grounded" in _SYNTHESIS_SYSTEM_PROMPT
    assert "BAD (fabricated" in _SYNTHESIS_SYSTEM_PROMPT


def test_task_spec_prompt_has_fewshot_examples():
    from app.services.synexia import task_spec_parser

    # Examples live inside the f-string template — verify via parse of a
    # benign message (flag may be off; we only need the prompt path).
    # Simpler: the source must contain the example markers.
    import inspect
    src = inspect.getsource(task_spec_parser)
    assert 'Example 2 (follow-up' in src
    assert 'Do not invent' in src and '"elaboration"' in src


def test_task_spec_prompt_examples_are_valid_json():
    """The few-shot JSON blobs inside the prompt template must parse."""
    import inspect
    from app.services.synexia import task_spec_parser

    src = inspect.getsource(task_spec_parser)
    # Un-double the f-string braces, then extract balanced top-level objects
    # starting at {"task_kind" with a simple brace counter.
    flat = src.replace("{{", "{").replace("}}", "}")
    blobs = []
    idx = 0
    while True:
        start = flat.find('{"task_kind"', idx)
        if start == -1:
            break
        depth = 0
        for i in range(start, len(flat)):
            if flat[i] == "{":
                depth += 1
            elif flat[i] == "}":
                depth -= 1
                if depth == 0:
                    blobs.append(flat[start : i + 1])
                    break
        idx = start + 1
    assert len(blobs) >= 3
    for blob in blobs:
        obj = json.loads(blob)
        assert obj["task_kind"] in (
            "create_artifact", "answer_question", "analyze_data",
            "configure_system", "general",
        )


def test_automation_prompt_has_grounding_fewshot():
    from app.services.automation_executor import (
        _GROUNDING_FEWSHOT,
        _HONESTY_GUARDRAIL,
    )
    assert "GOOD:" in _GROUNDING_FEWSHOT and "BAD:" in _GROUNDING_FEWSHOT
    # The few-shot must be wired into the agent prompt construction.
    import inspect
    src = inspect.getsource(
        __import__("app.services.automation_executor", fromlist=["x"]))
    assert src.count("+ _GROUNDING_FEWSHOT") >= 1
    assert _HONESTY_GUARDRAIL in (
        _HONESTY_GUARDRAIL + _GROUNDING_FEWSHOT)


# ---------------------------------------------------------------- P1-5

def test_planner_chat_requests_json_object_mode():
    import inspect
    from app.services.db import nl2sql_planner
    src = inspect.getsource(nl2sql_planner)
    assert '"response_format"' in src or "'response_format'" in src
    assert '{"type": "json_object"}' in src


# ---------------------------------------------------------------- P1-6

def test_fsm_response_prompt_has_numbered_protocol():
    import inspect
    from app.services.synexia import fsm
    src = inspect.getsource(fsm.SynexiaFSM._build_response_prompt)
    assert "REASONING PROTOCOL" in src
    assert "Step 1: GROUND" in src
    assert "Step 4: SELF-CHECK" in src


# ---------------------------------------------------------------- P2-7

def test_registry_covers_shared_blocks_with_versions():
    from app.services.prompts.registry import BLOCK_REGISTRY, iter_blocks

    names = set(BLOCK_REGISTRY.keys())
    assert {
        "conversation_tone", "initiative", "runtime_date_anchor",
        "answer_verification", "schema_aware_protocol",
        "research_analyst_directive", "entity_master_filter",
        "grounding_required",
    } <= names
    for spec in iter_blocks():
        assert spec.version >= 1
        assert spec.description


def test_registry_blocks_resolve_and_are_stable():
    from app.services.prompts.registry import get_block

    text, version = get_block("conversation_tone")
    assert text.strip() and version >= 1
    text2, _ = get_block("conversation_tone")
    assert text == text2  # cached — stable within process


def test_registry_date_anchor_is_date_only():
    import re

    from app.services.prompts.registry import get_block

    text, _ = get_block("runtime_date_anchor")
    assert not re.search(r"\d{1,2}:\d{2}(:\d{2})?", text)


# ---------------------------------------------------------------- P2-9

@pytest.mark.asyncio
async def test_reflexion_disabled_by_default_returns_empty():
    from app.services.synexia.report_synthesis import _reflexion_pass

    llm = AsyncMock(return_value={"content": "revised prose" * 20})
    out = await _reflexion_pass(llm, "q", "draft " * 40, [{"a": 1}])
    assert out == ""
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_reflexion_enabled_revises(monkeypatch):
    from app.services.synexia import report_synthesis as rs

    monkeypatch.setattr(settings, "SYNTHESIS_REFLEXION_ENABLED", True)
    revised = "Corrected narrative. " * 20
    llm = AsyncMock(return_value={"content": revised})
    out = await rs._reflexion_pass(llm, "q", "draft " * 40, [{"a": 1}])
    assert out == revised.strip()
    # The critique prompt embeds the rubric + draft + rows.
    sent = llm.call_args[0][1][0]["content"]
    assert "FABRICATION" in sent and "DRAFT:" in sent


@pytest.mark.asyncio
async def test_reflexion_failure_is_non_fatal(monkeypatch):
    from app.services.synexia.report_synthesis import _reflexion_pass

    monkeypatch.setattr(settings, "SYNTHESIS_REFLEXION_ENABLED", True)
    llm = AsyncMock(side_effect=RuntimeError("llm down"))
    out = await _reflexion_pass(llm, "q", "draft " * 40, [{"a": 1}])
    assert out == ""


def test_reflexion_rubric_placeholders():
    from app.services.synexia.report_synthesis import _REFLEXION_RUBRIC
    assert "{user_message}" in _REFLEXION_RUBRIC
    assert "{rows_json}" in _REFLEXION_RUBRIC
    assert "{draft}" in _REFLEXION_RUBRIC
