"""Workflow DAG runner.

Pure runner: takes a flow graph + a handler dispatch table + a context
object, executes nodes in topological order, and returns the final output.
Node handlers are registered by the caller (real handlers live in
workflow_nodes.py). The runner is testable with fake handlers.

The context contract (ctx):
  - ctx.trigger_input          str   — chat message (trigger_chat handler)
  - ctx.outputs                dict  — node_id -> output, written by runner
  - ctx.record_step(node_id, status, output=None, error=None)  — optional
  - dependency callables per node handler (llm_complete, run_agent, ...)
"""

from typing import Any, Callable


def _topo_order(nodes: list[dict], edges: list[dict]) -> list[dict]:
    node_ids = [n["id"] for n in nodes]
    adj = {nid: [] for nid in node_ids}
    indeg = {nid: 0 for nid in node_ids}
    for e in edges:
        if e["source"] in adj and e["target"] in adj:
            adj[e["source"]].append(e["target"])
            indeg[e["target"]] += 1
    queue = [nid for nid in node_ids if indeg[nid] == 0]
    by_id = {n["id"]: n for n in nodes}
    ordered = []
    while queue:
        cur = queue.pop(0)
        ordered.append(by_id[cur])
        for nxt in adj[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    return ordered


def execute_flow_graph(
    flow: dict,
    ctx: Any,
    handlers: dict[str, Callable],
) -> dict[str, Any]:
    """Run a flow graph.

    Returns {"status": "completed"|"failed", "error": str|None, "output": Any}.
    """
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ordered = _topo_order(nodes, edges)
    final_output = None
    for node in ordered:
        nid = node["id"]
        ntype = node["type"]
        handler = handlers.get(ntype)
        if handler is None:
            err = f"node {nid}: no handler for type {ntype!r}"
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "failed", error=err)
            return {"status": "failed", "error": err, "output": None}
        try:
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "running")
            out = handler(ctx, node)
            ctx.outputs[nid] = out
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "completed", output=out)
            if ntype == "output":
                final_output = out.get("text") or out.get("json") or str(out)
        except Exception as exc:  # noqa: BLE001
            err = f"node {nid} ({ntype}) failed: {exc}"
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "failed", error=err)
            return {"status": "failed", "error": err, "output": None}
    return {"status": "completed", "error": None, "output": final_output}
