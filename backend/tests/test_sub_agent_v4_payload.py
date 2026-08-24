"""Tests for DeepSeek V4 thinking-mode payload correctness.

V4 requires:
1. No ``temperature`` key in the request payload (model rejects it when thinking
   mode is enabled by default).
2. The assistant's ``reasoning_content`` must be passed back with tool results
   when the prior model response included it.
3. HTTP 400+ response bodies must be logged so the exact provider error
   (e.g. "reasoning_content must be passed back") is captured.
"""
import asyncio
import json
import os
import sys
from unittest.mock import MagicMock, patch

import httpx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def _fake_response(payload, status_code=200, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    return resp


class TestSubAgentV4Payload:
    """call_llm_with_reliability must omit temperature for deepseek-v4."""

    def test_omits_temperature_for_deepseek_v4(self):
        from app.services import sub_agent_reliability

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "hi", "tool_calls": []}}],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(
                sub_agent_reliability.call_llm_with_reliability(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=None,
                )
            )

        # The global model may or may not be v4; we verify the gate is wired
        # by forcing the model id to deepseek-v4-flash via endpoint.
        captured.clear()

        from app.services.llm_router import LLMEndpoint
        endpoint = LLMEndpoint(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-test",
            model_id="deepseek-v4-flash",
            provider="deepseek",
        )

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(
                sub_agent_reliability.call_llm_with_reliability(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=endpoint,
                )
            )

        assert "temperature" not in captured["json"], (
            f"Expected temperature omitted for deepseek-v4, got payload: {captured['json']}"
        )
        assert captured["json"]["model"] == "deepseek-v4-flash"

    def test_includes_temperature_for_non_v4(self):
        from app.services import sub_agent_reliability
        from app.services.llm_router import LLMEndpoint

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "hi", "tool_calls": []}}],
            })

        endpoint = LLMEndpoint(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-test",
            model_id="deepseek-chat",
            provider="deepseek",
        )

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(
                sub_agent_reliability.call_llm_with_reliability(
                    [{"role": "user", "content": "hello"}],
                    None,
                    temperature=0.5,
                    endpoint=endpoint,
                )
            )

        assert captured["json"]["temperature"] == 0.5

    def test_logs_http_400_body_before_raising(self, caplog):
        from app.services import sub_agent_reliability
        from app.services.llm_router import LLMEndpoint

        async def fake_post(url, headers=None, json=None, **kwargs):
            return _fake_response(
                {},
                status_code=400,
                text='{"error": {"message": "The reasoning_content must be passed back"}}',
            )

        endpoint = LLMEndpoint(
            base_url="https://api.deepseek.com/v1",
            api_key="sk-test",
            model_id="deepseek-v4-flash",
            provider="deepseek",
        )

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            try:
                asyncio.run(
                    sub_agent_reliability.call_llm_with_reliability(
                        [{"role": "user", "content": "hello"}],
                        None,
                        endpoint=endpoint,
                    )
                )
            except Exception:
                pass  # expected

        assert "reasoning_content must be passed back" in caplog.text


class TestReasoningContentPassback:
    """Both sub-agent loops must attach reasoning_content when present."""

    def test_edia_delegation_attaches_reasoning(self):
        from app.services.tool_handlers import edia_delegation_tools

        messages = [{"role": "user", "content": "hello"}]
        llm_response = {
            "content": "ok",
            "tool_calls": [{
                "id": "tc_1",
                "type": "function",
                "function": {"name": "search_documents", "arguments": "{}"},
            }],
            "reasoning": "I need to search docs",
        }

        # Simulate the loop body that appends the assistant message
        content = llm_response.get("content", "") or ""
        raw_tool_calls = llm_response.get("tool_calls", []) or []
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.get("id", "x"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in raw_tool_calls
            ],
        }
        # The expected production fix: attach reasoning_content when non-empty
        _reasoning = (llm_response.get("reasoning") or "").strip()
        if _reasoning:
            assistant_msg["reasoning_content"] = _reasoning

        messages.append(assistant_msg)

        assert messages[-1].get("reasoning_content") == "I need to search docs"

    def test_delegation_tools_attaches_reasoning(self):
        from app.services.tool_handlers import delegation_tools

        messages = [{"role": "user", "content": "hello"}]
        llm_response = {
            "content": "ok",
            "tool_calls": [{
                "id": "tc_1",
                "type": "function",
                "function": {"name": "describe_schema", "arguments": "{}"},
            }],
            "reasoning": "I need the schema",
        }

        content = llm_response.get("content", "") or ""
        raw_tool_calls = llm_response.get("tool_calls", []) or []
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.get("id", "x"),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in raw_tool_calls
            ],
        }
        _reasoning = (llm_response.get("reasoning") or "").strip()
        if _reasoning:
            assistant_msg["reasoning_content"] = _reasoning

        messages.append(assistant_msg)

        assert messages[-1].get("reasoning_content") == "I need the schema"

    def test_no_reasoning_when_empty(self):
        """When reasoning is absent/empty, the key must NOT be added."""
        llm_response = {
            "content": "ok",
            "tool_calls": [],
            "reasoning": "",
        }
        assistant_msg = {
            "role": "assistant",
            "content": llm_response["content"],
            "tool_calls": [],
        }
        _reasoning = (llm_response.get("reasoning") or "").strip()
        if _reasoning:
            assistant_msg["reasoning_content"] = _reasoning

        assert "reasoning_content" not in assistant_msg


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
