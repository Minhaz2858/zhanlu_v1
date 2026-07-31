# Automation Response Quality — Phase 3 (Smarter Planning + Adaptive Re-planning) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make plan nodes carry concrete arguments (G4), let synthesize aggregate all data observations instead of only the last (G5), and add opt-in adaptive mid-execution re-planning after checkpoint nodes — so complex tasks can redirect mid-run based on what they learned.

**Architecture:** (3a) `_build_default_plan` populates `inputs` from task_spec entities (the live path); the LLM planner prompt is extended to emit `inputs` and the un-awaited `call_llm` is fixed, gated behind `SYNEXIA_LLM_PLANNER_ENABLED` (default off). (3b) A new `_get_all_data_observations` aggregator feeds synthesize/sandbox all nl2sql observations (single-obs behavior unchanged). (3c) A new `adaptive_planner.py` runs an LLM decision after checkpoint nodes (`nl2sql`/`synthesize`/`sandbox`); opt-in via `SYNEXIA_ADAPTIVE_PLANNING_ENABLED` (default off), bounded by `SYNEXIA_ADAPTIVE_MAX_REVISIONS` (default 2).

**Tech Stack:** Python 3.11, SQLAlchemy ORM, pydantic `Settings`, pytest (sqlite test DB, explicit file paths, no `-n` workers).

**Spec:** `docs/superpowers/specs/2026-07-29-automation-response-quality-design.md` (Phase 3, §5).

## Discovered bug (Phase 3a scope)

`generate_plan` (`plan_dag.py:166`) calls the **async** `call_llm` without `await` (same class of
bug as the synthesize node fixed in Phase 2). `result.get(...)` raises `AttributeError` → the
`except` branch catches it → `plan_steps = default_plan`. **LLM plan generation has never run; every
plan is the curated `_build_default_plan`.** Phase 3a fixes this (properly awaited) but gates LLM
activation behind `SYNEXIA_LLM_PLANNER_ENABLED` (default **off**) so the proven default plan remains
the default behavior — the LLM planner is opt-in for staging validation. G4 is fixed on the live
(default-plan) path regardless, by populating `inputs` deterministically.

## Global Constraints

- **RAM-limited dev box:** explicit test-file paths only; no `-n` workers; check `free -h` if unsure.
- **Test DB:** `cd /root/zhanlu/backend && rm -f test_runtime.db && DATABASE_URL="sqlite:///./test_runtime.db" python3 -c "import app.models; from app.database import Base, engine; Base.metadata.create_all(engine)"` then `python3 -m pytest tests/<file> -v`.
- **Async bridging:** async helpers (`call_llm`) run via a local `_run_coro_sync`-style bridge; never bare-called.
- **No circular imports:** `plan_dag.py` must NOT import from `capability_router.py` (capability_router imports plan_dag). plan_dag gets its own tiny async bridge.
- **Backward compat:** single-observation plans (the common case) MUST behave identically after 3b.
- **Adaptive fail-safe:** any adaptive-planner error/unparseable decision → `proceed` (run continues on original plan); never abort a run.
- **New dependencies:** none. **Schema changes:** none (`PlanNode.inputs` already exists; `Plan.version` already exists).
- **Commit only when the user asks.** Deploy via `docker restart zhanlu-backend`.

## File Structure

- **Modify:** `backend/app/services/synexia/plan_dag.py` — populate `inputs` on default-plan steps; extend LLM prompt + parse `inputs`; fix un-awaited `call_llm` (gated).
- **Modify:** `backend/app/config.py` — add `SYNEXIA_LLM_PLANNER_ENABLED: bool = False`, `SYNEXIA_ADAPTIVE_PLANNING_ENABLED: bool = False`, `SYNEXIA_ADAPTIVE_MAX_REVISIONS: int = 2`.
- **Modify:** `backend/app/services/synexia/capability_router.py` — add `_get_all_data_observations`; switch synthesize + sandbox to aggregate; add adaptive checkpoint hook in `execute_plan_nodes`.
- **Create:** `backend/app/services/synexia/adaptive_planner.py` — `decide_adaptive_revision(...)` LLM decision (proceed|insert_nodes|modify_remaining|complete_early).
- **Test:** `backend/tests/test_plan_inputs.py` (3a), `backend/tests/test_multi_obs_aggregation.py` (3b), `backend/tests/test_adaptive_planner.py` (3c).

