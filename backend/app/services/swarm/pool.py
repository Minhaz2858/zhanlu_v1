"""Bounded fan-out pool — run :class:`OrchestratedTask` items concurrently.

The swarm tool dispatches N subtasks; :class:`FanOutPool` bounds how many
run at once (asyncio semaphore) and enforces two global budgets:

- ``max_total_cost_usd`` — a cost ledger, charged per completed task via an
  injectable async ``cost_fn`` (default: delta of the token-tracker total
  cost for the conversation).
- ``max_wall_time_s`` — wall-clock budget for the whole run.

Budgets gate **new spawns only**: in-flight tasks always complete, and
tasks that never started get a synthetic :class:`OrchestratedResult`
(``success=False, error="skipped: <budget>"``) so the lead's aggregated
summary still accounts for them.

Each task runs through the same retry/escalation policy as the sequential
orchestrator (:func:`app.services.swarm.orchestrator.run_task_with_retries`)
— no duplicated policy logic. An async ``progress_callback`` receives
per-worker lifecycle events (the seam for a future live SSE stream; today
the ``swarm_orchestrate`` tool returns the collected timeline in its
result and posts the aggregated summary to the lead's mailbox).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional

from app.services.swarm.orchestrator import (
    OrchestratedResult,
    OrchestratedTask,
    OrchestrationPolicy,
    run_task_with_retries,
)

logger = logging.getLogger(__name__)

# Runner signature matches SwarmCoordinator.run_task /
# SwarmOrchestrator's Runner alias.
Runner = Callable[..., Awaitable]
CostFn = Callable[[OrchestratedTask, OrchestratedResult, int], Awaitable[float]]
ProgressCallback = Callable[[dict], Awaitable[None]]


@dataclass
class PoolBudget:
    """Global budgets for one pool run (0/None = unlimited)."""

    max_concurrent: int = 8
    max_total_cost_usd: float = 0.0
    max_wall_time_s: float = 0.0


def _make_delta_cost_fn(conversation_id: str) -> CostFn:
    """Default cost_fn — charge the token-tracker cost delta per task.

    Reads ``token_tracker.get_total_usage(conversation_id)`` before/after
    each task and returns the difference. Falls back to 0.0 on any error
    so cost accounting never breaks a run.
    """
    last = {"total": 0.0}

    async def _delta(task: OrchestratedTask, result: OrchestratedResult,
                     attempts: int) -> float:
        try:
            from app.services.token_tracker import get_token_tracker
            usage = get_token_tracker().get_total_usage(conversation_id)
            total = float(usage.get("total_cost_usd", 0.0) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm pool: token-tracker cost read failed: %s", exc)
            total = last["total"]
        delta = max(0.0, total - last["total"])
        last["total"] = total
        return delta

    return _delta


class FanOutPool:
    """Bounded fan-out executor over :class:`OrchestratedTask` items."""

    def __init__(
        self,
        *,
        max_concurrent: int = 8,
        max_total_cost_usd: float = 0.0,
        max_wall_time_s: float = 0.0,
        cost_fn: Optional[CostFn] = None,
        progress_callback: Optional[ProgressCallback] = None,
        conversation_id: Optional[str] = None,
    ) -> None:
        self.max_concurrent = int(max_concurrent or 0)
        self.max_total_cost_usd = float(max_total_cost_usd or 0.0)
        self.max_wall_time_s = float(max_wall_time_s or 0.0)
        self._cost_fn = cost_fn
        if cost_fn is None and conversation_id:
            self._cost_fn = _make_delta_cost_fn(conversation_id)
        self._progress_callback = progress_callback

    # ── Progress events ────────────────────────────────────────────────

    async def _emit(self, event: dict) -> None:
        if self._progress_callback is None:
            return
        try:
            await self._progress_callback(event)
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm pool progress callback failed: %s", exc)

    # ── Budgets ────────────────────────────────────────────────────────

    def _budget_reason(self, start: float, spent: float) -> Optional[str]:
        """Return the reason new spawns are blocked, or None if allowed."""
        if self.max_wall_time_s > 0 and (time.monotonic() - start) >= self.max_wall_time_s:
            return "wall_time_budget"
        if self.max_total_cost_usd > 0 and spent >= self.max_total_cost_usd:
            return "cost_budget"
        return None

    async def _charge(self, task: OrchestratedTask, result: OrchestratedResult,
                      attempts: int) -> float:
        if self._cost_fn is None:
            return 0.0
        try:
            return float(await self._cost_fn(task, result, attempts) or 0.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("swarm pool cost_fn failed: %s", exc)
            return 0.0

    # ── Run ────────────────────────────────────────────────────────────

    async def run(
        self,
        team_id: str,
        tasks: List[OrchestratedTask],
        *,
        policy: Optional[OrchestrationPolicy] = None,
        runner: Optional[Runner] = None,
    ) -> List[OrchestratedResult]:
        """Run tasks concurrently, bounded by concurrency + budget gates.

        Args:
            team_id: Team to run within (passed through to the runner).
            tasks: Subtasks to execute (order preserved in the result).
            policy: Retry/escalation policy (default: 1 retry, escalate to
                "general-purpose").
            runner: Async runner ``(team_id, agent_name, task, member_name)
                -> SwarmAgentResult``. Required — the pool has no
                coordinator of its own.

        Returns:
            One :class:`OrchestratedResult` per input task, in input order.
            Tasks skipped by a budget get a synthetic failed result.
        """
        tasks = list(tasks)
        policy = policy or OrchestrationPolicy()
        if runner is None:
            raise ValueError("FanOutPool.run requires a runner callable")
        n = len(tasks)
        start = time.monotonic()
        spent = 0.0
        results_by_index: dict[int, OrchestratedResult] = {}
        skipped: dict[int, str] = {}
        pending = asyncio.Queue()
        for i in range(n):
            pending.put_nowait(i)
        sem: Optional[asyncio.Semaphore] = None
        if self.max_concurrent > 0:
            sem = asyncio.Semaphore(self.max_concurrent)

        async def _worker() -> None:
            nonlocal spent
            while True:
                try:
                    idx = pending.get_nowait()
                except asyncio.QueueEmpty:
                    return
                task = tasks[idx]
                reason = self._budget_reason(start, spent)
                if reason:
                    skipped[idx] = reason
                    await self._emit({
                        "type": "task_skipped",
                        "index": idx,
                        "agent_name": task.agent_name,
                        "member_name": task.member_name,
                        "reason": reason,
                    })
                    continue
                await self._emit({
                    "type": "task_started",
                    "index": idx,
                    "agent_name": task.agent_name,
                    "member_name": task.member_name,
                })
                result = await run_task_with_retries(team_id, task, policy, runner)
                cost = await self._charge(task, result, result.attempts)
                spent += cost
                results_by_index[idx] = result
                await self._emit({
                    "type": "task_finished",
                    "index": idx,
                    "agent_name": task.agent_name,
                    "member_name": task.member_name,
                    "success": result.success,
                    "attempts": result.attempts,
                    "escalated": result.escalated,
                    "cost_usd": cost,
                })

        await self._emit({
            "type": "pool_started",
            "task_count": n,
            "max_concurrent": self.max_concurrent or n,
            "max_total_cost_usd": self.max_total_cost_usd,
            "max_wall_time_s": self.max_wall_time_s,
        })
        worker_count = self.max_concurrent if self.max_concurrent > 0 else n
        await asyncio.gather(*(_worker() for _ in range(max(1, worker_count))))

        ordered: List[OrchestratedResult] = []
        for idx in range(n):
            if idx in results_by_index:
                ordered.append(results_by_index[idx])
            else:
                reason = skipped.get(idx, "budget_exhausted")
                task = tasks[idx]
                ordered.append(OrchestratedResult(
                    task=task,
                    success=False,
                    final_response="",
                    attempts=0,
                    escalated=False,
                    error=f"skipped: {reason}",
                ))

        await self._emit({
            "type": "pool_finished",
            "succeeded": sum(1 for r in ordered if r.success),
            "total": n,
            "skipped": n - len(results_by_index),
            "wall_time_ms": round((time.monotonic() - start) * 1000, 1),
        })
        return ordered


__all__ = ["FanOutPool", "PoolBudget", "Runner", "CostFn", "ProgressCallback"]
