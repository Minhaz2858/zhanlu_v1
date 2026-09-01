"""Code node tests — sandbox (runtime) + handler (fake ctx)."""

from app.services.workflow_nodes import handle_code
from app.services.workflow_runtime import WorkflowExecutionContext as ExecutionContext


def _ctx():
    return object.__new__(ExecutionContext)  # run_code uses no db/session state


def _node(config):
    return {"id": "n2", "type": "code", "data": {"label": "Code", "config": config}}


class _FakeCtx:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run_code(self, code, item, timeout=10):
        self.calls.append((code, item, timeout))
        return self.result


def test_run_code_dict_output():
    r = _ctx().run_code('output = {"name": input.get("name"), "total": input.get("price", 0) * 2}',
                        {"name": "Laptop", "price": 1200})
    assert r["success"] is True
    assert r["items"] == [{"name": "Laptop", "total": 2400}]


def test_run_code_list_output():
    r = _ctx().run_code("output = [i for i in range(3)]", {})
    assert r["items"] == [0, 1, 2]


def test_run_code_missing_output_var():
    r = _ctx().run_code("x = 1", {})
    assert r["success"] is False
    assert "output" in r["text"]


def test_run_code_exception_reported():
    r = _ctx().run_code("output = 1 / 0", {})
    assert r["success"] is False
    assert "ZeroDivisionError" in r["text"]


def test_run_code_timeout():
    r = _ctx().run_code("while True:\n    pass", {}, timeout=1)
    assert r["success"] is False
    assert "timed out" in r["text"]


def test_run_code_blocks_imports():
    r = _ctx().run_code("import os\noutput = {'x': 1}", {})
    assert r["success"] is False
    assert "import" in r["text"] or "os" in r["text"]


def test_run_code_non_serializable_output():
    r = _ctx().run_code("output = {1, 2}", {})  # set literal — json.dumps raises TypeError
    assert r["success"] is False
    assert "JSON" in r["text"]


def test_handler_passes_each_item_and_timeout():
    ctx = _FakeCtx({"success": True, "text": "{}", "items": [{"x": 1}]})
    ctx.outputs = {"n1": {"text": "t", "items": [{"x": 1}, {"x": 2}]}}
    ctx.current_upstream = {"n1": {"text": "t", "items": [{"x": 1}, {"x": 2}]}}
    node = _node({"code": 'output = {"x": input.get("x")}', "timeout": 3})
    out = handle_code(ctx, node)
    assert len(ctx.calls) == 2  # run once per item
    (code, item, timeout) = ctx.calls[0]
    assert item == {"x": 1}
    assert timeout == 3
    assert (code, ctx.calls[1][1], ctx.calls[1][2]) == (code, {"x": 2}, 3)
    assert out["items"] == [{"x": 1}, {"x": 1}]  # fake returns same for both items


def test_handler_surfaces_sandbox_failure():
    ctx = _FakeCtx({"success": False, "text": "Code error: boom"})
    ctx.outputs = {"n1": {"text": "t"}}
    ctx.current_upstream = {"n1": {"text": "t"}}
    out = handle_code(ctx, _node({"code": "bad", "timeout": 1}))
    assert out["success"] is False
    assert "boom" in out["text"]
