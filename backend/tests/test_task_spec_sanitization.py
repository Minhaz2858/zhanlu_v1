"""TaskSpec / plan sanitization — free-form LLM output must never plan a
deliverable the pipeline cannot produce (2026-09-22, measured failure).

Turn: user asked "more details" after a successful inventory-lot answer.
The TaskSpec LLM invented ``artifact_intents=['elaboration']`` (not a type
the pipeline can produce) and ``user_signal='more details'``. plan_dag then
built a sandbox node "Generate elaboration" with format 'elaboration'; the
sandbox had no data rows, the turn failed with "output type 'elaboration'
isn't a supported format", and the agent answered from nothing.

Pins:
- invented artifact intents are dropped at the TaskSpec boundary;
- a create_artifact task left with no deliverable intent downgrades to a
  plain answer (which CAN elaborate from conversation or re-query);
- unknown user_signal values collapse to "default";
- valid intents pass through untouched;
- default plans only emit a sandbox node for a RENDERABLE format;
- plan steps carry sanitized output_artifact_type / format.
"""

from __future__ import annotations

from unittest.mock import patch

from app.services.synexia.plan_dag import (
    _build_default_plan,
    _sanitize_plan_step,
)
from app.services.synexia.task_spec_parser import parse_task_spec


def _fake_llm_spec(spec: dict):
    """Patch the sync LLM runner so parse_task_spec returns ``spec``."""

    def _fake_run(coro):
        return {"data": spec}

    return patch(
        "app.services.synexia.plan_dag._run_llm_sync", side_effect=_fake_run
    )


# ── TaskSpec boundary ─────────────────────────────────────────────────────


def test_invented_artifact_intent_dropped_and_task_downgraded():
    spec = {
        "task_kind": "create_artifact",
        "artifact_intents": ["elaboration"],
        "entities": {},
        "requires_data": False,
        "user_signal": "more details",
        "is_followup": True,
    }
    with _fake_llm_spec(spec):
        out = parse_task_spec("more details", conversation_context={})
    assert out["artifact_intents"] == []
    assert out["task_kind"] == "answer_question"
    assert out["user_signal"] == "default"


def test_valid_intents_and_signals_pass_through():
    spec = {
        "task_kind": "create_artifact",
        "artifact_intents": ["docx", "chart"],
        "entities": {},
        "requires_data": True,
        "user_signal": "export_docx",
        "is_followup": False,
    }
    with _fake_llm_spec(spec):
        out = parse_task_spec("make me a word report with a chart")
    assert out["artifact_intents"] == ["docx", "chart"]
    assert out["task_kind"] == "create_artifact"
    assert out["user_signal"] == "export_docx"


def test_mixed_intents_keep_only_known():
    spec = {
        "task_kind": "create_artifact",
        "artifact_intents": ["summary", "xlsx"],
        "entities": {},
        "requires_data": True,
        "user_signal": "default",
        "is_followup": False,
    }
    with _fake_llm_spec(spec):
        out = parse_task_spec("an xlsx of sales plus a summary")
    assert out["artifact_intents"] == ["xlsx"]
    assert out["task_kind"] == "create_artifact"


# ── Default plan gating ───────────────────────────────────────────────────


def test_default_plan_skips_sandbox_for_unrenderable_intent():
    steps = _build_default_plan(
        {
            "task_kind": "analyze_data",
            "artifact_intents": ["elaboration"],
            "requires_data": True,
            "user_message": "more details",
        },
        "data_agent",
    )
    assert not [s for s in steps if s["node_type"] == "sandbox"], (
        "an intent the sandbox cannot render must not become a doomed node"
    )
    assert any(s["node_type"] == "nl2sql" for s in steps)


def test_default_plan_keeps_sandbox_for_renderable_intent():
    steps = _build_default_plan(
        {
            "task_kind": "create_artifact",
            "artifact_intents": ["docx"],
            "requires_data": True,
            "user_message": "make a word report",
        },
        "data_agent",
    )
    sandbox = [s for s in steps if s["node_type"] == "sandbox"]
    assert sandbox and sandbox[0]["inputs"]["format"] == "docx"


# ── Plan step sanitizer (LLM-planner path) ────────────────────────────────


