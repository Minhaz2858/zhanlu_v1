"""Tests for the depth and structure of builtin sub-agent system prompts.

These tests ensure each sub-agent prompt contains the guardrails expected
of a production-grade agent (anti-hallucination, tool guidelines, file-format
intent handling, autonomy contract). They are intentionally substring checks
so they survive rewording of the prompts.
"""

from app.services.agent_definitions import (
    GENERAL_PURPOSE_PROMPT,
    EXPLORE_PROMPT,
    PLAN_PROMPT,
    WORKER_PROMPT,
    VERIFICATION_PROMPT,
)


# ---------- general-purpose ----------

def test_general_use_prompt_has_anti_hallucination():
    assert "NO HALLUCINATION" in GENERAL_PURPOSE_PROMPT
    assert "FILE-FORMAT INTENT" in GENERAL_PURPOSE_PROMPT


def test_general_use_prompt_has_tool_guidelines():
    assert "TOOL USAGE GUIDELINES" in GENERAL_PURPOSE_PROMPT
    assert "web_search" in GENERAL_PURPOSE_PROMPT
    assert "execute_code" in GENERAL_PURPOSE_PROMPT


def test_general_use_prompt_has_autonomy_contract():
    assert "AUTONOMY" in GENERAL_PURPOSE_PROMPT


def test_general_use_prompt_has_error_recovery():
    assert "ERROR" in GENERAL_PURPOSE_PROMPT.upper() or "retry" in GENERAL_PURPOSE_PROMPT.lower()


# ---------- explore ----------

def test_explore_prompt_has_search_strategy():
    assert "SEARCH STRATEGY" in EXPLORE_PROMPT
    assert "entry point" in EXPLORE_PROMPT.lower()


def test_explore_prompt_has_output_structure():
    assert "ARCHITECTURE MAPPING" in EXPLORE_PROMPT
    assert "dependencies" in EXPLORE_PROMPT.lower()


def test_explore_prompt_is_read_only():
    assert "READ-ONLY" in EXPLORE_PROMPT


# ---------- plan ----------

def test_plan_prompt_has_risk_framework():
    assert "RISK" in PLAN_PROMPT
    assert "effort" in PLAN_PROMPT.lower() or "ESTIMATION" in PLAN_PROMPT


def test_plan_prompt_has_dependency_analysis():
    assert "dependency" in PLAN_PROMPT.lower()
    assert "alternative" in PLAN_PROMPT.lower()


def test_plan_prompt_is_plan_mode():
    assert "PLAN MODE" in PLAN_PROMPT


# ---------- worker ----------

def test_worker_prompt_has_verification_step():
    assert "VERIFY" in WORKER_PROMPT.upper() or "verification" in WORKER_PROMPT.lower()
    assert "confirm" in WORKER_PROMPT.lower()


def test_worker_prompt_has_error_classification():
    assert "transient" in WORKER_PROMPT.lower() or "retry" in WORKER_PROMPT.lower()


def test_worker_prompt_has_rollback_guidance():
    assert "ROLLBACK" in WORKER_PROMPT.upper() or "rollback" in WORKER_PROMPT.lower()


# ---------- verification ----------

def test_verification_prompt_has_severity_levels():
    assert "CRITICAL" in VERIFICATION_PROMPT
    assert "MAJOR" in VERIFICATION_PROMPT
    assert "MINOR" in VERIFICATION_PROMPT


def test_verification_prompt_has_evidence_standard():
    assert "file:" in VERIFICATION_PROMPT.lower() or "citation" in VERIFICATION_PROMPT.lower() or "evidence" in VERIFICATION_PROMPT.lower()


def test_verification_prompt_has_coverage_criteria():
    assert "COVERAGE" in VERIFICATION_PROMPT.upper() or "checklist" in VERIFICATION_PROMPT.lower()
