"""Dynamic orchestration planning (2026-11-02, Phase 1 + Phase 4/5 events).

The Task Execution timeline must reflect the REAL plan, not a fixed
four-step template. The dynamic planner (parse_dynamic_plan) already
exists; these tests pin the new capabilities:

- every step carries a STABLE task_id (same task -> same id across
  replans; content-derived, not position-derived);
- steps carry user-friendly descriptions and depends_on when the planner
  provides them;
- the plan bounds widen to 2..8 steps (complex enterprise analyses);
- replanning frames exist: plan_updated (supersede/fail/replace a step)
  and replanning_started — the v3 loop never had a way to say "the plan
  changed";
- plan_step_added/completed frames now carry task_id so the frontend can
  preserve identity across plan updates.

All pure functions — no LLM calls (the planner LLM output is simulated
with fixture strings, per the no-paid-LLM-calls constraint).
"""

from __future__ import annotations

import json
import os
import sys

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.turn_planner import (  # noqa: E402
    DYNAMIC_PLAN_MAX_STEPS,
    TurnStep,
    parse_dynamic_plan,
    plan_replan_started_frame,
    plan_step_added_frame,
    plan_step_completed_frame,
    plan_step_updated_frame,
)


# ── task identity ─────────────────────────────────────────────────────────

def test_dynamic_plan_steps_have_stable_task_ids():
    plan = parse_dynamic_plan(json.dumps({
        "steps": [
            {"title": "Gather dealer sales data", "description": "Query the bound data source"},
            {"title": "Compare regional performance"},
            {"title": "Draft the summary report"},
        ],
    }), "data")
    assert plan is not None
    ids = [s.task_id for s in plan.steps]
    assert all(ids), "every step gets a task_id"
    assert len(set(ids)) == len(ids), "task ids are unique within a plan"
    # Same content -> same id (stable across replans / re-parses).
    reparsed = parse_dynamic_plan(json.dumps({
        "steps": [
            {"title": "Gather dealer sales data"},
            {"title": "Compare regional performance"},
            {"title": "Draft the summary report"},
        ],
    }), "data")
    assert [s.task_id for s in reparsed.steps] == ids


def test_reordering_preserves_task_identity_by_content():
    """A replan that keeps a task (same title) keeps its id even when the
    step moves position; genuinely new tasks get fresh ids."""
    first = parse_dynamic_plan(json.dumps({
        "steps": [{"title": "Gather data"}, {"title": "Analyze"}, {"title": "Report"}],
    }), "generic")
    second = parse_dynamic_plan(json.dumps({
        "steps": [{"title": "Gather data"}, {"title": "Report"}, {"title": "Sanity-check figures"}],
    }), "generic")
    id_of = {s.title_en: s.task_id for s in first.steps}
    ids2 = {s.title_en: s.task_id for s in second.steps}
    assert ids2["Gather data"] == id_of["Gather data"]
    assert ids2["Report"] == id_of["Report"]
    assert "Sanity-check figures" not in id_of  # genuinely new task


# ── descriptions / dependencies / bounds ──────────────────────────────────

def test_description_and_depends_on_parsed():
    plan = parse_dynamic_plan(json.dumps({
        "steps": [
            {"title": "Gather data", "description": "Query MySQL via ask_data_agent",
             "depends_on": []},
            {"title": "Analyze", "description": "Compare dealer performance",
             "depends_on": [1]},
        ],
    }), "data")
    assert plan is not None
    s2 = plan.steps[1]
    assert s2.description == "Compare dealer performance"
    assert s2.depends_on == (1,)


def test_plain_title_list_still_parses_with_defaults():
    plan = parse_dynamic_plan("1. Gather data\n2. Analyze results\n3. Write report", "report")
    assert plan is not None and len(plan.steps) == 3
    for s in plan.steps:
        assert s.task_id
        assert s.description is None
        assert s.depends_on == ()


def test_bounds_widen_to_eight_steps():
    assert DYNAMIC_PLAN_MAX_STEPS == 8
    titles = [{"title": f"Step number {i} does real work"} for i in range(1, 9)]
    plan = parse_dynamic_plan(json.dumps({"steps": titles}), "generic")
    assert plan is not None and len(plan.steps) == 8
    # 9 steps still rejected
    titles.append({"title": "One step too many for this plan"})
    assert parse_dynamic_plan(json.dumps({"steps": titles}), "generic") is None


# ── replanning frames ─────────────────────────────────────────────────────

def test_plan_step_updated_frame_carries_identity_and_status():
    step = TurnStep(2, "analyze", "act", "Analyze dealer performance", "分析经销商表现")
    step.task_id = "task-abc123"
    frame = plan_step_updated_frame(step, status="superseded")
    payload = json.loads(frame.removeprefix("data: ").strip())
    assert payload["type"] == "plan_updated"
    assert payload["step_index"] == 2
    assert payload["task_id"] == "task-abc123"
    assert payload["status"] == "superseded"
    assert payload["title"]


def test_plan_replan_started_frame():
    payload = json.loads(plan_replan_started_frame().removeprefix("data: ").strip())
    assert payload["type"] == "replanning_started"
    assert payload.get("reason")


def test_step_added_and_completed_frames_carry_task_id():
    step = TurnStep(1, "data", "act", "Gather the required data", "采集所需数据")
    step.task_id = "task-xyz789"
    added = json.loads(plan_step_added_frame(step).removeprefix("data: ").strip())
    done = json.loads(plan_step_completed_frame(step).removeprefix("data: ").strip())
    assert added["task_id"] == "task-xyz789"
    assert done["task_id"] == "task-xyz789"
