from app.services.workflow_executor import execute_flow_graph


class FakeContext:
    """Minimal ctx double: node handlers read inputs from upstream outputs."""

    def __init__(self, trigger_input):
        self.trigger_input = trigger_input
        self.outputs = {}
        self.step_events = []

    def record_step(self, node_id, status, output=None, error=None):
        self.step_events.append({"node_id": node_id, "status": status})


def fake_llm_handler(ctx, node):
    upstream = ctx.outputs.get("n1", {})
    return {"text": f"llm({upstream.get('text', '')})"}


def fake_output_handler(ctx, node):
    upstream = ctx.outputs.get("n2", {})
    return {"text": upstream.get("text", "")}


NODE_HANDLERS = {"llm": fake_llm_handler, "output": fake_output_handler}


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

    handlers = {"llm": boom, "output": fake_output_handler}
    ctx = FakeContext("hello")
    result = execute_flow_graph(flow, ctx, handlers)
    assert result["status"] == "failed"
    assert "kaboom" in result["error"]
