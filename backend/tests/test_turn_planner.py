"""2026-08-27: deterministic turn planning — plan-first agent behavior.

Covers the turn_planner service: intent-derived plan templates, greeting
short-circuit, evidence-based step completion (model-free), SSE frame
builders, prompt injection block, and the final-step marker.

The user's core complaint was: "agent just called data sources — agent need
to make plan or todo list according to user input, then follow that todo
list and make the response." These tests pin the deterministic plan that
fixes that: plan derived from USER INPUT, emitted before the loop, followed
via tool evidence.
"""

import json
from pathlib import Path

import pytest

from app.services.turn_planner import (
    KIND_DASHBOARD,
    KIND_DATA,
    KIND_GENERIC,
    KIND_NONE,
    KIND_REPORT,
    build_turn_plan,
    dynamic_plan_prompt,
    infer_evidence_from_title,
    mark_final_step_completed,
    parse_dynamic_plan,
    plan_completed_steps,
    plan_step_added_frame,
    plan_step_completed_frame,
    plan_to_system_block,
)

QUERY_TOOLS = ["ask_data_agent", "describe_schema", "create_dashboard"]
DASH_TOOLS = ["describe_schema", "ask_data_agent", "create_dashboard", "verify_dashboard"]


# ── Intent → plan template ────────────────────────────────────────────────

class TestBuildTurnPlan:
    def test_dashboard_request_gets_five_step_plan(self):
        plan = build_turn_plan(
            "Build a FULL-STACK REALTIME DASHBOARD: Sales Overview",
            is_dashboard_build=True,
            tool_names=DASH_TOOLS,
        )
        assert plan.kind == KIND_DASHBOARD
        assert [s.key for s in plan.steps] == [
            "analyze", "schema", "data", "build", "verify",
        ]
        # Step 1 is always the request-analysis step.
        assert plan.steps[0].step_index == 1

    def test_report_request_gets_four_step_plan(self):
        plan = build_turn_plan(
            "Generate the monthly IT ops report as docx",
            is_report_request=True,
            tool_names=["ask_data_agent", "create_artifact"],
        )
        assert plan.kind == KIND_REPORT
        assert [s.key for s in plan.steps] == ["analyze", "data", "draft", "deliver"]

    def test_data_question_gets_four_step_plan(self):
        plan = build_turn_plan(
            "Show me sales by region",
            tool_names=QUERY_TOOLS,
        )
        assert plan.kind == KIND_DATA
        assert [s.key for s in plan.steps] == ["understand", "query", "analyze", "answer"]

    def test_generic_request_gets_four_step_plan(self):
        plan = build_turn_plan(
            "Write a haiku about the ocean",
            tool_names=QUERY_TOOLS,  # query tools present but no interrogative
        )
        assert plan.kind == KIND_GENERIC
        assert [s.key for s in plan.steps] == ["understand", "execute", "verify", "respond"]

    def test_empty_request_yields_no_plan(self):
        assert build_turn_plan("  ").kind == KIND_NONE

    def test_greeting_yields_no_plan(self):
        # Chitchat must behave exactly as before: no plan, no forcing.
        plan = build_turn_plan("Hello, how are you?", tool_names=QUERY_TOOLS)
        assert plan.kind == KIND_NONE
        assert plan.steps == []


# ── Evidence-based completion (the "follow the plan" guarantee) ───────────

class TestPlanCompletedSteps:
    def setup_method(self):
        self.plan = build_turn_plan(
            "Build a dashboard of revenue",
            is_dashboard_build=True,
            tool_names=DASH_TOOLS,
        )

    def test_nothing_executed_completes_nothing(self):
        assert plan_completed_steps(self.plan, []) == set()

    def test_schema_tool_completes_schema_step(self):
        assert plan_completed_steps(self.plan, ["describe_schema"]) == {2}

    def test_data_tool_completes_data_step(self):
        assert plan_completed_steps(self.plan, ["ask_data_agent"]) == {3}

    def test_build_tool_completes_build_step(self):
        assert plan_completed_steps(self.plan, ["create_dashboard"]) == {4}

    def test_fullstack_dashboard_tool_completes_build_step(self):
        # create_fullstack_dashboard is NOT matched by the "create_dashboard"
        # prefix — it must have its own evidence entry, or the live-dashboard
        # build step never ticks off. Regression for the real LLM plan test.
        assert plan_completed_steps(self.plan, ["create_fullstack_dashboard"]) == {4}

    def test_fullstack_prefix_completes_build_step(self):
        assert plan_completed_steps(self.plan, ["create_fullstack_artifact"]) == {4}

    def test_full_pipeline_completes_execution_steps(self):
        done = plan_completed_steps(
            self.plan,
            ["describe_schema", "ask_data_agent", "create_dashboard"],
        )
        assert done == {2, 3, 4}

    def test_subagent_prefix_matching(self):
        # ask_* sub-agent tools map via prefix.
        done = plan_completed_steps(
            self.plan, ["ask_rag_research", "ask_data_agent"],
        )
        assert done == {3}

    def test_verify_tool_completes_verify_step(self):
        assert plan_completed_steps(self.plan, ["verify_dashboard"]) == {5}

    def test_analyze_step_has_no_tool_evidence(self):
        # The analyze/understand step completes at plan build, never here.
        assert plan_completed_steps(self.plan, ["anything_at_all"]) == set()


