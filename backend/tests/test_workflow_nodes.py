from app.services.workflow_nodes import (
    _render,
    all_items,
    first_item,
    handle_agent,
    handle_condition,
    handle_db_query,
    handle_kb_search,
    handle_llm,
    handle_mcp_tool,
    handle_output,
    handle_switch,
    handle_trigger,
)


def test_first_item_defaults_to_text():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "hi"}
    assert first_item(ctx, "n1") == {"text": "hi"}


def test_first_item_returns_first_dict():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "x", "items": [{"a": 1}, {"a": 2}]}
    assert first_item(ctx, "n1") == {"a": 1}


def test_all_items_passthrough():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "x", "items": [{"a": 1}]}
    assert all_items(ctx, "n1") == [{"a": 1}]


def test_all_items_defaults_to_text_item():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "hi"}
    assert all_items(ctx, "n1") == [{"text": "hi"}]


class FakeCtx:
    def __init__(self, **deps):
        self.deps = deps
        self.outputs = {}
        self.current_upstream = {}
        self.trigger_input = "hello"
        self.calls = []

    def llm_complete(self, prompt, model=None, **kw):
        return self.deps.get("llm_text", "llm-ok")

    def kb_search(self, kb_id, query, top_k=5):
        return self.deps.get("kb_results", [{"text": "doc1"}])

    def db_query(self, kb_id, query, mode="sql", limit=50):
        self.calls.append({"kb_id": kb_id, "query": query, "mode": mode, "limit": limit})
        return self.deps.get("db_result", {"text": "row1", "rows": [{"a": 1}]})

    def mcp_call(self, server_id, tool_name, arguments):
        self.calls.append({"server_id": server_id, "tool_name": tool_name, "arguments": arguments})
        return self.deps.get("mcp_result", {"success": True, "result": "mcp-ok"})

    def run_agent(self, agent_id, input_text, **kwargs):
        self.calls.append({"agent_id": agent_id, "input_text": input_text, "kwargs": kwargs})
        return {"text": "agent-done"}


def _node(nid, ntype, config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def test_trigger_passes_message():
    ctx = FakeCtx()
    out = handle_trigger(ctx, _node("n1", "trigger_chat", {}))
    assert out == {"text": "hello", "trigger": "chat", "items": [{"text": "hello"}]}


def test_llm_uses_prompt_template_and_upstream():
    ctx = FakeCtx(llm_text="llm-ok")
    ctx.outputs["n1"] = {"text": "the question"}
    node = _node("n2", "llm", {"prompt": "Answer: {{inputs.n1.text}}"})
    out = handle_llm(ctx, node)
    assert out["text"] == "llm-ok"


def test_condition_routes_by_upstream_value():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "5"}}
    node = _node("n3", "condition", {"branches": [
        {"op": "gt", "value": "3", "on_true": "pass"},
        {"op": "default", "on_true": "fail"},
    ]})
    out = handle_condition(ctx, node)
    assert out == {"text": "5", "branch": "pass", "_route": [0]}


def test_output_formats_markdown():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "# Title"}}
    out = handle_output(ctx, _node("n4", "output", {"format": "markdown"}))
    assert out["text"] == "# Title"


def test_kb_search_uses_query_and_returns_docs():
    ctx = FakeCtx(kb_results=[{"text": "doc1"}, {"text": "doc2"}])
    ctx.outputs["n1"] = {"text": "safety policy"}
    node = _node("n5", "kb_search", {"kb_id": "kb1", "query": "{{inputs.n1.text}}", "top_k": 2})
    out = handle_kb_search(ctx, node)
    assert out["text"] == "doc1\n\ndoc2"
    assert len(out["docs"]) == 2


def test_render_resolves_item_field_paths():
    """{{inputs.<node>.items[N].field}} deep paths + back-compat."""
    ctx = FakeCtx()
    ctx.outputs["n2"] = {
        "text": "rows",
        "items": [{"name": "Laptop", "price": 1200}, {"name": "Chair", "price": 150}],
    }
    assert _render("{{inputs.n2.items[0].name}} costs {{inputs.n2.items[0].price}}", ctx) == "Laptop costs 1200"
    assert _render("{{inputs.n2.items}}", ctx) == "[{'name': 'Laptop', 'price': 1200}, {'name': 'Chair', 'price': 150}]"
    assert _render("{{inputs.n2.text}}", ctx) == "rows"  # back-compat
    assert _render("{{inputs.n2.items[9].name}}", ctx) == ""  # out of range -> ""


