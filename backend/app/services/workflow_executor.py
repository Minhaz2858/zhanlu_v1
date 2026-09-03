"""Workflow DAG runner.

Pure runner: takes a flow graph + a handler dispatch table + a context
object, executes nodes in topological order, and returns the final output.
Node handlers are registered by the caller (real handlers live in
workflow_nodes.py). The runner is testable with fake handlers.

Branch routing (n8n semantics): router nodes (condition / switch) return an
output dict containing ``_route`` — the list of output-port indices that
received the item. The runner activates ONLY the outgoing edges from those
ports; nodes downstream of inactive ports are recorded as ``skipped`` and
never executed. Non-router nodes activate all of their outgoing edges.

Source-handle convention: ``source-main`` = port 0, ``source-main-1`` =
port 1, ...; supplemental handles (``source-ai_llm`` etc.) are provider
wiring and are always active.

The context contract (ctx):
  - ctx.trigger_input          str   — chat message (trigger handler)
  - ctx.outputs                dict  — node_id -> output, written by runner
  - ctx.record_step(node_id, status, output=None, error=None)  — optional
  - dependency callables per node handler (llm_complete, run_agent, ...)
"""

from typing import Any, Callable

from app.services.workflow_node_types import is_router_type
from app.services.tracing.spans import workflow_node_span


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


def _parse_source_handle(source_handle: str | None) -> tuple[str, int]:
    """'source-main' -> ('main', 0); 'source-main-2' -> ('main', 2)."""
    sh = (source_handle or "source-main").removeprefix("source-")
    if "-" in sh:
        head, _, tail = sh.rpartition("-")
        if tail.isdigit():
            return head, int(tail)
    return sh, 0


