from app.services.workflow_validation import validate_flow


def test_valid_linear_flow():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat"}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "LLM", "config": {"prompt": "x"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
        ],
    }
    assert validate_flow(flow) == []


def test_cycle_detected():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}},
            {"id": "n2", "type": "condition", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {"branches": []}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "C"}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {"id": "e2", "source": "n2", "target": "n3"},
            {"id": "e3", "source": "n3", "target": "n2"},
        ],
    }
    errors = validate_flow(flow)
    assert any("cycle" in e.lower() for e in errors)


def test_dangling_edge_rejected():
    flow = {
        "version": 1,
        "nodes": [{"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}}],
        "edges": [{"id": "e1", "source": "n1", "target": "n_missing"}],
    }
    assert any("dangling" in e.lower() for e in validate_flow(flow))


def test_missing_required_config():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "A"}},
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "C"}},
        ],
        "edges": [],
    }
    assert any("llm" in e.lower() and "prompt" in e.lower() for e in validate_flow(flow))


def test_no_trigger_and_no_output():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n2", "type": "llm", "position": {"x": 1, "y": 0}, "data": {"label": "B", "config": {"prompt": "x"}}},
        ],
        "edges": [],
    }
    errs = validate_flow(flow)
    assert any("trigger" in e.lower() for e in errs)
    assert any("output" in e.lower() for e in errs)
