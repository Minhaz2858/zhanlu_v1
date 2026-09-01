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
    assert result["status"] == "completed"
    statuses = {s["node_id"]: s["status"] for s in ctx.step_events}
    assert statuses["n3"] == "skipped"
    assert statuses["n4"] == "completed"
    assert "n3" not in ctx.outputs
