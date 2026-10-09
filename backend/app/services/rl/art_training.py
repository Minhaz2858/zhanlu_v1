"""ART (Agent Reinforcement Trainer) integration — GRPO phase (RL Phase 3).

Follows https://github.com/openpipe/art — the OpenPipe Agent Reinforcement
Trainer: GRPO + LoRA on open-weight models (Qwen 2.5/3.x), served in vLLM.

Architecture:
  - ``build_rollout_spec`` turns golden test cases into rollout tasks.
  - ``reward_function`` is the DETERMINISTIC half of the Phase-0 reward
    (outcome + grounding gates); the LLM judge (RULER-style) supplies the
    quality component at rollout time.
  - ``train_with_art`` runs the actual GRPO loop. It lazy-imports
    ``openpipe_art`` (NOT in requirements.txt — it pulls vLLM/Unsloth/TRL;
    install it only on the GPU training machine) and requires
    ``RL_ART_SERVER_URL`` (the ART server running vLLM) plus
    ``RL_ART_BASE_MODEL`` (e.g. Qwen/Qwen3-8B).

The loop mirrors ART's documented pattern:
    model = art.TrainableModel(name, base_model); await model.init_client(...)
    group = art.TrajectoryGroup([...]); await model.add(group)
    await model.train()

This module is deliberately import-safe on machines WITHOUT a GPU: every
ART import is lazy, and the deterministic reward function is independently
testable.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

COMPLETE_MARKER = "[COMPLETE]"


@dataclass
class ARTConfig:
    """Training config — all values from env (RL_ART_*)."""

    server_url: str = ""
    base_model: str = ""
    model_name: str = "zhanlu-agent"
    rollouts_per_task: int = 4
    max_steps: int = 20
    train_backend: str = ""  # e.g. wandb serverless or skypilot

    @classmethod
    def from_env(cls) -> "ARTConfig":
        from app.config import settings

        return cls(
            server_url=getattr(settings, "RL_ART_SERVER_URL", "") or "",
            base_model=getattr(settings, "RL_ART_BASE_MODEL", "") or "",
            model_name=getattr(settings, "RL_ART_MODEL_NAME", "zhanlu-agent"),
            rollouts_per_task=int(getattr(settings, "RL_ART_ROLLOUTS_PER_TASK", 4)),
            max_steps=int(getattr(settings, "RL_ART_MAX_STEPS", 20)),
            train_backend=getattr(settings, "RL_ART_BACKEND", "") or "",
        )

    def validate(self) -> list[str]:
        missing = []
        if not self.server_url:
            missing.append("RL_ART_SERVER_URL")
        if not self.base_model:
            missing.append("RL_ART_BASE_MODEL")
        return missing


@dataclass
class RolloutTask:
    """One golden task = one TrajectoryGroup source."""

    task_id: str
    prompt: str
    expected_criteria: list[str] = field(default_factory=list)


def build_rollout_spec(db, max_tasks: int = 25) -> list[RolloutTask]:
    """Golden test cases → rollout tasks (the training environment)."""
    try:
        from app.models.agent_test_case import AgentTestCase

        rows = (
            db.query(AgentTestCase)
            .filter(AgentTestCase.is_deleted == False)  # noqa: E712
            .limit(max_tasks)
            .all()
        )
        spec = []
        for r in rows:
            criteria = []
            if getattr(r, "expected_contains", None):
                criteria = r.expected_contains
            elif getattr(r, "expected_output", None):
                criteria = [str(r.expected_output)[:200]]
            spec.append(RolloutTask(
                task_id=r.id,
                prompt=r.input_message if hasattr(r, "input_message") else r.question,
                expected_criteria=criteria,
            ))
        return [s for s in spec if s.prompt]
    except Exception as e:  # noqa: BLE001
        logger.warning("rollout spec build failed: %s", e)
        return []


def reward_function(
    *,
    task: RolloutTask,
    final_response: str,
    tool_calls: list[dict] | None = None,
    judge_score: float | None = None,
) -> tuple[float, dict[str, Any]]:
    """Deterministic reward for one rollout (outcome + process components).

    - criteria coverage: fraction of expected_criteria satisfied
    - grounding: no ungrounded-report markers (deterministic detectors)
    - efficiency: mild penalty for excessive tool calls
    - judge: RULER-style LLM score when supplied
    """
    components: dict[str, Any] = {}
    response = (final_response or "").strip()

    criteria = task.expected_criteria or []
    if criteria:
        hit = sum(1 for c in criteria if str(c).lower()[:80] in response.lower())
        coverage = hit / len(criteria)
    else:
        coverage = 1.0 if response else 0.0
    components["criteria_coverage"] = round(coverage, 3)

    grounding = 0.0
    ungrounded_markers = (
        "i could not retrieve", "no data was retrieved",
        "i had trouble", "fabricated",
    )
    if any(m in response.lower() for m in ungrounded_markers):
        grounding = -0.5
    components["grounding"] = grounding

    efficiency = 0.0
    n_calls = len(tool_calls or [])
    if n_calls > 12:
        efficiency = -0.2 * (n_calls - 12) / 12
    components["efficiency"] = round(efficiency, 3)

    judge = 0.0
    if isinstance(judge_score, (int, float)):
        judge = 0.5 * float(judge_score)
        components["judge"] = round(judge, 3)

    reward = coverage + grounding + efficiency + judge
    components["total"] = round(reward, 3)
    return round(reward, 3), components


async def train_with_art(
    config: ARTConfig,
    db,
    *,
    max_tasks: int = 25,
) -> dict:
    """Run the GRPO loop over golden tasks (requires the GPU server).

    Lazy-imports ``openpipe_art``; raises a clear RuntimeError when the
    package/server is unavailable so ops get an actionable message.
    """
    missing = config.validate()
    if missing:
        raise RuntimeError(
            f"ART training not configured — missing env: {', '.join(missing)}. "
            "Set RL_ART_SERVER_URL (ART server running vLLM) and "
            "RL_ART_BASE_MODEL (e.g. Qwen/Qwen3-8B)."
        )
    try:
        import art  # type: ignore  # pip install openpipe-art (GPU machine only)
    except ImportError as e:
        raise RuntimeError(
            "openpipe-art is not installed on this machine. ART training runs "
            "on the GPU server: pip install openpipe-art (server side), or use "
            "the serverless backend (RL_ART_BACKEND)."
        ) from e

    tasks = build_rollout_spec(db, max_tasks=max_tasks)
    if not tasks:
        return {"started": False, "reason": "no golden tasks available"}

    model = art.TrainableModel(
        name=config.model_name,
        base_model=config.base_model,
    )
    await model.init_client(
        backend_config=config.train_backend or None,
        base_url=config.server_url,
    )

    for step in range(1, config.max_steps + 1):
        groups = []
        for task in tasks:
            group = art.TrajectoryGroup()
            for _ in range(config.rollouts_per_task):
                # The rollout executes the governed agent loop against the
                # ART-served model; messages + tool calls accumulate into the
                # trajectory.
                trajectory = await _run_rollout(config, db, task)
                group.add(trajectory)
            groups.append(group)
        await model.train(groups)
        logger.info("ART: completed GRPO step %d/%d", step, config.max_steps)

    return {
        "started": True,
        "steps": config.max_steps,
        "tasks_per_step": len(tasks),
        "rollouts_per_task": config.rollouts_per_task,
    }


def _art_endpoint(config: ARTConfig):
    """LLMEndpoint pointing at the ART server's OpenAI-compatible vLLM."""
    from app.services.llm_router import LLMEndpoint

    return LLMEndpoint(
        base_url=config.server_url,
        api_key="art-training",
        model_id=config.base_model,
    )


