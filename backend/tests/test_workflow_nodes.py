from app.services.workflow_nodes import (
    handle_condition,
    handle_kb_search,
    handle_llm,
    handle_output,
    handle_trigger,
)


class FakeCtx:
    def __init__(self, **deps):
        self.deps = deps
        self.outputs = {}
        self.trigger_input = "hello"

    def llm_complete(self, prompt, model=None, **kw):
        return self.deps.get("llm_text", "llm-ok")

    def kb_search(self, kb_id, query, top_k=5):
        return self.deps.get("kb_results", [{"text": "doc1"}])


def _node(nid, ntype, config):
    return {"id": nid, "type": ntype, "position": {"x": 0, "y": 0},
            "data": {"label": nid, "config": config}}


def test_trigger_passes_message():
    ctx = FakeCtx()
    out = handle_trigger(ctx, _node("n1", "trigger_chat", {}))
    assert out == {"text": "hello"}


def test_llm_uses_prompt_template_and_upstream():
    ctx = FakeCtx(llm_text="llm-ok")
    ctx.outputs["n1"] = {"text": "the question"}
    node = _node("n2", "llm", {"prompt": "Answer: {{inputs.n1.text}}"})
    out = handle_llm(ctx, node)
    assert out["text"] == "llm-ok"


def test_condition_routes_by_upstream_value():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "5"}
    node = _node("n3", "condition", {"branches": [
        {"op": "gt", "value": "3", "on_true": "pass"},
        {"op": "default", "on_true": "fail"},
    ]})
    out = handle_condition(ctx, node)
    assert out == {"branch": "pass"}


def test_output_formats_markdown():
    ctx = FakeCtx()
    ctx.outputs["n1"] = {"text": "# Title"}
    out = handle_output(ctx, _node("n4", "output", {"format": "markdown"}))
    assert out["text"] == "# Title"


def test_kb_search_uses_query_and_returns_docs():
    ctx = FakeCtx(kb_results=[{"text": "doc1"}, {"text": "doc2"}])
    ctx.outputs["n1"] = {"text": "safety policy"}
    node = _node("n5", "kb_search", {"kb_id": "kb1", "query": "{{inputs.n1.text}}", "top_k": 2})
    out = handle_kb_search(ctx, node)
    assert out["text"] == "doc1\n\ndoc2"
    assert len(out["docs"]) == 2
