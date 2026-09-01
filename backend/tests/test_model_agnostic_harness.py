"""Model-agnostic harness tests: ANY LLM model, ANY context window works.

The harness must never assume a model's context window from its NAME alone.
These tests pin the contract:

- Unknown models get a CONSERVATIVE default (32K) so a small-window model
  compacts early and never overflows the provider with a 400.
- Known families keep their exact windows via heuristics.
- The endpoint probe (vLLM ``max_model_len`` / Ollama ``context_length``)
  overrides the default with the REAL window and registers it by model_id
  so every consumer (compaction, pre-flight, persistence) agrees.
- Admin-set ``LlmModel.context_window`` always wins over the probe.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


# ── Helpers: spin up a fake OpenAI-compatible / Ollama endpoint ──────────

class _FakeModelsServer(BaseHTTPRequestHandler):
    """Serves /v1/models (vLLM-style) and /api/show (Ollama-style)."""

    models = {
        "tiny-local": {"max_model_len": 16_384},
        "mid-local": {"max_model_len": 32_768},
    }

    def do_GET(self):  # noqa: N802
        if self.path == "/v1/models":
            body = json.dumps({
                "object": "list",
                "data": [
                    {"id": mid, "object": "model", **meta}
                    for mid, meta in self.models.items()
                ],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):  # noqa: N802
        if self.path == "/api/show":
            body = json.dumps({
                "model": "ollama-8k",
                "model_info": {"context_length": 8_192},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, format, *args):  # noqa: A002 — keep test output clean
        pass


@pytest.fixture()
def fake_llm_server():
    srv = HTTPServer(("127.0.0.1", 0), _FakeModelsServer)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


# ── Test 1: unknown models are safe (conservative default, no overflow) ──

def test_unknown_model_gets_conservative_window():
    from app.services.compaction import get_context_window

    # Brand-new / obscure models must NOT be assumed 128K (that overflows
    # an 8K/16K model on the very next LLM call).  Compact early instead.
    assert get_context_window("totally-unknown-model-2099") == 32_000
    assert get_context_window("") == 32_000
    # Known families keep their exact windows.
    assert get_context_window("deepseek-chat") == 128_000
    assert get_context_window("gpt-4o") == 128_000
    assert get_context_window("claude-sonnet") == 200_000


# ── Test 2: probe discovers the REAL window from a vLLM endpoint ─────────

def test_probe_vllm_max_model_len(fake_llm_server):
    from app.services.context_probe import (
        get_registered_context_window,
        probe_context_window,
    )
    import app.services.context_probe as cp

    cp._cache.clear()
    cp._registry.clear()

    assert probe_context_window(fake_llm_server, "k", "tiny-local") == 16_384
    assert get_registered_context_window("tiny-local") == 16_384
    assert probe_context_window(fake_llm_server, "k", "mid-local") == 32_768


# ── Test 3: probe falls back to Ollama native /api/show ──────────────────

def test_probe_ollama_context_length(fake_llm_server):
    from app.services.context_probe import probe_context_window
    import app.services.context_probe as cp

    cp._cache.clear()
    cp._registry.clear()

    assert probe_context_window(fake_llm_server, "k", "ollama-8k") == 8_192


# ── Test 4: probed window flows into get_context_window for ALL callers ──

def test_probed_window_flows_into_compaction(fake_llm_server):
    from app.services.compaction import get_context_window
    from app.services.context_probe import probe_context_window
    import app.services.context_probe as cp

    cp._cache.clear()
    cp._registry.clear()

    # Before probing: unknown → conservative 32K.
    assert get_context_window("tiny-local") == 32_000
    # After probing the endpoint: the REAL window wins everywhere.
    probe_context_window(fake_llm_server, "k", "tiny-local")
    assert get_context_window("tiny-local") == 16_384
    # The auto-compact threshold must also scale to the probed window.
    from app.services.compaction import get_autocompact_threshold

    assert get_autocompact_threshold("tiny-local") < 16_384


# ── Test 5: admin-set context_window beats the probe ─────────────────────

def test_admin_set_window_beats_probe(fake_llm_server):
    from app.services.llm_router import LLMEndpoint, _apply_context_window_probe
    import app.services.context_probe as cp

    cp._cache.clear()
    cp._registry.clear()

    ep = LLMEndpoint(
        base_url=fake_llm_server, api_key="k", model_id="tiny-local",
        context_window=7_777,  # admin override
    )
    assert _apply_context_window_probe(ep).context_window == 7_777

    # Probe fills the gap when admin left it NULL.
    ep2 = LLMEndpoint(base_url=fake_llm_server, api_key="k", model_id="tiny-local")
    assert _apply_context_window_probe(ep2).context_window == 16_384


# ── Test 6: probe never raises / never blocks on a dead endpoint ─────────

def test_probe_dead_endpoint_is_safe():
    from app.services.context_probe import probe_context_window
    import app.services.context_probe as cp

    cp._cache.clear()
    cp._registry.clear()

    # Connection refused → None, quickly, no exception.
    assert probe_context_window("http://127.0.0.1:1", "k", "x", timeout=0.5) is None