async def _run_rollout(config: ARTConfig, db, task: RolloutTask):
    """One rollout: the governed agent loop against the ART-served model.

    The ART server exposes an OpenAI-compatible vLLM endpoint, so the
    rollout pins it as the candidate LLMEndpoint and drives the SAME
    governed sub-agent loop as production. The judge supplies the quality
    component of the reward.
    """
    import asyncio

    import art  # type: ignore

    trajectory = art.Trajectory(
        messages_and_choices=[{"role": "user", "content": task.prompt}],
        metadata={"task_id": task.task_id},
    )
    try:
        from app.config import settings
        from app.services.tool_handlers.delegate_tool import _run_sub_agent_inner

        endpoint = _art_endpoint(config)
        result = await asyncio.wait_for(
            _run_sub_agent_inner(
                task=task.prompt,
                agent_name="general_assistant",
                db=db,
                user_id="art-rollout",
                max_iterations=config.max_steps,
                endpoint=endpoint,
            ),
            timeout=float(getattr(settings, "EVAL_GATE_CASE_TIMEOUT_S", 120)),
        )
        final_response = result.get("response") or result.get("error") or ""
        trajectory.messages_and_choices.append(
            {"role": "assistant", "content": final_response})
        judge_score = 0.0
        try:
            from app.services.synexia.quality_eval import evaluate_quality

            judge = evaluate_quality(
                user_message=task.prompt,
                assistant_text=final_response,
                task_spec={"acceptance_criteria": list(task.expected_criteria)},
            )
            judge_score = float(getattr(judge, "completeness_score", 0.0) or 0.0)
        except Exception:  # noqa: BLE001 — judge failure = neutral quality
            judge_score = 0.0
        reward, components = reward_function(
            task=task, final_response=final_response, judge_score=judge_score,
        )
        trajectory.reward = reward
        trajectory.metadata["components"] = components
    except Exception as e:  # noqa: BLE001 — failed rollout = negative reward
        logger.warning("ART rollout failed for %s: %s", task.task_id, e)
        trajectory.reward = -1.0
        trajectory.messages_and_choices.append(
            {"role": "assistant", "content": f"(rollout error: {e})"})
    return trajectory


__all__ = [
    "ARTConfig",
    "RolloutTask",
    "build_rollout_spec",
    "reward_function",
    "train_with_art",
]
