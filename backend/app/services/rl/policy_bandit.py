"""Policy bandit — ε-greedy optimization over loop-level decisions (RL Phase 1).

The Medium RL article's "RL as a runtime control system" applied to the
decisions Zhanlu's loop actually makes every turn:

- **arm** = one discrete policy choice (e.g. route=fsm vs route=react;
  model_id per task_type; delegate-vs-loop per delegation family)
- **pull** = a real invocation on that arm
- **reward** = the trajectory reward from services/rl/rewards.py

Algorithm: ε-greedy with exponential-decay means (recent turns matter more
than ancient ones) and a persistence bootstrap from ``trajectory_rewards``
joined ``agent_invocations``. Exploration is capped by epsilon; everything
is advisory — callers keep their deterministic fallback.

NOT (yet) full RL: no policy gradient, no environment model. This is the
bandit layer of the article's runtime control loop; GRPO/ART (Phase 3)
optimizes the *policy inside* the model, this optimizes the *decisions
around* it.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_DECAY = 0.98          # exponential-decay factor per update
_DEFAULT_EPSILON = 0.1
_MIN_PULLS_FOR_EXPLOIT = 5  # fewer pulls than this → treat as unexplored


class EpsilonGreedyBandit:
    """Thread-safe ε-greedy bandit with decaying means per arm."""

    def __init__(self, name: str, epsilon: float = _DEFAULT_EPSILON) -> None:
        self.name = name
        self.epsilon = epsilon
        self._values: dict[str, float] = {}
        self._counts: dict[str, int] = {}
        self._last_pull_ts: dict[str, float] = {}
        self._lock = threading.Lock()

    def select(self, arms: list[str]) -> str:
        """Pick an arm: ε explore (random), else the highest decaying mean."""
        import random

        if not arms:
            raise ValueError("no arms to select from")
        with self._lock:
            unexplored = [a for a in arms if self._counts.get(a, 0) < _MIN_PULLS_FOR_EXPLOIT]
            if unexplored and random.random() >= self.epsilon:
                # prefer exploration of under-sampled arms even in exploit mode
                return random.choice(unexplored)
            if random.random() < self.epsilon:
                return random.choice(arms)
            best, best_v = None, -math.inf
            for arm in arms:
                v = self._values.get(arm, 0.0)
                if v > best_v:
                    best, best_v = arm, v
            return best or arms[0]

    def update(self, arm: str, reward: float) -> float:
        """Incremental mean with exponential decay. Returns the new mean."""
        with self._lock:
            count = self._counts.get(arm, 0)
            old = self._values.get(arm, 0.0)
            if count == 0:
                new = reward
            else:
                # decayed mean: weight the new sample as 1/(1+decay^count)….
                # simpler and stable: alpha = 1/min(count+1, 20)
                alpha = 1.0 / min(count + 1, 20)
                new = old + alpha * (reward - old)
            self._values[arm] = new
            self._counts[arm] = count + 1
            self._last_pull_ts[arm] = time.monotonic()
            return new

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "epsilon": self.epsilon,
                "arms": {
                    arm: {
                        "mean": round(self._values.get(arm, 0.0), 4),
                        "count": self._counts.get(arm, 0),
                    }
                    for arm in sorted(
                        set(self._values) | set(self._counts),
                        key=lambda a: -self._values.get(a, 0.0),
                    )
                },
            }

    def best_arm(self, min_pulls: int = _MIN_PULLS_FOR_EXPLOIT) -> str | None:
        with self._lock:
            best, best_v = None, -math.inf
            for arm, count in self._counts.items():
                if count < min_pulls:
                    continue
                v = self._values.get(arm, 0.0)
                if v > best_v:
                    best, best_v = arm, v
            return best


# ── Singleton bandits ───────────────────────────────────────────────────

_route_bandit: EpsilonGreedyBandit | None = None
_bandit_lock = threading.Lock()


def get_route_bandit() -> EpsilonGreedyBandit:
    """Bandit over execution routes (react / fsm / workflow)."""
    global _route_bandit
    with _bandit_lock:
        if _route_bandit is None:
            _route_bandit = EpsilonGreedyBandit("route")
        return _route_bandit


def record_reward(route: str, reward: float) -> None:
    """Record one trajectory reward into the route bandit (called by the
    reward writer when the invocation has a route)."""
    if route:
        try:
            get_route_bandit().update(route, reward)
        except Exception as e:  # noqa: BLE001
            logger.debug("bandit update failed: %s", e)


def bootstrap_from_rewards(db, window_hours: int = 168) -> int:
    """Seed the route bandit from recent trajectory rewards (chronological
    replay, so the decaying means reflect recency). Returns pulls loaded."""
    from datetime import datetime, timedelta, timezone

    from app.models.agent_invocation import AgentInvocation
    from app.models.trajectory_reward import TrajectoryReward

    since = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    rows = (
        db.query(TrajectoryReward, AgentInvocation)
        .join(AgentInvocation, AgentInvocation.id == TrajectoryReward.invocation_id)
        .filter(TrajectoryReward.created_date >= since)
        .order_by(TrajectoryReward.created_date.asc())
        .limit(5000)
        .all()
    )
    bandit = get_route_bandit()
    n = 0
    for reward_row, invocation in rows:
        if reward_row.route:
            bandit.update(reward_row.route, reward_row.reward)
            n += 1
    if n:
        logger.info("policy_bandit: bootstrapped %d pulls from rewards", n)
    return n


def bandit_backed_routing_decision(
    fsm_mean: float, fsm_pulls: int,
    react_mean: float, react_pulls: int,
    min_pulls: int = 20,
) -> float | None:
    """Reward-driven FSM-vs-ReAct signal for routing_feedback.

    Returns the suggested FSM-plan-confidence delta direction:
    negative → route more to FSM, positive → fewer, None → inconclusive.
    (Kept as a *function of means* so the caller applies its own threshold
    arithmetic — the bandit advises, the router decides.)
    """
    if fsm_pulls < min_pulls or react_pulls < min_pulls:
        return None
    delta = fsm_mean - react_mean
    if delta > 0.1:
        return -0.05   # FSM better → lower threshold (more FSM traffic)
    if delta < -0.1:
        return 0.05    # FSM worse → raise threshold
    return None


__all__ = [
    "EpsilonGreedyBandit",
    "get_route_bandit",
    "record_reward",
    "bootstrap_from_rewards",
    "bandit_backed_routing_decision",
]
