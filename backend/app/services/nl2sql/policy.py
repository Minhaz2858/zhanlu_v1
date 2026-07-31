"""NL2SQL policy guard — enforces allow-lists and row/cost limits per binding."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str = ""
    row_limit: int = 1000
    cost_threshold: float = 1000.0       # pg cost units
    max_execution_ms: int = 5000


@dataclass
class PolicyConfig:
    """Per-binding configuration merged from ``AgentDataBinding`` row."""

    allowed_tables: list[str] = field(default_factory=list)
    allowed_columns: list[str] = field(default_factory=list)
    block_tables: list[str] = field(default_factory=list)
    row_limit: int = 1000
    cost_threshold: float = 1000.0
    max_execution_ms: int = 5000


def evaluate(
    tables_referenced: list[str],
    estimated_cost: float,
    *,
    policy_config: PolicyConfig | None = None,
) -> PolicyDecision:
    """Evaluate whether a planned query should be allowed.

    Args:
        tables_referenced: Tables that will be touched by the query.
        estimated_cost: Estimated cost from ``EXPLAIN`` (pg cost units).
        policy_config: Per-binding limits; if ``None``, uses defaults.

    Returns:
        ``PolicyDecision`` with ``allowed`` and a human-readable reason.
    """
    conf = policy_config or PolicyConfig()

    # 1. Allow-list check
    if conf.allowed_tables:
        allowed_lower = {t.lower() for t in conf.allowed_tables}
        for t in tables_referenced:
            if t.lower() not in allowed_lower:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Table '{t}' is not in the agent's allowed-tables list",
                    row_limit=conf.row_limit,
                    cost_threshold=conf.cost_threshold,
                    max_execution_ms=conf.max_execution_ms,
                )

    # 2. Block-list check
    if conf.block_tables:
        block_lower = {t.lower() for t in conf.block_tables}
        for t in tables_referenced:
            if t.lower() in block_lower:
                return PolicyDecision(
                    allowed=False,
                    reason=f"Table '{t}' is explicitly blocked for this agent",
                    row_limit=conf.row_limit,
                    cost_threshold=conf.cost_threshold,
                    max_execution_ms=conf.max_execution_ms,
                )

    # 3. Cost threshold
    if estimated_cost > conf.cost_threshold:
        return PolicyDecision(
            allowed=False,
            reason=f"Estimated cost {estimated_cost:.1f} exceeds threshold {conf.cost_threshold:.1f}",
            row_limit=conf.row_limit,
            cost_threshold=conf.cost_threshold,
            max_execution_ms=conf.max_execution_ms,
        )

    return PolicyDecision(
        allowed=True,
        reason="OK",
        row_limit=conf.row_limit,
        cost_threshold=conf.cost_threshold,
        max_execution_ms=conf.max_execution_ms,
    )