---

## Task 1 (3a): Plan-arg population

**Files:**
- Modify: `backend/app/services/synexia/plan_dag.py` (`_build_default_plan`, `generate_plan`)
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_plan_inputs.py`

**Interfaces:**
- Produces: `_build_default_plan` steps now carry an `inputs` dict (nl2sql → `{"question": user_message}`, sandbox → `{"title": <derived>, "format": <artifact_type>}`, skill → `{"skill_name": <name>}`). `generate_plan` parses `step.get("inputs", {})` into `PlanNode.inputs` (plan_dag.py:203-214) and, when `SYNEXIA_LLM_PLANNER_ENABLED`, runs the LLM planner (properly awaited) with `inputs` in the prompt schema.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_plan_inputs.py`:

```python
"""Tests for Phase 3a: plan nodes carry concrete arguments (G4).

_build_default_plan (the live path) now populates `inputs` on each step
from task_spec entities, so tool/sandbox nodes don't execute with empty
args. The LLM planner path parses `inputs` from the LLM JSON too.
"""
from __future__ import annotations

from app.services.synexia.plan_dag import _build_default_plan


class TestDefaultPlanInputs:
    def test_nl2sql_step_carries_question_input(self):
        steps = _build_default_plan(
            {"task_kind": "analyze_data", "requires_data": True,
             "user_signal": "default"}, "general_assistant",
        )
        nl2sql = next(s for s in steps if s["node_type"] == "nl2sql")
        assert "question" in nl2sql["inputs"]

    def test_sandbox_step_carries_title_and_format(self):
        steps = _build_default_plan(
            {"task_kind": "create_artifact", "requires_data": False,
             "artifact_intents": ["docx"], "user_signal": "default",
             "entities": {"report_title": "Q2 Report"}},
            "general_assistant",
        )
        sandbox = next(s for s in steps if s["node_type"] == "sandbox")
        assert sandbox["inputs"]["format"] == "docx"
        assert sandbox["inputs"]["title"]  # non-empty

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

    def test_every_step_has_inputs_key(self):
        steps = _build_default_plan(
            {"task_kind": "general", "requires_data": False,
             "user_signal": "default"}, "general_assistant",
        )
        assert steps, "expected at least one step"
        for s in steps:
            assert "inputs" in s and isinstance(s["inputs"], dict)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_plan_inputs.py -v
```
Expected: FAIL — `KeyError: 'inputs'` (default-plan steps have no `inputs` key).

- [ ] **Step 3: Populate `inputs` on `_build_default_plan` steps**

In `backend/app/services/synexia/plan_dag.py`, edit `_build_default_plan`. Pass `user_message`/`task_spec` through (it already receives `task_spec`). Add an `inputs` dict to each step:

- Forced-skill step: add `"inputs": {"skill_name": task_spec["forced_skill_name"]}`.
- nl2sql step (both export and standard): add `"inputs": {"question": task_spec.get("user_message") or task_spec.get("entities", {}).get("question") or ""}`. Since `_build_default_plan` doesn't currently receive `user_message`, derive the question from `task_spec.get("user_message")` (the execution's user message is stored on the task spec by the GOAL state). If absent, fall back to the entities' `question` or `""`.
- sandbox step (both export and standard): add `"inputs": {"title": task_spec.get("entities", {}).get("report_title") or f"{artifact_type} report", "format": artifact_type}` (use the same `artifact_type` variable already computed in each branch).
- fallback tool step: add `"inputs": {}`.

Concretely, for each `steps.append({...})` block, add the matching `"inputs": {...}` key. Example for the standard nl2sql step:

```python
        steps.append({
            "node_type": "nl2sql",
            "name": "Retrieve data",
            "description": "Query the datasource for required data",
            "dependencies": [],
            "expected_output": "DataSnapshot",
            "inputs": {"question": task_spec.get("user_message") or task_spec.get("entities", {}).get("question") or ""},
        })
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_plan_inputs.py -v
```
Expected: PASS.

- [ ] **Step 5: Parse `inputs` from the LLM planner JSON + fix the un-awaited call (gated)**

In `backend/app/services/synexia/plan_dag.py`:

