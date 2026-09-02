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


def test_schedule_trigger_accepted():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_schedule", "position": {"x": 0, "y": 0},
             "data": {"label": "Sched", "config": {"cron": "0 9 * * *"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n3"}],
    }
    assert validate_flow(flow) == []


def test_webhook_trigger_requires_token():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_webhook", "position": {"x": 0, "y": 0},
             "data": {"label": "Hook", "config": {"token": ""}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [{"id": "e1", "source": "n1", "target": "n3"}],
    }
    errs = validate_flow(flow)
    assert any("token" in e.lower() for e in errs)


def test_db_query_requires_connector():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat"}},
            {"id": "n2", "type": "db_query", "position": {"x": 1, "y": 0},
             "data": {"label": "DB", "config": {"mode": "sql", "sql": "SELECT 1"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [],
    }
    errs = validate_flow(flow)
    assert any("connector" in e.lower() or "kb_id" in e.lower() for e in errs)


def test_http_request_requires_url():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_manual", "position": {"x": 0, "y": 0}, "data": {"label": "M"}},
            {"id": "n2", "type": "http_request", "position": {"x": 1, "y": 0},
             "data": {"label": "HTTP", "config": {"method": "GET"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [],
    }
    errs = validate_flow(flow)
    assert any("url" in e.lower() for e in errs)


def test_code_requires_code():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_manual", "position": {"x": 0, "y": 0}, "data": {"label": "M"}},
            {"id": "n2", "type": "code", "position": {"x": 1, "y": 0},
             "data": {"label": "Code", "config": {"language": "python"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [],
    }
    errs = validate_flow(flow)
    assert any("code" in e.lower() for e in errs)


def test_mcp_tool_requires_server_and_tool():
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "Chat"}},
            {"id": "n2", "type": "mcp_tool", "position": {"x": 1, "y": 0},
             "data": {"label": "MCP", "config": {"server_id": "m1"}}},
            {"id": "n3", "type": "output", "position": {"x": 2, "y": 0}, "data": {"label": "Out"}},
        ],
        "edges": [],
    }
    errs = validate_flow(flow)
    assert any("tool_name" in e.lower() for e in errs)


def test_merge_flow_validates():
    """A merge receiving TWO upstream edges (parallel branches) validates clean."""
    flow = {
        "version": 1,
        "nodes": [
            {"id": "n1", "type": "trigger_chat", "position": {"x": 0, "y": 0}, "data": {"label": "in", "config": {}}},
            {"id": "n2", "type": "set_fields", "position": {"x": 320, "y": 0}, "data": {"label": "a", "config": {"fields": []}}},
            {"id": "n3", "type": "set_fields", "position": {"x": 320, "y": 200}, "data": {"label": "b", "config": {"fields": []}}},
            {"id": "m1", "type": "merge", "position": {"x": 640, "y": 100}, "data": {"label": "m", "config": {}}},
            {"id": "n4", "type": "output", "position": {"x": 960, "y": 100}, "data": {"label": "out", "config": {}}},
        ],
        "edges": [
            {"id": "e1", "source": "n1", "target": "n2", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e2", "source": "n1", "target": "n3", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e3", "source": "n2", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main"},
            {"id": "e4", "source": "n3", "target": "m1", "sourceHandle": "source-main", "targetHandle": "target-main-1"},
            {"id": "e5", "source": "m1", "target": "n4", "sourceHandle": "source-main", "targetHandle": "target-main"},
        ],
    }
    assert validate_flow(flow) == []
