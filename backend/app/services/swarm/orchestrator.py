"""Swarm orchestrator — result-driven re-dispatch for parallel agents.

Closes the Phase-5 autonomy gap: ``SwarmCoordinator._run_agent`` posted
results to the mailbox but *nothing consumed them to decide next steps*.
Spawning was fire-and-forget, so a failed worker was never retried or
escalated.

:class:`SwarmOrchestrator` runs a list of subtasks through awaitable
:meth:`SwarmCoordinator.run_task` calls and, on failure, **re-dispatches**
the task with the failure context appended (and optionally **escalates** to
a stronger agent on the final retry). It then posts an aggregated summary
to the team lead's mailbox.

The runner is pluggable (``runner`` arg) so the re-dispatch/escalation
policy is unit-testable without a database or LLM — pass a fake runner that
fails N times then succeeds.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Runner signature: (team_id, agent_name, task, member_name) -> SwarmAgentResult.
# Matches SwarmCoordinator.run_task. Kept as a loose type alias for clarity.
Runner = Callable[..., Awaitable]


@dataclass
class OrchestratedTask:
    """One subtask for the orchestrator to run (with re-dispatch)."""

    agent_name: str
    task: str
    priority: int = 0
    member_name: Optional[str] = None


@dataclass
class OrchestrationPolicy:
    """Re-dispatch + escalation policy.

    Attributes:
        max_retries: Number of re-dispatch attempts after the initial run
            (0 = single attempt, no retry; 1 = one retry → 2 total attempts).
        escalate_to: Agent definition to switch to on the final retry when
            the original agent keeps failing (e.g. escalate "worker" →
            "general-purpose"). None = never escalate.
    """

    max_retries: int = 1
    escalate_to: Optional[str] = "general-purpose"

    def retry_hint(self, task: str, error: str, prior_response: str) -> str:
        """Augment the task prompt with the prior failure context."""
        return (
            f"{task}\n\n"
            f"A previous attempt failed ({error}). "
            f"Prior output: {prior_response[:200]}. "
            f"Try a different approach and avoid repeating the mistake."
        )


@dataclass
class OrchestratedResult:
    task: OrchestratedTask
    success: bool
    final_response: str
    attempts: int
    escalated: bool
    error: Optional[str] = None


class SwarmOrchestrator:
    """Run subtasks with result-driven re-dispatch and escalation."""

    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def orchestrate(
        self,
        team_id: str,
        tasks: list[OrchestratedTask],
        policy: Optional[OrchestrationPolicy] = None,
        runner: Optional[Runner] = None,
    ) -> list[OrchestratedResult]:
        """Run each task, re-dispatching on failure, escalating on the last retry.

        Args:
            team_id: The team to run within.
            tasks: Subtasks to execute (sequentially; each gets its own
                re-dispatch budget).
            policy: Re-dispatch/escalation policy (default: 1 retry,
                escalate to "general-purpose").
            runner: Optional async runner ``(team_id, agent_name, task,
                member_name) -> SwarmAgentResult``. Defaults to
                ``coordinator.run_task``. Override for unit tests.

        Returns:
            One :class:`OrchestratedResult` per task, in input order.
        """
        policy = policy or OrchestrationPolicy()
        runner = runner or self.coordinator.run_task
        results: list[OrchestratedResult] = []

        for t in tasks:
            agent = t.agent_name
            prompt = t.task
            attempts = 0
            escalated = False
            result = None
            while True:
                try:
                    result = await runner(team_id, agent, prompt, t.member_name)
                except Exception as e:  # noqa: BLE001
                    from app.services.swarm.runtime import SwarmAgentResult
                    result = SwarmAgentResult(
                        member_name=t.member_name or agent, agent_name=agent,
                        task=prompt, success=False, error=str(e),
                    )
                attempts += 1
                if result.success or attempts > policy.max_retries:
                    break
                # Failure with retries remaining — prepare a re-dispatch.
                if (
                    attempts >= policy.max_retries
                    and policy.escalate_to
                    and agent != policy.escalate_to
                ):
                    # Final retry: escalate to a stronger agent.
                    agent = policy.escalate_to
                    escalated = True
                prompt = policy.retry_hint(
                    t.task, result.error or "failed", result.final_response or "",
                )

            results.append(OrchestratedResult(
                task=t,
                success=bool(result and result.success),
                final_response=(result.final_response if result else ""),
                attempts=attempts,
                escalated=escalated,
                error=(result.error if result and not result.success else None),
            ))

        self._post_summary(team_id, results)
        return results

    def _post_summary(self, team_id: str, results: list[OrchestratedResult]) -> None:
        """Post an aggregated outcome summary to the team lead's mailbox.

        Uses high priority so the orchestrator's final verdict is dequeued
        ahead of ordinary worker chatter.
        """
        team = self.coordinator.registry.get_team(team_id)
        if not team:
            return
        # Ensure a "main"/lead recipient exists. send_message resolves "main"
        # to the first lead member; if there's no lead, the message is dropped
        # (reported as success) — acceptable for a best-effort summary.
        sender = "orchestrator"
        if sender not in team.members:
            self.coordinator.registry.add_member(team_id, sender, role="lead")
        succeeded = sum(1 for r in results if r.success)
        lines = [f"Orchestration complete: {succeeded}/{len(results)} tasks succeeded."]
        for r in results:
            status = "OK" if r.success else "FAIL"
            esc = " (escalated)" if r.escalated else ""
            lines.append(
                f"  [{status}] {r.task.task[:80]} — attempts={r.attempts}{esc}"
                + (f" err={r.error}" if r.error else "")
            )
        body = "\n".join(lines)
        self.coordinator.registry.send_message(
            team_id, sender, "main", body,
            summary=f"Orchestration: {succeeded}/{len(results)} succeeded",
            priority=10,
        )