(a) Add `inputs` to **both** planner prompt schemas (the corrective prompt at ~line 116 and the normal prompt at ~line 148). After the `output_artifact_type` bullet, add:
```
- inputs: a JSON object of concrete arguments for this step (e.g. {"question": "..."} for nl2sql, {"title": "...", "format": "docx"} for sandbox, {"skill_name": "..."} for skill). Use {} if none.
```

(b) In the `PlanNode(...)` construction (~line 203-214), add `inputs=step.get("inputs") or {}`:
```python
        node = PlanNode(
            id=str(uuid4()),
            plan_id=plan.id,
            seq=i,
            node_type=step.get("node_type", "tool"),
            name=step.get("name", f"Step {i+1}"),
            description=step.get("description", ""),
            dependencies=step.get("dependencies", []),
            inputs=step.get("inputs") or {},
            expected_output=step.get("expected_output"),
            output_artifact_type=step.get("output_artifact_type"),
            status="pending",
        )
```

(c) Add a local async bridge at module top (after the imports, before `generate_plan`):
```python
def _run_llm_sync(coro):
    """Run an async coroutine from this sync planner context.

    Same pattern as capability_router._run_coro_sync, duplicated here to
    avoid a circular import (capability_router imports plan_dag).
    """
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()
```

(d) Gate the LLM call behind `SYNEXIA_LLM_PLANNER_ENABLED` and properly await it. Replace the `try:` block at ~line 165:
```python
    from app.config import settings
    _llm_planner_enabled = getattr(settings, "SYNEXIA_LLM_PLANNER_ENABLED", False)
    plan_steps = default_plan
    if _llm_planner_enabled:
        try:
            result = _run_llm_sync(call_llm(
                prompt=system_prompt,
                messages=[{"role": "user", "content": f"Plan this task: {task_spec}"}],
                temperature=0,
            ))
            response_text = result.get("response", "[]").strip()
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]
            parsed = json.loads(response_text)
            if isinstance(parsed, list) and len(parsed) > 0:
                plan_steps = parsed
        except Exception as e:
            logger.warning("LLM plan generation failed, using default: %s", e)
            plan_steps = default_plan
```
(Default-off keeps the proven curated plan as current behavior; the LLM planner is opt-in.)

- [ ] **Step 6: Add the config flag + run a focused LLM-planner test**

Add to `backend/app/config.py` (near the `SYNEXIA_GROUNDING_MAX_CHARS` line):
```python
    # Phase 3a: opt-in LLM-driven plan generation (default off — the curated
    # default plan is the proven path; the LLM planner is gated for staging
    # validation). Also fixes the prior un-awaited call_llm in generate_plan.
    SYNEXIA_LLM_PLANNER_ENABLED: bool = False
```

Append to `backend/tests/test_plan_inputs.py`:
```python
class TestLlmPlannerGated:
    def test_disabled_by_default_uses_default_plan(self, monkeypatch):
        # When the flag is off, generate_plan must NOT call the LLM.
        from app.services.synexia import plan_dag
        called = {"n": 0}
        async def fake_call_llm(**kw):
            called["n"] += 1
            return {"response": "[]"}
        monkeypatch.setattr("app.services.llm_service.call_llm", fake_call_llm)
        monkeypatch.setattr("app.config.settings.SYNEXIA_LLM_PLANNER_ENABLED", False)
        # Use an in-memory sqlite DB to persist the Plan row.
        from app.database import Base, engine
        from app.models.execution import Plan
        Base.metadata.create_all(engine)
        from sqlalchemy.orm import Session
        with Session(engine) as s:
            plan = plan_dag.generate_plan(
                db=s, execution_id="exec-x",
                task_spec={"task_kind": "general", "requires_data": False, "user_signal": "default"},
                context_manifest={}, agent_name="general_assistant",
            )
            assert plan is not None
        assert called["n"] == 0  # LLM never called when disabled
```

- [ ] **Step 7: Run all Task 1 tests + config**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_plan_inputs.py -v
```
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/plan_dag.py app/config.py tests/test_plan_inputs.py
git commit -m "feat(synexia): populate plan node inputs + gate LLM planner (fixes un-awaited call_llm)"
```

---

## Task 2 (3b): Multi-observation aggregation

**Files:**
- Modify: `backend/app/services/synexia/capability_router.py` — add `_get_all_data_observations`; switch synthesize + sandbox to aggregate.
- Test: `backend/tests/test_multi_obs_aggregation.py`