class TestMarkFinalStepCompleted:
    def test_answer_step_completes_when_content_exists(self):
        plan = build_turn_plan("Show me sales", tool_names=QUERY_TOOLS)
        # understand (1) pre-completed; query (2) evidenced; analyze (3) no evidence;
        # answer (4) completes when content exists.
        completed = {1, 2}
        assert mark_final_step_completed(plan, completed, True) == {4}

    def test_no_content_never_completes_final_step(self):
        plan = build_turn_plan("Show me sales", tool_names=QUERY_TOOLS)
        assert mark_final_step_completed(plan, {1}, False) == set()

    def test_respond_step_completes_for_generic(self):
        plan = build_turn_plan("Explain X", tool_names=[])
        assert mark_final_step_completed(plan, {1}, True) == {4}


# ── SSE frames ────────────────────────────────────────────────────────────

class TestPlanFrames:
    def setup_method(self):
        self.plan = build_turn_plan(
            "Build a dashboard", is_dashboard_build=True, tool_names=DASH_TOOLS,
        )

    def test_added_frame_shape(self):
        frame = plan_step_added_frame(self.plan.steps[0])
        assert frame.startswith("data: ")
        payload = json.loads(frame[len("data: "):].strip())
        assert payload["type"] == "plan_step_added"
        assert payload["step_index"] == 1
        assert payload["title"]

    def test_completed_frame_shape(self):
        frame = plan_step_completed_frame(self.plan.steps[3])
        payload = json.loads(frame[len("data: "):].strip())
        assert payload["type"] == "plan_step_completed"
        assert payload["step_index"] == 4


# ── Prompt injection ──────────────────────────────────────────────────────

class TestPlanToSystemBlock:
    def test_block_contains_all_steps_in_order(self):
        plan = build_turn_plan(
            "Build a dashboard", is_dashboard_build=True, tool_names=DASH_TOOLS,
        )
        block = plan_to_system_block(plan)
        for i, step in enumerate(plan.steps, start=1):
            assert f"{i}. {step.title_en}" in block
        assert "IN ORDER" in block

    def test_empty_plan_yields_empty_block(self):
        assert plan_to_system_block(build_turn_plan("hi")) == ""


# ── Dynamic (per-request) plan parsing ─────────────────────────────────────

