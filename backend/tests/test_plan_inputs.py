"""Tests for Phase 3a: plan nodes carry concrete arguments (G4).

_build_default_plan (the live path) now populates `inputs` on each step
from task_spec entities, so tool/sandbox nodes don't execute with empty
args. The LLM planner path parses `inputs` from the LLM JSON too.
"""
from __future__ import annotations

import pytest

from app.services.synexia.plan_dag import _build_default_plan


class TestDefaultPlanInputs:
    def test_nl2sql_step_carries_question_input(self):
        steps = _build_default_plan(
            {"task_kind": "analyze_data", "requires_data": True,
             "user_message": "summarize Q2 sales", "user_signal": "default"},
            "general_assistant",
        )
        nl2sql = next(s for s in steps if s["node_type"] == "nl2sql")
        assert "question" in nl2sql["inputs"]
        assert nl2sql["inputs"]["question"] == "summarize Q2 sales"

    def test_sandbox_step_carries_title_and_format(self):
        steps = _build_default_plan(
            {"task_kind": "create_artifact", "requires_data": False,
             "artifact_intents": ["docx"], "user_signal": "default",
             "entities": {"report_title": "Q2 Report"}},
            "general_assistant",
        )
        sandbox = next(s for s in steps if s["node_type"] == "sandbox")
        assert sandbox["inputs"]["format"] == "docx"
        assert sandbox["inputs"]["title"] == "Q2 Report"

    def test_export_dag_sandbox_carries_format(self):
        steps = _build_default_plan(
            {"task_kind": "create_artifact", "requires_data": True,
             "artifact_intents": ["pptx"], "user_signal": "export_pptx"},
            "general_assistant",
        )
        sandbox = next(s for s in steps if s["node_type"] == "sandbox")
        assert sandbox["inputs"]["format"] == "pptx"

    def test_skill_step_carries_skill_name(self):
        steps = _build_default_plan(
            {"task_kind": "general", "forced_skill": True,
             "forced_skill_name": "sales-analyzer", "user_signal": "default"},
            "general_assistant",
        )
        skill = next(s for s in steps if s["node_type"] == "skill")
        assert skill["inputs"]["skill_name"] == "sales-analyzer"

    def test_selected_skill_step_carries_skill_id_and_name(self):
        steps = _build_default_plan(
            {"task_kind": "general", "user_signal": "default",
             "selected_skill": {"id": "tool-1", "name": "board-report"},
             "selected_skill_id": "tool-1", "selected_skill_name": "board-report"},
            "general_assistant",
        )
        skill = next(s for s in steps if s["node_type"] == "skill")
        assert skill["inputs"]["skill_name"] == "board-report"
        assert skill["inputs"]["skill_id"] == "tool-1"

    def test_every_step_has_inputs_key(self):
        steps = _build_default_plan(
            {"task_kind": "general", "requires_data": False,
             "user_signal": "default"}, "general_assistant",
        )
        assert steps, "expected at least one step"
        for s in steps:
            assert "inputs" in s and isinstance(s["inputs"], dict)


# ── In-memory sqlite session for generate_plan tests ───────────────────────
@pytest.fixture()
def db_session():
    import app.models  # noqa: F401  (register all models on Base)
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    with Session() as s:
        yield s


class TestLlmPlannerGated:
    def test_disabled_by_default_uses_default_plan(self, monkeypatch, db_session):
        """When the flag is off, generate_plan must NOT call the LLM."""
        from app.services.synexia import plan_dag
        called = {"n": 0}

        async def fake_call_llm(**kw):
            called["n"] += 1
            return {"response": "[]"}

        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        monkeypatch.setattr("app.config.settings.SYNEXIA_LLM_PLANNER_ENABLED", False)

        plan = plan_dag.generate_plan(
            db=db_session, execution_id="exec-x",
            task_spec={"task_kind": "general", "requires_data": False, "user_signal": "default"},
            context_manifest={}, agent_name="general_assistant",
        )
        assert plan is not None
        assert called["n"] == 0  # LLM never called when disabled
        # default-plan steps carry an inputs dict (G4 — never None/missing)
        assert all(isinstance(n.inputs, dict) for n in plan.nodes)

    def test_enabled_calls_llm_and_parses_inputs(self, monkeypatch, db_session):
        """When the flag is on, the LLM planner runs (properly awaited) and
        its step `inputs` flow into PlanNode.inputs."""
        from app.services.synexia import plan_dag
        called = {"n": 0}

        async def fake_call_llm(**kw):
            called["n"] += 1
            return {"response": '[{"node_type":"nl2sql","name":"q","dependencies":[],"inputs":{"question":"hello"}}]'}

        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        monkeypatch.setattr("app.config.settings.SYNEXIA_LLM_PLANNER_ENABLED", True)

        plan = plan_dag.generate_plan(
            db=db_session, execution_id="exec-y",
            task_spec={"task_kind": "general", "requires_data": False, "user_signal": "default"},
            context_manifest={}, agent_name="general_assistant",
        )
        assert called["n"] == 1  # LLM called exactly once
        assert len(plan.nodes) == 1
        assert plan.nodes[0].inputs == {"question": "hello"}