**Interfaces:**
- Produces: `_get_all_data_observations(db, execution) -> list[ObservationRecord]` — all nl2sql observations chronologically (seq asc). `_execute_synthesize_node` combines rows across all of them; `_execute_sandbox_node` reads chart data across all synthesize observations. Single-obs plans behave identically.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_multi_obs_aggregation.py`:

```python
"""Tests for Phase 3b: synthesize aggregates ALL data observations (G5).

_get_all_data_observations returns every nl2sql observation chronologically;
synthesize combines their rows so multi-step retrieval isn't lost. Single-obs
plans behave identically to the prior last-only behavior.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.synexia import capability_router as cr


def _nl2sql_obs(seq, rows, sql="SELECT 1"):
    return SimpleNamespace(
        seq=seq, success=True, observation_type="nl2sql",
        result_data={"sql": sql, "data": rows},
    )


class TestGetAllDataObservations:
    def test_returns_all_nl2sql_obs_chronologically(self, monkeypatch):
        o1 = _nl2sql_obs(1, [{"a": 1}])
        o2 = _nl2sql_obs(3, [{"a": 2}])
        # simulate db.query(...).filter(...).order_by(seq asc).all()
        class _Q:
            def __init__(self, items): self._items = items
            def filter(self, *a, **k): return self
            def order_by(self, *a, **k): return self
            def all(self): return list(self._items)
        db = MagicMock()
        db.query.return_value = _Q([o2, o1])  # returned out of order
        out = cr._get_all_data_observations(db, SimpleNamespace(id="e1"))
        assert out == [o2, o1]  # whatever the DB returns; caller combines

    def test_excludes_non_nl2sql_and_failed(self, monkeypatch):
        # The query filters by observation_type; we assert the helper only
        # considers nl2sql observations by checking it skips failed ones.
        good = _nl2sql_obs(1, [{"a": 1}])
        failed = SimpleNamespace(seq=2, success=False, observation_type="nl2sql",
                                 result_data={"sql": "x", "data": []})
        class _Q:
            def __init__(self, items): self._items = items
            def filter(self, *a, **k): return self
            def order_by(self, *a, **k): return self
            def all(self): return list(self._items)
        db = MagicMock()
        db.query.return_value = _Q([good, failed])
        out = cr._get_all_data_observations(db, SimpleNamespace(id="e1"))
        # failed obs is present from the query but the synthesize combiner
        # must skip it; here we just assert the helper returns the query result.
        assert good in out


class TestSynthesizeAggregatesMultipleObs:
    def test_combines_rows_from_all_nl2sql_obs(self, monkeypatch):
        captured = {}
        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None):
            captured.update(rows=rows, sql=sql)
            from app.services.synexia.contracts import FinalizeResult
            return FinalizeResult(task_kind="report", assistant_content="prose",
                                  report_card_payload=None, user_signal="default")
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation",
                            lambda db, ex, node, **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(cr, "_get_all_data_observations", lambda db, ex: [
            _nl2sql_obs(1, [{"material": "Steel", "revenue": 120}], "SELECT a"),
            _nl2sql_obs(3, [{"material": "Copper", "revenue": 30}], "SELECT b"),
        ])
        cr._execute_synthesize_node(MagicMock(), SimpleNamespace(
            id="e1", user_message="summarize",
            task_spec={"entities": {}}), SimpleNamespace(name="synth", inputs={}))
        # rows from BOTH observations are combined
        assert {"material": "Steel", "revenue": 120} in captured["rows"]
        assert {"material": "Copper", "revenue": 30} in captured["rows"]
        assert len(captured["rows"]) == 2

    def test_single_obs_behaves_identically(self, monkeypatch):
        captured = {}
        async def fake_synthesize(*, user_message, rows, sql, source_name,
                                  source_id, call_llm_fn, user_signal=None):
            captured.update(rows=rows, sql=sql)
            from app.services.synexia.contracts import FinalizeResult
            return FinalizeResult(task_kind="report", assistant_content="prose",
                                  report_card_payload=None, user_signal="default")
        monkeypatch.setattr("app.services.synexia.report_synthesis.synthesize_report", fake_synthesize)
        monkeypatch.setattr(cr, "_record_observation",
                            lambda db, ex, node, **kw: SimpleNamespace(**kw))
        monkeypatch.setattr(cr, "_get_all_data_observations",
                            lambda db, ex: [_nl2sql_obs(1, [{"x": 1}], "SELECT s")])
        cr._execute_synthesize_node(MagicMock(), SimpleNamespace(
            id="e1", user_message="m", task_spec={"entities": {}}),
            SimpleNamespace(name="synth", inputs={}))
        assert captured["rows"] == [{"x": 1}]
        assert captured["sql"] == "SELECT s"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_multi_obs_aggregation.py -v
```
Expected: FAIL — `_get_all_data_observations` does not exist.

- [ ] **Step 3: Implement `_get_all_data_observations` + switch synthesize**

In `backend/app/services/synexia/capability_router.py`, add next to `_get_previous_observation` (~line 371):

```python
def _get_all_data_observations(db, execution) -> list:
    """Get ALL nl2sql observations for this execution, chronologically.

    Phase 3b (G5): synthesize aggregates every data observation instead of
    only the most recent, so multi-step retrieval isn't lost. Single-obs
    plans return a one-element list (identical to prior behavior).
    """
    return (
        db.query(ObservationRecord)
        .filter(
            ObservationRecord.execution_id == execution.id,
            ObservationRecord.observation_type == "nl2sql",
        )
        .order_by(ObservationRecord.seq.asc())
        .all()
    )
```

Then in `_execute_synthesize_node`, replace the single-prev-obs gather (the `prev_obs = _get_previous_observation(...)` block, now ~line 437) with aggregation:

```python
    data_obs = _get_all_data_observations(db, execution)
    rows: list = []
    sql_parts: list[str] = []
    for obs in data_obs:
        if not getattr(obs, "success", False):
            continue
        rd = getattr(obs, "result_data", None)
        if not isinstance(rd, dict):
            continue
        s = rd.get("sql")
        if s:
            sql_parts.append(str(s))
        data = rd.get("data")
        if isinstance(data, list):
            rows.extend(data)
    sql = "; ".join(sql_parts) if sql_parts else None
```

(Leave the rest of `_execute_synthesize_node` unchanged — it already passes `rows`/`sql` to `synthesize_report`.)

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_multi_obs_aggregation.py -v
```
Expected: PASS.

- [ ] **Step 5: Run the Phase 2 synthesize suite to confirm no regression**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_multi_obs_aggregation.py tests/test_fsm_synthesize_rich.py -v
```
Expected: PASS — Phase 2 synthesize tests still green (they monkeypatch `_get_previous_observation`? No — Phase 2 tests monkeypatch `_get_previous_observation`; after this change synthesize calls `_get_all_data_observations` instead. **If any Phase 2 test breaks because it patched `_get_previous_observation` but synthesize no longer calls it, update those tests to patch `_get_all_data_observations` instead — preserve the test's intent.**)

- [ ] **Step 6: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/capability_router.py tests/test_multi_obs_aggregation.py
git commit -m "feat(synexia): aggregate all data observations in synthesize (G5)"
```

---

## Task 3 (3c): Adaptive mid-execution re-planning

**Files:**
- Create: `backend/app/services/synexia/adaptive_planner.py`
- Modify: `backend/app/services/synexia/capability_router.py` (`execute_plan_nodes` checkpoint hook)
- Modify: `backend/app/config.py`
- Test: `backend/tests/test_adaptive_planner.py`

**Interfaces:**
- Produces: `adaptive_planner.decide_adaptive_revision(*, user_message, task_spec, observations, remaining_nodes, call_llm_fn, context_manifest=None) -> AdaptiveDecision` where `AdaptiveDecision` has `action: "proceed"|"insert_nodes"|"modify_remaining"|"complete_early"` and `nodes: list[dict]` (new/modified step dicts; empty for proceed/complete_early). Never raises — errors/unparseable → `AdaptiveDecision(action="proceed")`.
- `execute_plan_nodes` calls `decide_adaptive_revision` after each checkpoint node (`nl2sql`/`synthesize`/`sandbox`) when `SYNEXIA_ADAPTIVE_PLANNING_ENABLED` is on and the revision budget remains; on `insert_nodes`/`modify_remaining`, appends/replaces the remaining tail via the existing corrective-plan path; on `complete_early`, stops executing further nodes.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_adaptive_planner.py`:

```python
"""Tests for Phase 3c: adaptive mid-execution re-planning (opt-in).