def test_sanitize_plan_step_drops_invented_artifact_type():
    step = {
        "node_type": "sandbox",
        "name": "Generate elaboration",
        "dependencies": [],
        "output_artifact_type": "elaboration",
        "inputs": {"title": "elaboration report", "format": "elaboration"},
    }
    out_type, inputs = _sanitize_plan_step(step)
    assert out_type is None
    assert "format" not in inputs


def test_sanitize_plan_step_keeps_renderable_type():
    step = {
        "node_type": "sandbox",
        "name": "Generate docx",
        "dependencies": [],
        "output_artifact_type": "docx",
        "inputs": {"title": "report", "format": "docx"},
    }
    out_type, inputs = _sanitize_plan_step(step)
    assert out_type == "docx"
    assert inputs["format"] == "docx"


def test_sanitize_plan_step_normalises_known_alias():
    step = {
        "node_type": "sandbox",
        "name": "Generate dataframe",
        "dependencies": [],
        "output_artifact_type": "dataframe",
        "inputs": {"format": "dataframe"},
    }
    out_type, inputs = _sanitize_plan_step(step)
    assert out_type == "xlsx"
    assert inputs["format"] == "xlsx"

# ── P0-B/P0-A: file-intent gate + report decomposition ────────────────────


def test_sandbox_steps_stripped_without_file_intent():
    """2026-10-10 (round 3): the user's file signal is authoritative. The
    planner habitually over-produces sandbox steps — live: 'show last week
    sales data' (TaskSpec artifacts=[]) got an unrequested xlsx from a
    data-fed format step. WITHOUT artifact_intents/export signal, ALL
    sandbox steps are stripped; WITH them, all are kept."""
    from app.services.synexia.plan_dag import _strip_sandbox_without_file_intent

    steps = [
        {"node_type": "nl2sql", "name": "Retrieve data", "dependencies": [], "inputs": {}},
        {"node_type": "sandbox", "name": "Generate sales_report",
         "dependencies": [0], "inputs": {"format": "docx"}},
    ]
    out = _strip_sandbox_without_file_intent(
        steps, {"artifact_intents": [], "user_signal": "default"}
    )
    assert [s["node_type"] for s in out] == ["nl2sql"]


def test_sandbox_steps_kept_with_file_intent():
    from app.services.synexia.plan_dag import _strip_sandbox_without_file_intent

    steps = [
        {"node_type": "nl2sql", "name": "Retrieve data", "dependencies": [], "inputs": {}},
        {"node_type": "sandbox", "name": "Generate docx",
         "dependencies": [0], "inputs": {"format": "docx"}},
    ]
    out = _strip_sandbox_without_file_intent(
        steps, {"artifact_intents": ["docx"], "user_signal": "export_docx"}
    )
    assert [s["node_type"] for s in out] == ["nl2sql", "sandbox"]


def test_report_decomposition_builds_per_dimension_queries():
    from app.services.synexia.plan_dag import _build_report_decomposition_steps

    steps = _build_report_decomposition_steps({
        "task_kind": "analyze_data",
        "requires_data": True,
        "user_message": "上周按产品、客户及组织划分的销售报告",
        "dimensions": ["产品", "客户", "组织"],
    })
    assert steps is not None
    nl2sql = [s for s in steps if s["node_type"] == "nl2sql"]
    # totals + one per dimension + prior-period twin
    assert len(nl2sql) == 5
    synth = [s for s in steps if s["node_type"] == "synthesize"]
    assert len(synth) == 1
    # synthesize depends on ALL the nl2sql nodes
    assert sorted(synth[0]["dependencies"]) == list(range(len(nl2sql)))


def test_report_decomposition_none_without_dimensions():
    from app.services.synexia.plan_dag import _build_report_decomposition_steps

    assert _build_report_decomposition_steps({
        "task_kind": "analyze_data", "requires_data": True,
        "user_message": "total sales", "dimensions": [],
    }) is None


def test_default_plan_uses_decomposition_when_dimensions_present():
    steps = _build_default_plan(
        {
            "task_kind": "analyze_data",
            "requires_data": True,
            "user_message": "上周按产品、客户及组织划分的销售报告",
            "dimensions": ["产品", "客户", "组织"],
            "user_signal": "default",
        },
        "data_agent",
    )
    assert len([s for s in steps if s["node_type"] == "nl2sql"]) == 5
    assert not [s for s in steps if s["node_type"] == "sandbox"]
