"""TrajectoryReward — per-turn scalar reward for RL (ADLC/RL Phase 0).

One row per agent_invocation: the formalized reward signal that Phases 1-3
consume (bandit policy updates, SFT dataset filtering, GRPO reward
functions). Components are kept alongside the scalar so reward attribution
stays auditable (which signal moved the number).
"""

from sqlalchemy import Float, Integer, JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import TimestampedBase


class TrajectoryReward(TimestampedBase):
    __tablename__ = "trajectory_rewards"

    invocation_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    route: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reward: Mapped[float] = mapped_column(Float, nullable=False)
    components: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # "auto" (computed by the reward function) | "human" (HITL override)
    source: Mapped[str] = mapped_column(String(20), default="auto", nullable=False)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "invocation_id": self.invocation_id,
            "agent_name": self.agent_name,
            "route": self.route,
            "reward": self.reward,
            "components": self.components or {},
            "source": self.source,
        }
