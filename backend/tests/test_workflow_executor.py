from app.services.workflow_executor import execute_flow_graph


class FakeContext:
    """Minimal ctx double: node handlers read inputs from upstream outputs."""

    def __init__(self, trigger_input):
        self.trigger_input = trigger_input
        self.outputs = {}
        self.step_events = []

    def record_step(self, node_id, status, output=None, error=None):
        self.step_events.append({"node_id": node_id, "status": status})


def fake_trigger_handler(ctx, node):
    return {"text": ctx.trigger_input}


def fake_llm_handler(ctx, node):
    upstream = next(iter(ctx.current_upstream.values()), {})
    return {"text": f"llm({upstream.get('text', '')})"}


def fake_output_handler(ctx, node):
    upstream = next(iter(ctx.current_upstream.values()), {})
    return {"text": upstream.get("text", "")}


NODE_HANDLERS = {"trigger_chat": fake_trigger_handler, "llm": fake_llm_handler, "output": fake_output_handler}


def test_executes_in_topological_order():
    flow = {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "L", "config": {"prompt": "x"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, NODE_HANDLERS)
    assert result["output"] == "llm(hello)"
    assert result["status"] == "completed"


def test_node_failure_routed_to_output():
    flow = {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "L", "config": {"prompt": "x"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }

    def boom(ctx, node):
        raise RuntimeError("kaboom")

    handlers = {"trigger_chat": fake_trigger_handler, "llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"
    assert "kaboom" in result["error"]


# ---------------------------------------------------------------------------
# Branch routing (n8n): router nodes activate ONLY the chosen output port;
# downstream of inactive ports is recorded as 'skipped' and never runs.
# ---------------------------------------------------------------------------

def fake_condition_handler(ctx, node):
    upstream = ctx.outputs.get("n1", {})
    ok = upstream.get("text") == "go"
    return {"text": upstream.get("text", ""), "branch": "true" if ok else "false", "_route": [0] if ok else [1]}


def make_routed_flow():
    return {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "condition", "position": {"x": 1, "y": 0}, "data": {"label": "C", "config": {}}},
            {"id": "n3", "type": "llm", "position": {"x": 2, "y": 0}, "data": {"label": "TrueBranch", "config": {"prompt": "x"}}},
            {"id": "n4", "type": "llm", "position": {"x": 2, "y": 2}, "data": {"label": "FalseBranch", "config": {"prompt": "x"}}},
            {"id": "n5", "type": "output", "position": {"x": 3, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "sourceHandle": "source-main", "target": "n3"},
            {"id": "e3", "source": "n2", "sourceHandle": "source-main-1", "target": "n4"},
            {"id": "e4", "source": "n3", "target": "n5"},
        ],
    }


def test_true_branch_executes_false_branch_skipped():
    flow = make_routed_flow()
    handlers = {
        "trigger_chat": fake_trigger_handler,
        "condition": fake_condition_handler,
        "llm": fake_llm_handler,
        "output": fake_output_handler,
    }
    ctx = FakeContext("go")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "completed"
    assert result["output"] == "llm(go)"
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n3"] == "completed"   # true branch ran
    assert statuses["n4"] == "skipped"     # false branch did NOT run
    assert "n4" not in ctx.outputs


def test_false_branch_executes_true_branch_skipped():
    flow = make_routed_flow()
    handlers = {
        "trigger_chat": fake_trigger_handler,
        "condition": fake_condition_handler,
        "llm": fake_llm_handler,
        "output": fake_output_handler,
    }
    ctx = FakeContext("stop")
    result = execute_flow_graph(flow, ctx, handlers)
    # The only output node (n5) sits on the true branch; routing to the
    # false branch means NO output node executes. That must fail loudly
    # with a routing error — a silent "completed" with empty output looks
    # like a broken bot in chat (regression: dead switch port returned
    # empty replies).
    assert result["status"] == "failed"
    assert "no output" in result["error"]
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n3"] == "skipped"
    assert statuses["n4"] == "completed"
    assert "n3" not in ctx.outputs


def test_unconnected_switch_port_fails_with_routing_hint():
    """A switch routing to a port with no outgoing edge must surface a
    helpful error naming the nodes that never ran — not a blank reply."""
    flow = {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "condition", "position": {"x": 1, "y": 0}, "data": {"label": "C", "config": {}}},
            {"id": "n3", "type": "llm", "position": {"x": 2, "y": 0}, "data": {"label": "Branch", "config": {"prompt": "x"}}},
            {"id": "n4", "type": "output", "position": {"x": 3, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        # n2 routes to port 1 (false) but only port 0 has an edge — n3/n4
        # never run, the graph completes with no output node executed.
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "sourceHandle": "source-main", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n4"},
        ],
    }
    handlers = {
        "trigger_chat": fake_trigger_handler,
        "condition": fake_condition_handler,
        "llm": fake_llm_handler,
        "output": fake_output_handler,
    }
    ctx = FakeContext("stop")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"
    assert "no output" in result["error"]
    assert "n3" in result["error"] and "n4" in result["error"]


# ---------------------------------------------------------------------------
# n8n-style failure policy: retryOnFail + continueOnFail (per-node config)
# ---------------------------------------------------------------------------

def _linear_flow(node_cfg=None, n2_type="llm"):
    return {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": n2_type, "position": {"x": 1, "y": 0}, "data": {"label": "L", "config": node_cfg or {"prompt": "x"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }


def test_retry_recovers_after_transient_failure():
    flow = _linear_flow({"prompt": "x", "retryOnFail": 1})
    calls = {"n": 0}

    def flaky(ctx, node):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"text": "recovered"}

    handlers = {"trigger_chat": fake_trigger_handler, "llm": flaky, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "completed"
    assert result["output"] == "recovered"
    assert calls["n"] == 2
    statuses = [s["status"] for s in ctx.step_events if s["node_id"] == "n2"]
    assert statuses == ["running", "retrying", "running", "completed"]


def test_retry_exhausted_aborts_with_failed_node_info():
    flow = _linear_flow({"prompt": "x", "retryOnFail": 2})

    def boom(ctx, node):
        raise RuntimeError("kaboom")

    handlers = {"trigger_chat": fake_trigger_handler, "llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"
    assert "kaboom" in result["error"]
    assert result["failed_node_id"] == "n2"
    assert result["failed_node_type"] == "llm"
    # 1 initial + 2 retries = 3 attempts, then failed
    statuses = [s["status"] for s in ctx.step_events if s["node_id"] == "n2"]
    assert statuses == ["running", "retrying", "running", "retrying", "running", "failed"]
    assert "n3" not in ctx.outputs


def test_continue_on_fail_keeps_workflow_running():
    flow = _linear_flow({"prompt": "x", "continueOnFail": True})

    def boom(ctx, node):
        raise RuntimeError("kaboom")

    handlers = {"trigger_chat": fake_trigger_handler, "llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "completed"
    assert "kaboom" in result["output"]  # output node received the error item text
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n2"] == "failed"
    assert statuses["n3"] == "completed"
    assert ctx.outputs["n2"]["success"] is False
    assert ctx.outputs["n2"]["error"] == "kaboom"


def test_continue_on_fail_combines_retry_then_continue():
    """retryOnFail=1 + continueOnFail=True: after both attempts fail, the
    workflow continues with the error item instead of aborting."""
    flow = _linear_flow({"prompt": "x", "retryOnFail": 1, "continueOnFail": True})
    calls = {"n": 0}

    def always_flaky(ctx, node):
        calls["n"] += 1
        raise RuntimeError("transient")

    handlers = {"trigger_chat": fake_trigger_handler, "llm": always_flaky, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "completed"
    assert "transient" in result["output"]  # error item flowed downstream
    assert calls["n"] == 2                  # 1 initial + 1 retry
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n2"] == "failed"
    assert statuses["n3"] == "completed"


def test_continue_off_by_default_aborts():
    flow = _linear_flow({"prompt": "x"})  # no flags — default abort

    def boom(ctx, node):
        raise RuntimeError("kaboom")

    handlers = {"trigger_chat": fake_trigger_handler, "llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"


# ---------------------------------------------------------------------------
# merge (n8n Merge): multiple MAIN inputs converge into one item list.
# The REAL handler runs against the REAL executor — proves the edge-ordered
# current_upstream contract the merge node relies on.
# ---------------------------------------------------------------------------

from app.services.workflow_nodes import handle_merge  # noqa: E402


def fake_branch_handler(ctx, node):
    """Emit one item tagged with the node's label (stands in for set_fields)."""
    return {"text": node["data"]["label"], "items": [{"branch": node["data"]["label"]}]}


def make_merge_flow():
    return {
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "T", "config": {}}},
            {"id": "n2", "type": "set_fields", "position": {"x": 1, "y": 0}, "data": {"label": "A", "config": {"fields": []}}},
            {"id": "n3", "type": "set_fields", "position": {"x": 1, "y": 2}, "data": {"label": "B", "config": {"fields": []}}},
            {"id": "m1", "type": "merge", "position": {"x": 2, "y": 1}, "data": {"label": "M", "config": {}}},
            {"id": "n4", "type": "output", "position": {"x": 3, "y": 1}, "data": {"label": "O", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e2", "source": "n1", "target": "n3", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e3", "source": "n2", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e4", "source": "n3", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main-1"},
            {"id": "e5", "source": "m1", "target": "n4", "sourceHandle": "source-main", "targetHandle": "target-main"},
        ],
    }


def test_merge_combines_parallel_branches():
    """Two branches feed one merge; both run, items concatenate in edge order."""
    flow = make_merge_flow()
    handlers = {
        "trigger_chat": fake_trigger_handler,
        "set_fields": fake_branch_handler,
        "merge": handle_merge,
        "output": fake_output_handler,
    }
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "completed"
    # Both branches ran — no skipped nodes anywhere.
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n2"] == "completed"
    assert statuses["n3"] == "completed"
    assert statuses["m1"] == "completed"
    assert statuses["n4"] == "completed"
    # Merge concatenated the branch items in edge order (n2 then n3).
    assert ctx.outputs["m1"]["items"] == [{"branch": "A"}, {"branch": "B"}]
    assert "A" in result["output"] and "B" in result["output"]