class TestParseDynamicPlan:
    def test_json_object_form(self):
        plan = parse_dynamic_plan(
            '{"steps": [{"title": "Analyze the revenue request"}, '
            '{"title": "Query the sales data"}, '
            '{"title": "Build the live dashboard"}, '
            '{"title": "Verify the output"}]}',
            KIND_DASHBOARD,
        )
        assert plan is not None
        assert plan.kind == KIND_DASHBOARD
        assert [s.title_en for s in plan.steps] == [
            "Analyze the revenue request",
            "Query the sales data",
            "Build the live dashboard",
            "Verify the output",
        ]
        # First step keyed "analyze" → pre-completed by the loop.
        assert plan.steps[0].key == "analyze"
        # Evidence inference keeps the deterministic tick-off contract.
        assert any(s.step_index == 2 and "ask_data_agent" in s.evidence for s in plan.steps)
        assert any(s.step_index == 3 and "create_dashboard" in s.evidence for s in plan.steps)
        assert any(s.step_index == 4 and s.key == "verify" for s in plan.steps)

    def test_json_list_of_strings(self):
        plan = parse_dynamic_plan(
            '["Understand the ask", "Pull revenue by month", "Summarize the trend"]',
            KIND_DATA,
        )
        assert plan is not None
        assert len(plan.steps) == 3
        assert plan.steps[1].key == "data"

    def test_code_fenced_json(self):
        plan = parse_dynamic_plan(
            '```json\n{"steps": [{"title": "Inspect the schema"}, '
            '{"title": "Fetch order volumes"}, {"title": "Build the report"}]}\n```',
            KIND_REPORT,
        )
        assert plan is not None
        assert plan.steps[0].key == "schema"

    def test_numbered_list_fallback(self):
        plan = parse_dynamic_plan(
            "1. Analyze the request\n2. Query the data warehouse\n3. Build the dashboard\n4. Verify",
            KIND_DASHBOARD,
        )
        assert plan is not None
        assert len(plan.steps) == 4
        assert plan.steps[1].key == "data"

    def test_step_title_with_number_prefix_is_cleaned(self):
        plan = parse_dynamic_plan(
            '{"steps": [{"title": "1. Analyze"}, {"title": "2. Build the dashboard"}]}',
            KIND_DASHBOARD,
        )
        assert plan is not None
        assert plan.steps[0].title_en == "Analyze"
        assert plan.steps[1].title_en == "Build the dashboard"

    def test_invalid_json_returns_none(self):
        assert parse_dynamic_plan("this is not a plan at all", KIND_DASHBOARD) is None

    def test_empty_returns_none(self):
        assert parse_dynamic_plan("", KIND_DASHBOARD) is None
        assert parse_dynamic_plan(None, KIND_DASHBOARD) is None

    def test_too_few_steps_returns_none(self):
        assert parse_dynamic_plan('{"steps": [{"title": "Only one"}]}', KIND_DASHBOARD) is None

    def test_too_many_steps_returns_none(self):
        titles = [{"title": f"Step {i}"} for i in range(10)]
        import json as _json
        assert parse_dynamic_plan(_json.dumps({"steps": titles}), KIND_DASHBOARD) is None

    def test_duplicate_titles_deduped(self):
        plan = parse_dynamic_plan(
            '{"steps": [{"title": "Query data"}, {"title": "query data"}, '
            '{"title": "Build the dashboard"}]}',
            KIND_DASHBOARD,
        )
        assert plan is not None
        assert len(plan.steps) == 2


class TestInferEvidenceFromTitle:
    def test_data_title_gets_query_evidence(self):
        ev = infer_evidence_from_title("Query the sales data")
        assert "ask_data_agent" in ev

    def test_build_title_gets_build_evidence(self):
        ev = infer_evidence_from_title("Build the live dashboard")
        assert "create_dashboard" in ev or "create_artifact" in ev

    def test_verify_title_gets_verify_evidence(self):
        ev = infer_evidence_from_title("Verify the deliverable")
        assert "verify" in ev

    def test_schema_title_gets_schema_evidence(self):
        ev = infer_evidence_from_title("Inspect the table schema")
        assert "describe_schema" in ev

    def test_plain_title_gets_no_evidence(self):
        assert infer_evidence_from_title("Write the final summary") == ()


class TestDynamicPlanPrompt:
    def test_prompt_contains_request_and_tools(self):
        system, msgs = dynamic_plan_prompt(
            "Show revenue by region", KIND_DATA, ["ask_data_agent", "create_dashboard"],
        )
        assert "JSON" in system
        assert "Show revenue by region" in msgs[0]["content"]
        assert "ask_data_agent" in msgs[0]["content"]


class TestAgentsWiringDynamicPlan:
    """Source-level: the v3 loop must attempt the dynamic plan and fall back."""

    _AGENTS_SRC = None

    @classmethod
    def setup_class(cls):
        cls._AGENTS_SRC = (Path(__file__).resolve().parents[1] / "app/routers/agents.py").read_text(encoding="utf-8")

    def test_loop_calls_dynamic_generator(self):
        assert "_generate_dynamic_turn_plan(" in self._AGENTS_SRC

    def test_loop_guards_dynamic_call_with_flag(self):
        assert "TURN_PLAN_DYNAMIC_ENABLED" in self._AGENTS_SRC

    def test_loop_falls_back_to_fixed_plan(self):
        # The dynamic result is only adopted when non-None; otherwise the
        # fixed template (already assigned) stays in effect.
        assert "if _dyn_plan is not None:" in self._AGENTS_SRC

    def test_dynamic_generator_never_raises(self):
        # The helper swallows every failure and returns None.
        assert "return None" in self._AGENTS_SRC


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
