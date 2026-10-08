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


# ── Regression: the REAL provider shape for an array schema ───────────────
class TestLlmPlannerArraySchema:
    """``plan_dag`` is the only caller with a TOP-LEVEL array schema.

    The tests above feed ``{"response": "<json string>"}`` (the plain-text
    path), which is NOT what a provider in json_object mode returns.  The
    real shape is the parsed list, and llm_service used to put it ONLY in
    ``response`` while leaving ``data`` unset — so generate_plan read
    ``data -> None``, hit its fallback, and called ``.strip()`` on a list.
    The resulting AttributeError was swallowed into "using default plan".

    These tests pin the exact production shapes.
    """

    _STEPS = [
        {"node_type": "nl2sql", "name": "fetch_sales", "dependencies": [],
         "inputs": {"question": "sales by month"}, "expected_output": "rows"},
        {"node_type": "agent", "name": "summarise", "dependencies": [0],
         "inputs": {}, "expected_output": "answer"},
    ]

    def _plan(self, monkeypatch, db_session, llm_result):
        from app.services.synexia import plan_dag

        called = {"n": 0}

        async def fake_call_llm(**kw):
            called["n"] += 1
            return llm_result

        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        monkeypatch.setattr("app.config.settings.SYNEXIA_LLM_PLANNER_ENABLED", True)
        # These tests pin the provider-reply PARSING shapes, not the
        # hallucinated-name guard — whose verdict depends on global
        # tool-registry state warmed up by other tests in this file
        # ('agent:summarise' is a fake name and IS dropped when the
        # registry is populated). Neutralise the guard here.
        monkeypatch.setattr(
            plan_dag, "_drop_unknown_steps", lambda steps: (steps, [])
        )
        plan = plan_dag.generate_plan(
            db=db_session, execution_id="exec-arr",
            task_spec={"task_kind": "general", "requires_data": True, "user_signal": "default"},
            context_manifest={}, agent_name="general_assistant",
        )
        return plan, called

    def test_parsed_array_in_data_and_response(self, monkeypatch, db_session, caplog):
        """Provider shape: parsed list under ``data`` (post-fix) — the LLM's
        steps must be used and no fallback warning emitted."""
        result = {"model": "m", "usage": {}, "data": self._STEPS, "response": self._STEPS}
        plan, called = self._plan(monkeypatch, db_session, result)

        assert called["n"] == 1
        assert [n.name for n in plan.nodes] == ["fetch_sales", "summarise"]
        assert plan.nodes[0].inputs == {"question": "sales by month"}
        assert not [r for r in caplog.records if "LLM plan generation failed" in r.getMessage()]

    def test_list_only_in_response_still_parses(self, monkeypatch, db_session, caplog):
        """Legacy / cached shape: a list in ``response`` with no ``data``
        (what the pre-release fix and any llm_cache entry for the broken
        call both produced).  Must not raise, must not silently degrade."""
        result = {"model": "m", "usage": {}, "response": self._STEPS}
        plan, called = self._plan(monkeypatch, db_session, result)

        assert called["n"] == 1
        assert [n.name for n in plan.nodes] == ["fetch_sales", "summarise"]
        assert not [r for r in caplog.records if "LLM plan generation failed" in r.getMessage()]

    def test_fenced_array_text_still_parses(self, monkeypatch, db_session):
        """A provider that ignores json mode and wraps the array in a code
        fence must still plan correctly (the fence-stripping path)."""
        import json

        fenced = "```json\n" + json.dumps(self._STEPS) + "\n```"
        result = {"model": "m", "usage": {}, "response": fenced}
        plan, called = self._plan(monkeypatch, db_session, result)

        assert called["n"] == 1
        assert [n.name for n in plan.nodes] == ["fetch_sales", "summarise"]

    def test_schema_echo_envelope_is_planned(self, monkeypatch, db_session, caplog):
        """The shape DeepSeek returns LIVE for this schema: it echoes the
        envelope, with the real steps inside ``items``.  Before the fix this
        produced a dict under ``data`` (not a list), so the planner silently
        ran the default plan with no error at all."""
        envelope = {"type": "array", "items": self._STEPS}
        result = {"model": "m", "usage": {}, "data": envelope, "response": envelope,
                  "type": "array", "items": self._STEPS}
        plan, called = self._plan(monkeypatch, db_session, result)

        assert called["n"] == 1
        assert [n.name for n in plan.nodes] == ["fetch_sales", "summarise"]
        assert plan.nodes[0].inputs == {"question": "sales by month"}

    def test_fenced_envelope_text_is_planned(self, monkeypatch, db_session):
        """Envelope delivered as fenced TEXT (no parsed value at all) must
        still be planned — the text path has to unwrap too."""
        import json

        fenced = "```json\n" + json.dumps({"type": "array", "items": self._STEPS}) + "\n```"
        plan, called = self._plan(monkeypatch, db_session, {"model": "m", "usage": {}, "response": fenced})

        assert called["n"] == 1
        assert [n.name for n in plan.nodes] == ["fetch_sales", "summarise"]

    def test_unusable_reply_falls_back_loudly(self, monkeypatch, db_session, caplog):
        """A genuinely unusable reply must STILL fall back — but must say so.
        The silent fallback is what hid the original defect: every enabled
        planner turn ran the generic default plan while the only visible
        symptom was a downstream "dependencies not met" warning."""
        import logging

        caplog.set_level(logging.WARNING, logger="app.services.synexia.plan_dag")
        plan, called = self._plan(
            monkeypatch, db_session, {"model": "m", "usage": {}, "response": "I can't do that."}
        )

        assert plan is not None
        assert [n.name for n in plan.nodes] != ["fetch_sales", "summarise"]
        assert any(
            "no usable step list" in r.getMessage() for r in caplog.records
        ), f"expected a fallback warning; got {[r.getMessage() for r in caplog.records]}"