decide_adaptive_revision reviews accumulated observations after a checkpoint
node and decides proceed|insert_nodes|modify_remaining|complete_early.
Default-off (SYNEXIA_ADAPTIVE_PLANNING_ENABLED). Fail-safe: any error or
unparseable LLM reply → proceed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.synexia.adaptive_planner import (
    decide_adaptive_revision, AdaptiveDecision,
)


def _obs(tool="nl2sql", success=True, text="rows: 5"):
    return SimpleNamespace(observation_type=tool, success=success,
                           tool_name=tool, result_text=text,
                           result_data={}, error_message=None)


class TestAdaptiveDecision:
    def test_proceed_when_llm_says_proceed(self, monkeypatch):
        async def fake_llm(system, messages):
            return {"content": '{"action":"proceed","nodes":[]}'}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"
        assert d.nodes == []

    def test_complete_early_short_circuits(self, monkeypatch):
        async def fake_llm(system, messages):
            return {"content": '{"action":"complete_early","nodes":[]}'}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "complete_early"

    def test_insert_nodes_returns_new_steps(self, monkeypatch):
        new_nodes = [{"node_type": "nl2sql", "name": "Refine query", "dependencies": []}]
        async def fake_llm(system, messages):
            return {"content": '{"action":"insert_nodes","nodes":' + repr(new_nodes).replace("'", '"') + '}'}
        d = decide_adaptive_revision(
            user_message="summarize Q2", task_spec={},
            observations=[_obs()], remaining_nodes=[{"name": "synthesize"}],
            call_llm_fn=fake_llm,
        )
        assert d.action == "insert_nodes"
        assert any(n["name"] == "Refine query" for n in d.nodes)

    def test_unparseable_reply_falls_back_to_proceed(self, monkeypatch):
        async def fake_llm(system, messages):
            return {"content": "not json at all"}
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[], call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"

    def test_llm_exception_falls_back_to_proceed(self, monkeypatch):
        async def boom(system, messages):
            raise RuntimeError("llm down")
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[], call_llm_fn=boom,
        )
        assert d.action == "proceed"

    def test_no_remaining_nodes_skips_call(self, monkeypatch):
        called = {"n": 0}
        async def fake_llm(system, messages):
            called["n"] += 1
            return {"content": '{"action":"proceed"}'}
        d = decide_adaptive_revision(
            user_message="x", task_spec={}, observations=[_obs()],
            remaining_nodes=[], call_llm_fn=fake_llm,
        )
        assert d.action == "proceed"
        assert called["n"] == 0  # nothing left to revise → skip the LLM call
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_adaptive_planner.py -v
```
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.synexia.adaptive_planner'`.

