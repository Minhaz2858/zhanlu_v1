"""Tests for the swarm fan-out pool — bounded parallel executor.

Covers (design doc ``docs/plans/2026-09-02-swarm-fanout-design.md``):
  - bounded concurrency: 12 tasks @ max_concurrent=4 → max_active == 4
  - cost cap stops new spawns once the ledger is exhausted
  - wall-time cap stops new spawns once the budget expires
  - per-task retry + escalation policy is preserved under concurrency
  - progress callback records per-worker events
  - results come back in input order; skipped tasks get synthetic results
All runners are fakes — no LLM or DB required.
"""

from __future__ import annotations

import asyncio

import pytest

from app.services.swarm.orchestrator import (
    OrchestratedTask,
    OrchestrationPolicy,
)
from app.services.swarm.runtime import SwarmAgentResult


def _tasks(n: int, agent: str = "worker") -> list[OrchestratedTask]:
    return [
        OrchestratedTask(agent_name=agent, task=f"task-{i}", member_name=f"w{i}")
        for i in range(n)
    ]


async def _ok_runner(team_id: str, agent_name: str, task: str,
                     member_name: str | None = None) -> SwarmAgentResult:
    return SwarmAgentResult(
        member_name=member_name or agent_name, agent_name=agent_name,
        task=task, final_response=f"done:{task}",
    )


async def _cost_one(task: OrchestratedTask, result, attempts: int) -> float:
    return 1.0


class _ConcurrencyRunner:
    """Fake runner that tracks the max number of concurrent in-flight runs."""

    def __init__(self, sleep: float = 0.01):
        self.sleep = sleep
        self.active = 0
        self.max_active = 0
        self.calls = 0

    async def __call__(self, team_id, agent_name, task, member_name=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.calls += 1
        try:
            if self.sleep:
                await asyncio.sleep(self.sleep)
            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, final_response=f"done:{task}",
            )
        finally:
            self.active -= 1


class TestBoundedConcurrency:
    @pytest.mark.asyncio
    async def test_12_tasks_max_concurrent_4(self):
        from app.services.swarm.pool import FanOutPool
        runner = _ConcurrencyRunner()
        pool = FanOutPool(max_concurrent=4)
        results = await pool.run("team-1", _tasks(12), runner=runner)
        assert len(results) == 12
        assert all(r.success for r in results)
        assert runner.max_active == 4
        # Input order preserved.
        assert [r.task.task for r in results] == [f"task-{i}" for i in range(12)]
        # 12 tasks / 4 concurrent ≈ 3 batches.
        assert runner.calls == 12

    @pytest.mark.asyncio
    async def test_unlimited_concurrency_runs_all(self):
        from app.services.swarm.pool import FanOutPool
        runner = _ConcurrencyRunner(sleep=0)
        pool = FanOutPool(max_concurrent=0)
        results = await pool.run("team-1", _tasks(8), runner=runner)
        assert len(results) == 8
        assert all(r.success for r in results)


class TestBudgets:
    @pytest.mark.asyncio
    async def test_cost_cap_stops_new_spawns(self):
        from app.services.swarm.pool import FanOutPool
        pool = FanOutPool(
            max_concurrent=1,
            max_total_cost_usd=2.5,
            cost_fn=_cost_one,
        )
        results = await pool.run("team-1", _tasks(12), runner=_ok_runner)
        succeeded = [r for r in results if r.success]
        skipped = [r for r in results if not r.success]
        assert len(succeeded) == 3
        assert len(skipped) == 9
        for r in skipped:
            assert r.error and "budget" in r.error

    @pytest.mark.asyncio
    async def test_wall_time_cap_stops_new_spawns(self):
        from app.services.swarm.pool import FanOutPool

        async def slow_runner(team_id, agent_name, task, member_name=None):
            await asyncio.sleep(0.05)
            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, final_response="ok",
            )

        pool = FanOutPool(max_concurrent=1, max_wall_time_s=0.01)
        results = await pool.run("team-1", _tasks(6), runner=slow_runner)
        succeeded = [r for r in results if r.success]
        skipped = [r for r in results if not r.success]
        assert len(succeeded) >= 1
        assert len(skipped) >= 1
        assert all(r.error and "wall" in r.error for r in skipped)

    @pytest.mark.asyncio
    async def test_zero_budget_means_unlimited(self):
        from app.services.swarm.pool import FanOutPool
        pool = FanOutPool(max_concurrent=4, max_total_cost_usd=0, max_wall_time_s=0)
        results = await pool.run("team-1", _tasks(10), runner=_ok_runner)
        assert len(results) == 10
        assert all(r.success for r in results)