def execute_flow_graph(
    flow: dict,
    ctx: Any,
    handlers: dict[str, Callable],
    *,
    partial_from: str | None = None,
    seed_outputs: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Run a flow graph.

    Returns {"status": "completed"|"failed", "error": str|None, "output": Any}.

    Partial re-run mode (iterate-loop / rerun-from-node): when ``partial_from``
    is a node id, only that node and everything downstream of it (via main
    edges) executes again. Outputs for every other node must be supplied in
    ``seed_outputs`` — they act as reference values captured from a prior
    completed run (the caller of a rerun is expected to pass the full output
    map of the original execution). Seeded nodes are NOT re-executed; their
    stored outputs are pre-loaded into ``ctx.outputs`` and their edges are
    activated exactly as in the original run (router ``_route`` honored), so
    the rerun subgraph sees identical upstream items. Seeding a node that IS
    inside the rerun subgraph is ignored (it runs live). With no ``partial_from``
    the behavior is byte-identical to a plain full run.
    """
    nodes = flow.get("nodes") or []
    edges = flow.get("edges") or []
    ordered = _topo_order(nodes, edges)
    node_by_id = {n["id"]: n for n in nodes}

    # n8n-style connection semantics: MAIN edges carry data flow and are
    # branch-activatable; supplemental edges (ai_llm/ai_tool/ai_kb) wire
    # providers/dependencies and are always active.
    main_upstream_map: dict[str, list[tuple[str, int]]] = {n["id"]: [] for n in nodes}
    main_outgoing: dict[str, list[tuple[str, int]]] = {n["id"]: [] for n in nodes}
    supplemental_upstreams: dict[str, dict[str, list[str]]] = {}
    for e in edges:
        ctype, index = _parse_source_handle(e.get("sourceHandle"))
        if ctype == "main":
            main_upstream_map.setdefault(e["target"], []).append((e["source"], index))
            main_outgoing.setdefault(e["source"], []).append((e["target"], index))
        else:
            supplemental_upstreams.setdefault(e["target"], {}).setdefault(ctype, []).append(e["source"])
    ctx.node_by_id = node_by_id
    ctx.supplemental_upstreams = supplemental_upstreams

    # active[target] = set of upstream node ids whose edge is active
    active: dict[str, set[str]] = {n["id"]: set() for n in nodes}
    final_output = None

    def _activate(nid: str, ntype: str, out: dict) -> None:
        """Activate outgoing main edges — routers only from chosen ports."""
        if is_router_type(ntype):
            chosen = set(out.get("_route") or [])
            if not chosen:
                chosen = {0}
        else:
            chosen = None  # all ports
        for tgt, index in main_outgoing.get(nid, []):
            if chosen is None or index in chosen:
                active.setdefault(tgt, set()).add(nid)

    # Partial re-run mode: build the rerun subgraph (partial_from + all
    # downstream via main edges), pre-load reference outputs for everything
    # outside it, and reconstruct edge activation from those stored outputs.
    rerun_set: set[str] | None = None
    if partial_from is not None:
        if partial_from not in node_by_id:
            err = f"rerun from {partial_from}: node not in flow"
            return {
                "status": "failed", "error": err, "output": None,
                "failed_node_id": partial_from, "failed_node_type": None,
            }
        rerun_set = {partial_from}
        queue = [partial_from]
        while queue:
            cur = queue.pop(0)
            for tgt, _idx in main_outgoing.get(cur, []):
                if tgt not in rerun_set:
                    rerun_set.add(tgt)
                    queue.append(tgt)
        # Seeds = outputs of nodes that are NOT re-run. A seed for a node
        # inside the rerun subgraph is ignored — it executes live anyway.
        seeds = {
            nid: out
            for nid, out in (seed_outputs or {}).items()
            if nid in node_by_id and nid not in rerun_set
        }
        ctx.outputs.update(seeds)
        for nid, out in seeds.items():
            _activate(nid, node_by_id[nid]["type"], out)
        # Every re-run node needs all its upstreams: either they are re-run
        # too, or their reference output was seeded from a completed run.
        for node in ordered:
            nid = node["id"]
            if nid not in rerun_set:
                continue
            for src, _idx in main_upstream_map.get(nid, []):
                if src not in rerun_set and src not in seeds:
                    err = (
                        f"rerun from {partial_from} requires upstream {src} output "
                        "from a completed run — run the full workflow first"
                    )
                    return {
                        "status": "failed", "error": err, "output": None,
                        "failed_node_id": nid, "failed_node_type": node["type"],
                    }

    for node in ordered:
        nid = node["id"]
        if partial_from is not None and nid not in rerun_set:
            continue  # seeded reference node — do not re-execute
        ntype = node["type"]
        upstreams = main_upstream_map.get(nid, [])
        active_in = {src for src, _idx in upstreams if src in active.get(nid, set())}
        if upstreams and not active_in:
            # No active input edge -> branch not taken (n8n: node simply
            # does not run for this execution).
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "skipped")
            continue
        ctx.current_upstream = {
            src: ctx.outputs[src]
            for src, _idx in upstreams
            if src in ctx.outputs and src in active_in
        }
        handler = handlers.get(ntype)
        if handler is None:
            err = f"node {nid}: no handler for type {ntype!r}"
            if hasattr(ctx, "record_step"):
                ctx.record_step(nid, "failed", error=err)
            return {
                "status": "failed", "error": err, "output": None,
                "failed_node_id": nid, "failed_node_type": ntype,
            }

        # n8n-style per-node failure policy (universal schema props):
        #   retryOnFail     — retry attempts before giving up (0 = no retry)
        #   continueOnFail  — on final failure, emit an error item and keep
        #                     the workflow running instead of aborting
        config = (node.get("data") or {}).get("config") or {}
        try:
            retry_on_fail = max(int(config.get("retryOnFail") or 0), 0)
        except (TypeError, ValueError):
            retry_on_fail = 0
        continue_on_fail = bool(config.get("continueOnFail"))

        with workflow_node_span(nid, ntype) as _node_span:
            out: dict = {}
            failure: Exception | None = None
            attempts = retry_on_fail + 1
            for attempt in range(attempts):
                try:
                    if attempt > 0 and hasattr(ctx, "record_step"):
                        ctx.record_step(nid, "retrying", error=f"attempt {attempt + 1} of {attempts}")
                    if hasattr(ctx, "record_step"):
                        ctx.record_step(nid, "running")
                    out = handler(ctx, node)
                    failure = None
                    break
                except Exception as exc:  # noqa: BLE001
                    failure = exc
            if failure is not None:
                err = f"node {nid} ({ntype}) failed: {failure}"
                if hasattr(ctx, "record_step"):
                    ctx.record_step(nid, "failed", error=err)
                _node_span.set_attribute("success", False)
                _node_span.set_attribute("error", err[:500])
                if not continue_on_fail:
                    return {
                        "status": "failed", "error": err, "output": None,
                        "failed_node_id": nid, "failed_node_type": ntype,
                    }
                # Continue-on-fail (n8n semantics): downstream nodes receive an
                # error item; the node itself stays marked failed in the run log.
                out = {
                    "text": f"[{ntype} failed: {failure}]",
                    "items": [],
                    "success": False,
                    "error": str(failure),
                }
            else:
                _node_span.set_attribute("success", True)
                if hasattr(ctx, "record_step"):
                    ctx.record_step(nid, "completed", output=out)
            ctx.outputs[nid] = out
        if ntype == "output":
            final_output = out.get("text") or out.get("json") or str(out)
        _activate(nid, ntype, out)
    if final_output is None:
        # The graph ran but no `output` node executed (e.g. a switch routed
        # to an unconnected default port). Return failed with a routing
        # error instead of "completed" with empty output — the chat path
        # turns an empty reply into a silent blank message that looks like
        # the bot is broken.
        skipped = [n["id"] for n in nodes if n["type"] != "trigger_chat" and n["id"] not in ctx.outputs]
        hint = ""
        if skipped:
            hint = f" (nodes never ran: {', '.join(skipped[:5])})"
        return {
            "status": "failed",
            "error": "workflow produced no output — a switch likely routed to an unconnected port" + hint,
            "output": None,
        }
    return {"status": "completed", "error": None, "output": final_output}
