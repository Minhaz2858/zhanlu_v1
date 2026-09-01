"""Phase 5 — dispatcher bounded concurrency + FSM per-node callback tests.

  * Tier B #5 — ``_run_executor`` is bounded by an ``asyncio.Semaphore``
    sized from ``AUTOMATION_MAX_CONCURRENCY``, so a burst of due tasks
    can't exhaust RAM / rate-limit the provider.
  * Tier A #3 — ``execute_plan_nodes`` invokes the ``on_plan_node``
    callback at each node lifecycle transition (running/completed/...) so
    the automation activity feed shows the plan executing step-by-step.
"""
import asyncio
import os
import sys
from unittest.mock import patch, MagicMock

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services import automation_dispatcher as disp


# ---------------------------------------------------------------------------
# Tier B #5 — bounded concurrency
# ---------------------------------------------------------------------------

async def test_run_executor_serializes_when_cap_is_one():
    """With a 1-slot semaphore, two concurrent _run_executor calls never
    overlap — the second starts only after the first releases."""
    disp._concurrency_sem = asyncio.Semaphore(1)
    try:
        active = [0]
        peak = [0]
        done_order = []

        def fake_execute(eid):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            # Simulate work so the second call would overlap if unbounded.
            import time
            time.sleep(0.05)
            active[0] -= 1
            done_order.append(eid)

        with patch("app.services.automation_executor.execute_automation", side_effect=fake_execute):
            await asyncio.gather(
                disp._run_executor("a"),
                disp._run_executor("b"),
            )
        # Peak concurrency must be 1 (serialized), never 2.
        assert peak[0] == 1, f"expected serialization, peak={peak[0]}"
        assert len(done_order) == 2
    finally:
        disp._concurrency_sem = None


async def test_run_executor_allows_overlap_when_cap_is_two():
    """With a 2-slot semaphore, two concurrent calls can overlap."""
    disp._concurrency_sem = asyncio.Semaphore(2)
    try:
        active = [0]
        peak = [0]

        def fake_execute(eid):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            import time
            time.sleep(0.05)
            active[0] -= 1

        with patch("app.services.automation_executor.execute_automation", side_effect=fake_execute):
            await asyncio.gather(
                disp._run_executor("a"),
                disp._run_executor("b"),
            )
        assert peak[0] == 2, f"expected overlap with cap=2, peak={peak[0]}"
    finally:
        disp._concurrency_sem = None


async def test_run_executor_runs_unbounded_when_no_semaphore():
    """If the semaphore isn't initialized (e.g. trigger before start), the
    task still runs (unbounded) rather than being dropped."""
    disp._concurrency_sem = None
    called = [False]

    def fake_execute(eid):
        called[0] = True

    with patch("app.services.automation_executor.execute_automation", side_effect=fake_execute):
        await disp._run_executor("a")
    assert called[0] is True


# ---------------------------------------------------------------------------
# Tier A #3 — FSM per-node activity callback
# ---------------------------------------------------------------------------

def test_execute_plan_nodes_invokes_on_plan_node_per_node():
    """execute_plan_nodes calls on_plan_node with running→completed (or
    failed) for each executed node, so the SSE path can emit activity steps."""
    from app.services.synexia.capability_router import execute_plan_nodes
    from app.models.execution import ObservationRecord

    # Build a minimal plan with 2 nodes.
    n1 = MagicMock(); n1.id = "n1"; n1.name = "search_web"; n1.node_type = "tool"
    n1.seq = 1; n1.inputs = {}; n1.status = "pending"; n1.error = None
    n1.started_at = None; n1.completed_at = None; n1.result = None
    n1.dependencies = []
    n2 = MagicMock(); n2.id = "n2"; n2.name = "summarize"; n2.node_type = "skill"
    n2.seq = 2; n2.inputs = {}; n2.status = "pending"; n2.error = None
    n2.started_at = None; n2.completed_at = None; n2.result = None
    n2.dependencies = []

    plan = MagicMock(); plan.nodes = [n1, n2]

    execution = MagicMock(); execution.id = "e1"
    execution.policy_decision = {}
    execution.task_spec = {}
    execution.context_manifest = {}
    execution.agent_name = "R"

    db = MagicMock()

    # Stub _execute_single_node to return a successful observation.
    def fake_single(db_, execution_, node, user_id, data_ctx_extras=None):
        return ObservationRecord(
            id="o", execution_id="e1", plan_node_id=node.id, seq=0,
            observation_type="tool_call", tool_name=node.name,
            success=True, result_data={"ok": True}, result_text="ok",
        )

    events = []

    def on_node(node_dict, status, detail=None):
        events.append((node_dict.get("name"), status))

    with patch("app.services.synexia.capability_router._execute_single_node", side_effect=fake_single), \
         patch("app.services.synexia.capability_router._topological_sort", return_value=[n1, n2]), \
         patch("app.services.synexia.capability_router._dependencies_met", return_value=True), \
         patch("app.services.synexia.capability_router.evaluate_node", return_value={"decision": "allow", "reason": ""}), \
         patch("app.services.synexia.capability_router._recoverable_failures", return_value=[]):
        execute_plan_nodes(db, execution, plan, user_id=None, on_plan_node=on_node)

    # Each node emits running then completed.
    names_statuses = events
    assert ("search_web", "running") in names_statuses
    assert ("search_web", "completed") in names_statuses
    assert ("summarize", "running") in names_statuses
    assert ("summarize", "completed") in names_statuses


def test_on_plan_node_callback_exceptions_are_swallowed():
    """A misbehaving callback must never break execution."""
    from app.services.synexia.capability_router import execute_plan_nodes
    from app.models.execution import ObservationRecord

    n1 = MagicMock(); n1.id = "n1"; n1.name = "t"; n1.node_type = "tool"
    n1.seq = 1; n1.inputs = {}; n1.status = "pending"; n1.error = None
    n1.started_at = None; n1.completed_at = None; n1.result = None
    n1.dependencies = []
    plan = MagicMock(); plan.nodes = [n1]
    execution = MagicMock(); execution.id = "e1"
    execution.policy_decision = {}; execution.task_spec = {}
    execution.context_manifest = {}; execution.agent_name = "R"
    db = MagicMock()

    def fake_single(db_, execution_, node, user_id, data_ctx_extras=None):
        return ObservationRecord(
            id="o", execution_id="e1", plan_node_id=node.id, seq=0,
            observation_type="tool_call", tool_name=node.name, success=True,
        )

    def bad_callback(node_dict, status, detail=None):
        raise RuntimeError("callback exploded")

    with patch("app.services.synexia.capability_router._execute_single_node", side_effect=fake_single), \
         patch("app.services.synexia.capability_router._topological_sort", return_value=[n1]), \
         patch("app.services.synexia.capability_router._dependencies_met", return_value=True), \
         patch("app.services.synexia.capability_router.evaluate_node", return_value={"decision": "allow", "reason": ""}), \
         patch("app.services.synexia.capability_router._recoverable_failures", return_value=[]):
        # Must not raise despite the bad callback.
        obs = execute_plan_nodes(db, execution, plan, user_id=None, on_plan_node=bad_callback)
    assert len(obs) == 1