class TestRetryAndEscalation:
    @pytest.mark.asyncio
    async def test_failing_task_retries_then_succeeds(self):
        from app.services.swarm.pool import FanOutPool
        calls = {"n": 0}

        async def flaky_runner(team_id, agent_name, task, member_name=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return SwarmAgentResult(
                    member_name=member_name or agent_name, agent_name=agent_name,
                    task=task, success=False, error="boom",
                )
            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, final_response="recovered",
            )

        pool = FanOutPool(max_concurrent=2)
        results = await pool.run(
            "team-1", _tasks(1),
            policy=OrchestrationPolicy(max_retries=1, escalate_to=None),
            runner=flaky_runner,
        )
        assert results[0].success is True
        assert results[0].attempts == 2

    @pytest.mark.asyncio
    async def test_escalates_on_final_retry(self):
        from app.services.swarm.pool import FanOutPool
        calls = {"n": 0}

        async def worker_fails_then_general_ok(team_id, agent_name, task, member_name=None):
            calls["n"] += 1
            if agent_name == "worker":
                return SwarmAgentResult(
                    member_name=member_name or agent_name, agent_name=agent_name,
                    task=task, success=False, error="always fails",
                )
            return SwarmAgentResult(
                member_name=member_name or agent_name, agent_name=agent_name,
                task=task, final_response="escalated ok",
            )

        pool = FanOutPool(max_concurrent=1)
        results = await pool.run(
            "team-1", _tasks(1, agent="worker"),
            policy=OrchestrationPolicy(max_retries=1, escalate_to="general-purpose"),
            runner=worker_fails_then_general_ok,
        )
        assert results[0].success is True
        assert results[0].escalated is True
        assert results[0].attempts == 2


class TestProgressEvents:
    @pytest.mark.asyncio
    async def test_progress_callback_records_per_worker_events(self):
        from app.services.swarm.pool import FanOutPool
        events: list[dict] = []

        async def on_progress(event: dict) -> None:
            events.append(event)

        pool = FanOutPool(max_concurrent=2, progress_callback=on_progress)
        await pool.run("team-1", _tasks(4), runner=_ok_runner)

        types = [e["type"] for e in events]
        assert types[0] == "pool_started"
        assert types[-1] == "pool_finished"
        assert types.count("task_started") == 4
        assert types.count("task_finished") == 4
        assert types.count("task_skipped") == 0
        finished = [e for e in events if e["type"] == "task_finished"]
        assert {e["member_name"] for e in finished} == {"w0", "w1", "w2", "w3"}
        assert all(e["success"] for e in finished)
        summary = events[-1]
        assert summary["succeeded"] == 4
        assert summary["total"] == 4

    @pytest.mark.asyncio
    async def test_skipped_tasks_emit_progress_event(self):
        from app.services.swarm.pool import FanOutPool
        events: list[dict] = []

        async def on_progress(event: dict) -> None:
            events.append(event)

        pool = FanOutPool(
            max_concurrent=1, max_total_cost_usd=1.5, cost_fn=_cost_one,
            progress_callback=on_progress,
        )
        await pool.run("team-1", _tasks(4), runner=_ok_runner)
        skipped = [e for e in events if e["type"] == "task_skipped"]
        assert len(skipped) == 2
        assert all(e["reason"] == "cost_budget" for e in skipped)
        summary = events[-1]
        assert summary["skipped"] == 2
