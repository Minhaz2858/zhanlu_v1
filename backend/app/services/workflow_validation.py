"""Flow graph validation. Pure functions — no DB, no side effects.

Required-config rules come from the node-type registry
(services/workflow_node_types.py) so the schema stays in ONE place.
"""

from typing import Any

from app.services.workflow_node_types import NODE_TYPES, is_trigger_type, required_config_fields


def validate_flow(flow: dict[str, Any]) -> list[str]:
    """Return a list of human-readable errors (empty == valid)."""
    errors: list[str] = []
    if not isinstance(flow, dict) or "nodes" not in flow or "edges" not in flow:
        return ["flow must contain 'nodes' and 'edges'"]
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    node_ids = set()
    for n in nodes:
        nid = n.get("id")
        if not nid:
            errors.append("node missing id")
            continue
        node_ids.add(nid)
        ntype = n.get("type")
        if ntype not in NODE_TYPES:
            errors.append(f"node {nid}: unknown type {ntype!r}")
            continue
        config = (n.get("data") or {}).get("config") or {}
        for field in required_config_fields(ntype):
            if not config.get(field):
                errors.append(f"node {nid} ({ntype}): missing required config '{field}'")
    # dangling edges
    for e in edges:
        if e.get("source") not in node_ids or e.get("target") not in node_ids:
            errors.append(f"edge {e.get('id')}: dangling (source/target not found)")
    # cycle detection (Kahn)
    adj = {nid: [] for nid in node_ids}
    indeg = {nid: 0 for nid in node_ids}
    for e in edges:
        if e["source"] in adj and e["target"] in adj:
            adj[e["source"]].append(e["target"])
            indeg[e["target"]] += 1
    queue = [nid for nid, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    if seen != len(node_ids):
        errors.append("cycle detected in workflow graph")
    # structural requirements
    types = {n.get("id"): n.get("type") for n in nodes}
    if sum(1 for t in types.values() if is_trigger_type(t)) != 1:
        errors.append("workflow must have exactly one trigger node (chat, manual, schedule or webhook)")
    if "output" not in types.values():
        errors.append("workflow must have at least one output node")
    return errors
