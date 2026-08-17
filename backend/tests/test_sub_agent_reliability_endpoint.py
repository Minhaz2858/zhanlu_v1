"""Tests for LLMEndpoint threading in call_llm_with_reliability.

Verify that:
1. When an ``endpoint`` is provided, the POST targets the endpoint's
   base_url + model_id + api_key (NOT global settings).
2. When ``endpoint`` is None, it falls back to the legacy globals
   (get_model() / llm_url() / llm_headers()).
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestCallLLMWithReliabilityEndpoint(unittest.TestCase):
    """Verify call_llm_with_reliability honors the endpoint kwarg."""

    def test_endpoint_overrides_globals(self):
        from app.services import sub_agent_reliability
        from app.services.llm_router import LLMEndpoint

        endpoint = LLMEndpoint(
            base_url="https://api.moonshot.cn/v1",
            api_key="kimi-secret-key",
            model_id="kimi-k2.6",
            provider="moonshot",
        )

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({
                "choices": [{
                    "message": {"content": "hi", "tool_calls": []},
                }],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                sub_agent_reliability.call_llm_with_reliability(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=endpoint,
                )
            )

        self.assertEqual(result["content"], "hi")
        self.assertIn("api.moonshot.cn", captured["url"])
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer kimi-secret-key")
        self.assertEqual(captured["json"]["model"], "kimi-k2.6")

    def test_endpoint_none_falls_back_to_globals(self):
        from app.services import sub_agent_reliability
        from app.services.llm_service import get_model, llm_url, llm_headers

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({
                "choices": [{
                    "message": {"content": "global", "tool_calls": []},
                }],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                sub_agent_reliability.call_llm_with_reliability(
                    [{"role": "user", "content": "hello"}],
                    None,
                    endpoint=None,
                )
            )

        self.assertEqual(result["content"], "global")
        self.assertEqual(captured["url"], llm_url())
        self.assertEqual(captured["headers"], llm_headers())
        self.assertEqual(captured["json"]["model"], get_model())


if __name__ == "__main__":
    unittest.main()