def test_render_missing_path_returns_empty():
    ctx = FakeCtx()
    ctx.outputs["n2"] = {"text": "x", "items": [{"a": 1}]}
    assert _render("{{inputs.n2.items[0].missing}}", ctx) == ""


def test_render_leaves_unknown_placeholders_untouched():
    ctx = FakeCtx()
    ctx.outputs["n2"] = {"text": "x", "items": []}
    assert _render("{{inputs.n9.anything}}", ctx) == ""
    assert _render("{{ not_a_path }}", ctx) == "{{ not_a_path }}"


def test_trigger_emits_item():
    out = handle_trigger(FakeCtx(), _node("n1", "trigger_chat", {}))
    assert out["items"] == [{"text": "hello"}]


def test_llm_emits_item():
    out = handle_llm(FakeCtx(), _node("n2", "llm", {"prompt": "hi"}))
    assert out["items"] == [{"text": "llm-ok"}]


def test_db_query_emits_rows_as_items():
    ctx = FakeCtx(db_result={"text": "a", "rows": [{"a": 1}, {"a": 2}]})
    out = handle_db_query(ctx, _node("n6", "db_query", {"kb_id": "k", "mode": "sql", "sql": "S"}))
    assert out["items"] == [{"a": 1}, {"a": 2}]


def test_kb_search_emits_docs_as_items():
    ctx = FakeCtx(kb_results=[{"text": "d1", "source": "s1", "score": 0.9}])
    out = handle_kb_search(ctx, _node("n5", "kb_search", {"kb_id": "k", "query": "q"}))
    assert out["items"][0]["source"] == "s1"


def test_condition_passes_items_through():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "x", "items": [{"a": 1}]}}
    out = handle_condition(ctx, _node("n4", "condition", {"op": "eq", "value": "x"}))
    assert out["_route"] == [0]
    assert out["items"] == [{"a": 1}]


def test_agent_resolves_providers_from_supplemental_connections():
    """n8n-style wiring: ai_llm provider sets the sub-agent model, ai_kb
    providers add retrieval context, ai_tool providers list tools."""
    ctx = FakeCtx()
    ctx.node_by_id = {
        "n1": _node("n1", "llm", {"prompt": "x", "model": "deepseek-chat"}),
        "n2": _node("n2", "kb_search", {"kb_id": "kb1", "query": "q"}),
        "n3": _node("n3", "tool", {"tool_id": "toolA"}),
    }
    ctx.outputs["n2"] = {"text": "ctx", "docs": [{"text": "kb chunk"}]}
    ctx.supplemental_upstreams = {
        "n4": {"ai_llm": ["n1"], "ai_kb": ["n2"], "ai_tool": ["n3"]},
    }
    node = _node("n4", "agent", {"agent_id": "ag1", "input_template": "{{inputs.n1.text}}"})
    ctx.outputs["n1"] = {"text": "hi"}
    ctx.current_upstream = {"n1": {"text": "hi"}}
    out = handle_agent(ctx, node)
    assert ctx.calls[-1]["kwargs"]["model"] == "deepseek-chat"
    assert "kb chunk" in ctx.calls[-1]["kwargs"]["extra_context"]
    assert "toolA" in ctx.calls[-1]["kwargs"]["extra_context"]
    assert out["text"] == "agent-done"


def test_db_query_sql_mode_renders_template_and_returns_rows():
    ctx = FakeCtx(db_result={"text": '{"sales": 100}', "rows": [{"sales": 100}]})
    ctx.outputs["n1"] = {"text": "shirts"}
    node = _node("n6", "db_query", {
        "kb_id": "kb_sales", "mode": "sql",
        "sql": "SELECT * FROM orders WHERE product = '{{inputs.n1.text}}' LIMIT 5",
    })
    out = handle_db_query(ctx, node)
    call = ctx.calls[-1]
    assert call["kb_id"] == "kb_sales"
    assert call["query"] == "SELECT * FROM orders WHERE product = 'shirts' LIMIT 5"
    assert call["mode"] == "sql"
    assert out["count"] == 1
    assert out["rows"] == [{"sales": 100}]