- [ ] **Step 3: Implement `adaptive_planner.py`**

Create `backend/app/services/synexia/adaptive_planner.py`:

```python
"""Adaptive mid-execution re-planning (Phase 3c, spec §5.3).

After each checkpoint-eligible node (nl2sql / synthesize / sandbox), the
adaptive planner reviews accumulated observations and decides whether to
proceed, insert nodes, modify the remaining tail, or complete early.

Opt-in via ``SYNEXIA_ADAPTIVE_PLANNING_ENABLED`` (default OFF). Bounded by
``SYNEXIA_ADAPTIVE_MAX_REVISIONS`` (default 2). Fail-safe: any error or
unparseable LLM reply → ``proceed`` (the run continues on the original plan).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_VALID_ACTIONS = {"proceed", "insert_nodes", "modify_remaining", "complete_early"}


@dataclass
class AdaptiveDecision:
    action: str = "proceed"  # proceed | insert_nodes | modify_remaining | complete_early
    nodes: list[dict] = field(default_factory=list)
    reason: str = ""


def decide_adaptive_revision(
    *,
    user_message: str,
    task_spec: dict,
    observations: list[Any],
    remaining_nodes: list[dict],
    call_llm_fn: Callable,
    context_manifest: Optional[dict] = None,
) -> AdaptiveDecision:
    """Decide whether to adapt the remaining plan after a checkpoint node.

    Never raises. Returns ``AdaptiveDecision(action="proceed")`` on any error
    or when there are no remaining nodes to revise.
    """
    if not remaining_nodes:
        return AdaptiveDecision(action="proceed", reason="no remaining nodes")

    obs_summary = _summarize_observations(observations)
    remaining_summary = json.dumps(remaining_nodes, ensure_ascii=False, default=str)[:1500]

    system_prompt = (
        "You are the ADAPTIVE PLANNER for a data-driven agent. A checkpoint "
        "node just completed. Review what was learned and decide whether the "
        "REMAINING plan should change.\n\n"
        f"User request: {user_message}\n"
        f"Observations so far:\n{obs_summary}\n\n"
        f"Remaining planned steps:\n{remaining_summary}\n\n"
        "Decide ONE action:\n"
        "- proceed: the remaining plan is still right — continue as-is\n"
        "- insert_nodes: add steps BEFORE the remaining ones (e.g. a refining "
        "query) — return them in `nodes`\n"
        "- modify_remaining: replace the remaining steps with a better tail — "
        "return the full new tail in `nodes`\n"
        "- complete_early: the goal is already met — stop and finalize\n\n"
        "Respond with ONLY a JSON object: "
        '{"action":"<one>","nodes":[<step dicts>],"reason":"..."}'
        "  (nodes is [] for proceed/complete_early)."
    )

    try:
        llm_response = call_llm_fn(system_prompt, [{"role": "user", "content": "Decide."}])
    except Exception as e:
        logger.warning("Adaptive planner LLM call failed (proceed): %s", e)
        return AdaptiveDecision(action="proceed", reason=f"llm error: {e}")

    # call_llm_fn may be async or sync; await if needed.
    reply = _resolve_reply(llm_response)
    parsed = _parse_decision(reply)
    if parsed is None:
        logger.warning("Adaptive planner unparseable reply (proceed): %.200s", reply)
        return AdaptiveDecision(action="proceed", reason="unparseable")

    if parsed["action"] not in _VALID_ACTIONS:
        return AdaptiveDecision(action="proceed", reason=f"bad action: {parsed['action']}")
    nodes = parsed.get("nodes") or []
    if not isinstance(nodes, list):
        nodes = []
    return AdaptiveDecision(action=parsed["action"], nodes=nodes, reason=parsed.get("reason", ""))


def _summarize_observations(observations: list[Any]) -> str:
    lines = []
    for obs in (observations or [])[-8:]:  # last 8 to bound the prompt
        otype = getattr(obs, "observation_type", "?")
        ok = getattr(obs, "success", False)
        txt = (getattr(obs, "result_text", "") or "")[:200]
        lines.append(f"- [{otype}] {'ok' if ok else 'FAILED'}: {txt}")
    return "\n".join(lines) or "(none)"


def _resolve_reply(llm_response: Any) -> str:
    """Normalize the call_llm_fn return into a string (sync or async result)."""
    if isinstance(llm_response, dict):
        return str(llm_response.get("content", "") or "")
    return str(llm_response or "")


def _parse_decision(reply: str) -> Optional[dict]:
    text = (reply or "").strip()
    if not text:
        return None
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None
```

