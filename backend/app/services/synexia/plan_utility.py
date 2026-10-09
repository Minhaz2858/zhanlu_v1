"""Utility-based plan scoring (IBM reasoning/planning articles, P2-6).

The FSM GATE previously evaluated plans on policy only (allow/deny +
risk tier). This adds a deterministic UTILITY estimate — how good the
plan looks on cost, latency and risk axes BEFORE execution — surfaced in
the policy decision payload and logs so approval (human or automatic)
can weigh it.

Utility model (0-1, higher = better):
    utility = 1 − clamp(0.35·cost_norm + 0.35·latency_norm + 0.30·risk_norm)

All components are deterministic node-graph heuristics — no LLM calls,
never raises.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_WRITE_FRAGMENTS = (
    "delete", "drop", "update", "insert", "write", "send", "email",
    "publish", "restart", "kill", "approve", "export", "create_artifact",
)

# Per-node-type LLM-call cost proxy (a synthesize/agent node is a full
# LLM turn; a tool node is cheap).
_NODE_COST = {
    "synthesize": 3.0,
    "agent": 2.5,
    "skill": 1.5,
    "nl2sql": 1.5,
    "sandbox": 1.0,
    "tool": 0.5,
}


def _nodes_of(plan) -> list:
    nodes = getattr(plan, "nodes", None) or []
    return list(nodes)


def _node_field(node, field: str, default=""):
    value = getattr(node, field, default)
    return default if value is None else value


def _graph_depth(nodes: list) -> int:
    """Longest dependency chain (serial bottleneck of the DAG).

    Dependencies may be ORM ``seq`` ints (PlanNode) or spec ``node_id``
    strings (PlanNodeSpec) — both are mapped into one id-space.
    """
    key_of = {
        i: (getattr(n, "seq", None) or getattr(n, "node_id", None) or i)
        for i, n in enumerate(nodes)
    }
    id_to_idx = {key_of[i]: i for i in range(len(nodes))}
    memo: dict[int, int] = {}

    def deps(idx: int) -> list[int]:
        raw = _node_field(nodes[idx], "dependencies", []) or []
        out = []
        for d in raw if isinstance(raw, list) else []:
            j = id_to_idx.get(d)
            if j is not None and j != idx:
                out.append(j)
        return out

    def depth(idx: int, seen: set) -> int:
        if idx in memo:
            return memo[idx]
        if idx in seen:
            return 0  # cycle guard (plans should be acyclic)
        seen.add(idx)
        best = 0
        for j in deps(idx):
            best = max(best, depth(j, seen))
        seen.discard(idx)
        memo[idx] = best + 1
        return memo[idx]

    return max((depth(i, set()) for i in range(len(nodes))), default=0)


def _write_count(nodes: list) -> int:
    count = 0
    for n in nodes:
        text = f"{_node_field(n, 'name')} {_node_field(n, 'description')}".lower()
        if any(frag in text for frag in _WRITE_FRAGMENTS):
            count += 1
    return count


def score_plan(plan) -> dict:
    """Deterministic utility score for a Plan/PlanDAG. Never raises."""
    try:
        nodes = _nodes_of(plan)
        if not nodes:
            return {"utility": None, "components": {}, "warnings": [],
                    "reason": "empty plan"}

        n = len(nodes)
        cost_raw = sum(
            _NODE_COST.get(str(_node_field(node, "node_type", "tool")), 1.0)
            for node in nodes
        )
        cost_norm = min(1.0, cost_raw / 24.0)          # 24 cost units ≈ worst
        depth = _graph_depth(nodes)
        latency_norm = min(1.0, depth / 8.0)            # 8-deep chain ≈ worst
        writes = _write_count(nodes)
        risk_norm = min(1.0, writes / 4.0)              # 4+ write steps ≈ worst

        penalty = 0.35 * cost_norm + 0.35 * latency_norm + 0.30 * risk_norm
        utility = round(max(0.0, min(1.0, 1.0 - penalty)), 3)

        warnings: list[str] = []
        if writes:
            warnings.append(f"{writes} write/destructive step(s)")
        if depth >= 5:
            warnings.append(f"serial chain depth {depth} (latency bottleneck)")
        if n > 12:
            warnings.append(f"large plan ({n} nodes) — consider consolidation")

        return {
            "utility": utility,
            "components": {
                "nodes": n,
                "cost_raw": round(cost_raw, 2),
                "cost_norm": round(cost_norm, 3),
                "depth": depth,
                "latency_norm": round(latency_norm, 3),
                "write_steps": writes,
                "risk_norm": round(risk_norm, 3),
            },
            "warnings": warnings,
        }
    except Exception as exc:  # noqa: BLE001 — advisory scoring only
        logger.debug("plan_utility: scoring failed: %s", exc)
        return {"utility": None, "components": {}, "warnings": []}


__all__ = ["score_plan"]
