"""Tests for LLMEndpoint threading through the SynexiaFSM LLM call sites.

Verifies that a resolved hierarchical endpoint (project/agent → llm_models)
actually reaches the wire:

1. ``call_llm(endpoint=...)`` — POST targets the endpoint's base_url +
   model_id + api_key (NOT global settings), and task-based model routing
   is bypassed.
2. ``stream_chat_completion(endpoint=...)`` — same for the streaming path
   used by FSM FINALIZE.
3. ``ExecutionRequest.endpoint`` — the FSM input carries the pin and
   ``parse_task_spec`` / ``generate_plan`` / ``execute_plan_nodes`` /
   ``verify_with_llm`` accept and forward it.
4. ``endpoint=None`` keeps the legacy fallback (get_model / llm_url).
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import httpx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _fake_response(payload):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


def _endpoint():
    from app.services.llm_router import LLMEndpoint

    return LLMEndpoint(
        base_url="https://api.moonshot.cn/v1",
        api_key="kimi-secret-key",
        model_id="kimi-k2.6",
        provider="moonshot",
        context_window=131072,
    )


class TestCallLlmEndpoint(unittest.TestCase):
    """call_llm(endpoint=...) must target the pinned provider+model."""

    def test_endpoint_overrides_globals(self):
        from app.services.llm_service import call_llm

        endpoint = _endpoint()
        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "hi"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(call_llm(prompt="hello", messages=[], endpoint=endpoint))

        self.assertEqual(captured["url"], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(captured["json"]["model"], "kimi-k2.6")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer kimi-secret-key")
        self.assertEqual(result["response"], "hi")
        self.assertEqual(result["model"], "kimi-k2.6")

    def test_endpoint_none_falls_back_to_globals(self):
        """Regression: no endpoint → legacy globals still drive the call."""
        from app.services import llm_service
        from app.services.llm_service import call_llm

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "legacy"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(call_llm(prompt="hello", messages=[]))

        self.assertEqual(captured["url"], llm_service.llm_url())
        self.assertEqual(captured["json"]["model"], llm_service.get_model())
        self.assertEqual(result["response"], "legacy")


class TestStreamChatCompletionEndpoint(unittest.TestCase):
    """stream_chat_completion(endpoint=...) — the FSM FINALIZE path."""

    def test_endpoint_overrides_globals(self):
        from app.services.llm_service import stream_chat_completion

        endpoint = _endpoint()
        captured = {}

        class FakeResp:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def aiter_lines(self):
                async def gen():
                    yield 'data: {"choices":[{"delta":{"content":"Hi"}}]}'
                    yield "data: [DONE]"
                return gen()

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def stream(self, method, url, headers=None, json=None, **kwargs):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()

        async def go():
            out = []
            async for delta in stream_chat_completion("hello", endpoint=endpoint):
                out.append(delta)
            return "".join(out)

        with patch.object(httpx, "AsyncClient", FakeClient):
            text = asyncio.run(go())
        self.assertEqual(captured["url"], "https://api.moonshot.cn/v1/chat/completions")
        self.assertEqual(captured["json"]["model"], "kimi-k2.6")
        self.assertEqual(text, "Hi")


class TestExecutionRequestEndpoint(unittest.TestCase):
    """FSM input carries the pin; all internal LLM entry points accept it."""

    def test_execution_request_field(self):
        from app.services.synexia.fsm import ExecutionRequest

        ep = _endpoint()
        req = ExecutionRequest(
            conversation_id="c1", agent_name="a", user_message="hi", endpoint=ep,
        )
        self.assertIs(req.endpoint, ep)

    def test_parse_task_spec_forwards_endpoint(self):
        from app.services import llm_service
        from app.services.synexia.task_spec_parser import parse_task_spec

        ep = _endpoint()
        captured = {}

        async def fake_call_llm(**kwargs):
            captured["endpoint"] = kwargs.get("endpoint")
            return {
                "data": {
                    "task_kind": "general",
                    "artifact_intents": [],
                    "entities": {},
                    "requires_data": False,
                    "is_followup": False,
                },
                "response": None,
            }

        with patch.object(llm_service, "call_llm", side_effect=fake_call_llm):
            parse_task_spec("hi", endpoint=ep)
        self.assertIs(captured["endpoint"], ep)

    def test_generate_plan_forwards_endpoint(self):
        from app.services import llm_service
        from app.services.synexia.plan_dag import generate_plan

        ep = _endpoint()
        captured = {}

        async def fake_call_llm(**kwargs):
            captured["endpoint"] = kwargs.get("endpoint")
            return {"data": [], "response": "[]"}

        with patch.object(llm_service, "call_llm", side_effect=fake_call_llm):
            with patch("app.services.synexia.plan_dag.Plan"):
                with patch("app.services.synexia.plan_dag.PlanNode"):
                    try:
                        generate_plan(
                            db=MagicMock(), execution_id="e1", task_spec={},
                            context_manifest={}, agent_name="a", endpoint=ep,
                        )
                    except Exception:
                        # DB plumbing is mocked away; the endpoint capture is
                        # what matters here.
                        pass
        self.assertIs(captured["endpoint"], ep)

    def test_execute_plan_nodes_forwards_endpoint_to_nl2sql(self):
        from app.services.synexia import capability_router

        ep = _endpoint()

        # _execute_single_node must forward endpoint to the nl2sql executor.
        captured = {}

        def fake_nl2sql(db, execution, node, data_ctx_extras=None, endpoint=None):
            captured["endpoint"] = endpoint
            return MagicMock(success=True)

        with patch.object(
            capability_router, "_execute_nl2sql_node", side_effect=fake_nl2sql,
        ):
            node = MagicMock(node_type="nl2sql", inputs={"question": "q"})
            capability_router._execute_single_node(
                MagicMock(), MagicMock(), node, "u1", endpoint=ep,
            )
        self.assertIs(captured["endpoint"], ep)

    def test_verify_with_llm_forwards_endpoint(self):
        from app.config import settings as _settings
        from app.services import llm_service
        from app.services.synexia.verifier import verify_with_llm

        ep = _endpoint()
        captured = {}

        def fake_json_sync(prompt, schema=None, temperature=0.0, endpoint=None):
            captured["endpoint"] = endpoint
            return {"checks": []}

        with patch.object(_settings, "SYNEXIA_VERIFIER_LLM_ENABLED", True):
            with patch.object(llm_service, "chat_completion_json_sync", side_effect=fake_json_sync):
                verify_with_llm(MagicMock(), MagicMock(), endpoint=ep)
        self.assertIs(captured["endpoint"], ep)


if __name__ == "__main__":
    unittest.main()