def test_db_query_nl_mode_uses_question():
    ctx = FakeCtx(db_result={"text": "0 rows returned.", "rows": []})
    ctx.outputs["n1"] = {"text": "total sales"}
    node = _node("n6", "db_query", {
        "kb_id": "kb_sales", "mode": "nl",
        "question": "{{inputs.n1.text}}", "limit": 20,
    })
    out = handle_db_query(ctx, node)
    call = ctx.calls[-1]
    assert call["mode"] == "nl"
    assert call["query"] == "total sales"
    assert call["limit"] == 20
    assert out["count"] == 0


def test_mcp_tool_passes_arguments_json_with_template():
    ctx = FakeCtx(mcp_result={"success": True, "result": "feishu msg sent"})
    ctx.outputs["n1"] = {"text": "hello"}
    node = _node("n7", "mcp_tool", {
        "server_id": "mcp1", "tool_name": "send_message",
        "arguments": '{"text": "{{inputs.n1.text}}"}',
    })
    out = handle_mcp_tool(ctx, node)
    call = ctx.calls[-1]
    assert call["server_id"] == "mcp1"
    assert call["tool_name"] == "send_message"
    assert call["arguments"] == {"text": "hello"}
    assert out["text"] == "feishu msg sent"
    assert out["success"] is True


def test_mcp_tool_defaults_to_upstream_text():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "default payload"}}
    node = _node("n7", "mcp_tool", {"server_id": "mcp1", "tool_name": "echo", "arguments": ""})
    out = handle_mcp_tool(ctx, node)
    assert ctx.calls[-1]["arguments"] == {"input": "default payload"}
    assert out["text"] == "mcp-ok"


def test_mcp_tool_surfaces_failure_as_text():
    ctx = FakeCtx(mcp_result={"success": False, "error": "tool exploded"})
    node = _node("n7", "mcp_tool", {"server_id": "mcp1", "tool_name": "boom", "arguments": ""})
    out = handle_mcp_tool(ctx, node)
    assert out["success"] is False
    assert "tool exploded" in out["text"]


def test_condition_schema_form_field_op_value():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "7"}}
    node = _node("n3", "condition", {"field": "{{inputs.n1.text}}", "op": "gt", "value": "5", "on_true": "big", "on_false": "small"})
    out = handle_condition(ctx, node)
    assert out == {"text": "7", "branch": "big", "_route": [0], "items": [{"text": "7"}]}
    node2 = _node("n3", "condition", {"field": "", "op": "gt", "value": "10", "on_true": "big", "on_false": "small"})
    assert handle_condition(ctx, node2) == {"text": "7", "branch": "small", "_route": [1], "items": [{"text": "7"}]}


def test_switch_routes_to_first_matching_case():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "urgent"}}
    node = _node("n5", "switch", {"cases": [
        {"field": "{{inputs.n1.text}}", "op": "contains", "value": "urgent"},
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "low"},
    ]})
    out = handle_switch(ctx, node)
    assert out["case"] == 0
    assert out["_route"] == [0]


def test_switch_routes_to_default_when_no_case_matches():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "medium"}}
    node = _node("n5", "switch", {"cases": [
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "urgent"},
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "low"},
    ], "fallback_label": "default"})
    out = handle_switch(ctx, node)
    assert out["case"] == "default"
    assert out["_route"] == [3]


def test_switch_third_case_routes_to_port_two():
    ctx = FakeCtx()
    ctx.current_upstream = {"n1": {"text": "low"}}
    node = _node("n5", "switch", {"cases": [
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "urgent"},
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "normal"},
        {"field": "{{inputs.n1.text}}", "op": "eq", "value": "low"},
    ]})
    out = handle_switch(ctx, node)
    assert out["case"] == 2
    assert out["_route"] == [2]
