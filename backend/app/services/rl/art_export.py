"""SFT dataset export — verified trajectories in OpenAI/ART format (RL Phase 2).

Exports successful trajectories (reward ≥ threshold) as JSONL:

    {"messages": [{"role": "user", "content": ...},
                  {"role": "assistant", "content": ...}],
     "metadata": {"reward": ..., "route": ..., "agent": ...}}

Fed to ART's SFT/distillation mode (or any SFT trainer) as the warm-start
before GRPO — imitation of *verified* behavior only.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def export_sft_dataset(
    db,
    out_path: str,
    *,
    min_reward: float = 0.5,
    window_days: int = 30,
    limit: int = 2000,
) -> dict:
    """Write verified-successful trajectories to ``out_path`` as JSONL.

    Returns ``{"written": int, "path": str, "mean_reward": float}``.
    """
    from app.models.agent_invocation import AgentInvocation
    from app.models.trajectory_reward import TrajectoryReward

    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    rows = (
        db.query(AgentInvocation, TrajectoryReward)
        .join(TrajectoryReward, TrajectoryReward.invocation_id == AgentInvocation.id)
        .filter(
            AgentInvocation.status == "completed",
            AgentInvocation.created_date >= cutoff,
            TrajectoryReward.reward >= min_reward,
        )
        .order_by(TrajectoryReward.reward.desc())
        .limit(limit)
        .all()
    )

    written = 0
    rewards = []
    with open(out_path, "w", encoding="utf-8") as fh:
        for invocation, reward_row in rows:
            user_msg = (invocation.input_message or "").strip()
            assistant_msg = (invocation.assistant_content or "").strip()
            if not user_msg or not assistant_msg:
                continue
            record = {
                "messages": [
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": assistant_msg},
                ],
                "metadata": {
                    "reward": reward_row.reward,
                    "route": reward_row.route or invocation.route or "",
                    "agent": reward_row.agent_name or invocation.invocation_type,
                },
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            written += 1
            rewards.append(reward_row.reward)

    mean_reward = round(sum(rewards) / len(rewards), 3) if rewards else 0.0
    logger.info(
        "art_export: wrote %d SFT trajectories (mean reward %.3f) → %s",
        written, mean_reward, out_path,
    )
    return {"written": written, "path": out_path, "mean_reward": mean_reward}


__all__ = ["export_sft_dataset"]