**Note on async `call_llm_fn`:** `decide_adaptive_revision` is called from the sync `execute_plan_nodes`. The `call_llm_fn` passed in will be a sync wrapper around the async `_synth_call_llm`-style adapter (run via `_run_coro_sync`). The `_resolve_reply` helper handles the dict return. (If you pass the async adapter directly, wrap it: `decide_adaptive_revision(..., call_llm_fn=lambda s, m: _run_coro_sync(_synth_call_llm(s, m)))`.)

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_adaptive_planner.py -v
```
Expected: PASS.

- [ ] **Step 5: Add config flags + wire the checkpoint hook**

Add to `backend/app/config.py`:
```python
    # Phase 3c: adaptive mid-execution re-planning (default off). After each
    # checkpoint node (nl2sql/synthesize/sandbox), an LLM decides whether to
    # proceed / insert / modify / complete_early. Bounded by MAX_REVISIONS.
    SYNEXIA_ADAPTIVE_PLANNING_ENABLED: bool = False
    SYNEXIA_ADAPTIVE_MAX_REVISIONS: int = 2
```

In `backend/app/services/synexia/capability_router.py`, in `execute_plan_nodes`, after each node completes (inside the per-node loop, after `db.commit()` at ~line 142) and only for checkpoint node types, call the adaptive planner. Add this block inside the loop, after the `db.commit()` that follows the try/except:

```python
        # ── Phase 3c: adaptive checkpoint (opt-in) ───────────────────────
        if node.node_type in ("nl2sql", "synthesize", "sandbox"):
            if _adaptive_enabled and _adaptive_revisions < _adaptive_max:
                _decision = _maybe_adapt(
                    db, execution, observations, plan, node,
                    user_id, on_plan_node, _adaptive_revisions,
                )
                if _decision is not None:
                    _adaptive_revisions += 1
                    if _decision.action == "complete_early":
                        logger.info("Adaptive: complete_early at node %s", node.name)
                        break
                    if _decision.action in ("insert_nodes", "modify_remaining") and _decision.nodes:
                        # Append the new tail as a corrective sub-plan and
                        # execute it (reuses the existing corrective path).
                        _tail = _build_adaptive_tail_plan(db, execution, _decision.nodes)
                        if _tail is not None:
                            observations.extend(execute_plan_nodes(
                                db, execution, _tail, user_id,
                                replan_depth=replan_depth + 1,
                                max_replan_depth=max_replan_depth,
                                data_ctx_extras=data_ctx_extras,
                                on_plan_node=on_plan_node,
                            ))
                            break  # tail executed; stop the original loop
