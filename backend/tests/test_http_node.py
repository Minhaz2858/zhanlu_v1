"""HTTP Request node tests — handler (fake ctx) + runtime (real local stub)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.services.workflow_nodes import handle_http_request


class _FakeCtx:
    def __init__(self, result=None):
        self.result = result or {"text": "[]", "items": [], "status": 200, "success": True}
        self.calls = []

    def http_request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


def _node(config):
    return {"id": "n2", "type": "http_request", "data": {"label": "HTTP", "config": config}}


class _Stub(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self):
        body = json.dumps([{"id": 1}, {"id": 2}]).encode()
        if self.path == "/fail":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"nope")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._respond()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.server.last_body = self.rfile.read(length).decode()
        self._respond()


def _stub_server():
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_handler_renders_templates_and_passes_config():
    ctx = _FakeCtx()
    ctx.calls = []
    node = _node({
        "method": "POST",
        "url": "http://example.com/items",
        "headers": [{"key": "X-Trace", "value": "{{inputs.n1.text}}"}],
        "body": '{"q": "{{inputs.n1.text}}"}',
        "auth_type": "bearer",
        "bearer_token": "tok-{{inputs.n1.text}}",
        "timeout": 5,
    })
    ctx.outputs = {"n1": {"text": "abc"}}
    ctx.current_upstream = {"n1": {"text": "abc"}}
    out = handle_http_request(ctx, node)
    (args, kwargs) = ctx.calls[-1]
    assert args[0] == "POST"
    assert args[1] == "http://example.com/items"
    assert kwargs["headers"] == [{"key": "X-Trace", "value": "abc"}]
    assert kwargs["body"] == '{"q": "abc"}'
    assert kwargs["bearer_token"] == "tok-abc"
    assert out["status"] == 200


def test_runtime_http_request_parses_json_array_into_items():
    from app.services.workflow_runtime import WorkflowExecutionContext as ExecutionContext
    ctx = object.__new__(ExecutionContext)  # http_request uses no db/session state
    server = _stub_server()
    try:
        url = f"http://127.0.0.1:{server.server_port}/json"
        result = ctx.http_request("GET", url)
        assert result["success"] is True
        assert result["items"] == [{"id": 1}, {"id": 2}]
        assert result["status"] == 200
    finally:
        server.shutdown()


def test_runtime_http_request_sends_post_json_body():
    from app.services.workflow_runtime import WorkflowExecutionContext as ExecutionContext
    ctx = object.__new__(ExecutionContext)
    server = _stub_server()
    try:
        url = f"http://127.0.0.1:{server.server_port}/echo"
        result = ctx.http_request("POST", url, body='{"hello": "world"}')
        assert result["success"] is True
        assert json.loads(server.last_body) == {"hello": "world"}
    finally:
        server.shutdown()


def test_runtime_http_request_non_2xx_is_data_not_crash():
    from app.services.workflow_runtime import WorkflowExecutionContext as ExecutionContext
    ctx = object.__new__(ExecutionContext)
    server = _stub_server()
    try:
        url = f"http://127.0.0.1:{server.server_port}/fail"
        result = ctx.http_request("GET", url)
        assert result["success"] is False
        assert result["status"] == 404
        assert "nope" in result["text"]
    finally:
        server.shutdown()


def test_runtime_http_request_bearer_header():
    from app.services.workflow_runtime import WorkflowExecutionContext as ExecutionContext
    ctx = object.__new__(ExecutionContext)
    server = _stub_server()
    try:
        url = f"http://127.0.0.1:{server.server_port}/json"
        result = ctx.http_request("GET", url, auth_type="bearer", bearer_token="sekret")
        assert result["success"] is True
    finally:
        server.shutdown()