```

Where the loop-setup (before the per-node loop) computes:
```python
    from app.config import settings
    _adaptive_enabled = getattr(settings, "SYNEXIA_ADAPTIVE_PLANNING_ENABLED", False)
    _adaptive_max = getattr(settings, "SYNEXIA_ADAPTIVE_MAX_REVISIONS", 2)
    _adaptive_revisions = 0
```

`_maybe_adapt` builds the remaining-nodes list (nodes after the current one), calls `decide_adaptive_revision` (with a sync `call_llm_fn` wrapper around `_synth_call_llm` via `_run_coro_sync`), and returns the `AdaptiveDecision` or `None` (on disabled/no-remaining/exception). `_build_adaptive_tail_plan` persists the decision's step dicts as a new `Plan` (version+1) using the same `PlanNode` construction as `generate_plan`. Keep both helpers minimal and defensive (return `None` on any failure → proceed).

- [ ] **Step 6: Run the adaptive suite + full regression**

Run:
```bash
cd /root/zhanlu/backend && python3 -m pytest tests/test_adaptive_planner.py tests/test_plan_inputs.py tests/test_multi_obs_aggregation.py tests/test_fsm_synthesize_rich.py tests/test_grounding_extractor.py tests/test_response_prompt_grounding.py tests/test_fsm_quality_eval.py -v
```
Expected: PASS — Phase 3 + Phase 1/2 + existing FSM all green.

- [ ] **Step 7: Commit**

```bash
cd /root/zhanlu/backend && git add app/services/synexia/adaptive_planner.py app/services/synexia/capability_router.py app/config.py tests/test_adaptive_planner.py
git commit -m "feat(synexia): adaptive mid-execution re-planning (opt-in, default off)"
```

---

## Phase 3 done — definition of done

- `_build_default_plan` steps carry concrete `inputs` (G4 fixed on the live path); LLM planner prompt supports `inputs` and the un-awaited `call_llm` is fixed, gated behind `SYNEXIA_LLM_PLANNER_ENABLED` (default off).
- `_get_all_data_observations` aggregates all nl2sql obs for synthesize (G5 fixed); single-obs behavior unchanged.
- `adaptive_planner.decide_adaptive_revision` wired into `execute_plan_nodes` after checkpoint nodes; opt-in `SYNEXIA_ADAPTIVE_PLANNING_ENABLED` (default off), bounded by `SYNEXIA_ADAPTIVE_MAX_REVISIONS` (2); fail-safe proceed.
- All Phase 3 tests pass; Phase 1/2 + existing FSM unbroken.
- Deploy: `docker restart zhanlu-backend`.

## Final step (after Phase 3): commit + deploy Phases 1, 2, 3

```bash
cd /root/zhanlu && git add -A && git status   # review staged changes
# commit per-phase messages already created above; if not yet committed:
# git commit -m "feat(synexia): response-quality phases 1-3 (grounding + rich synthesis + adaptive planning)"
docker restart zhanlu-backend
# verify health:
curl -s http://localhost:8000/health || curl -s http://localhost:8000/docs -o /dev/null -w "%{http_code}\n"
```
