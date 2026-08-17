"""Tests for LLMEndpoint threading in _call_synthesis_llm.

Verify that:
1. When an ``endpoint`` is provided, the POST targets the endpoint's
   base_url + model_id + api_key (NOT global settings).
2. When ``endpoint`` is None, it falls back to the legacy globals
   (get_model() / llm_url() / llm_headers()).
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


class TestCallSynthesisLLMEndpoint(unittest.TestCase):
    """Verify _call_synthesis_llm honors the endpoint kwarg."""

    def test_endpoint_overrides_globals(self):
        from app.routers import agents
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
                "choices": [{"message": {"content": "synth"}}],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                agents._call_synthesis_llm(
                    "system", [{"role": "user", "content": "hi"}], endpoint=endpoint
                )
            )

        self.assertEqual(result["content"], "synth")
        self.assertIn("api.moonshot.cn", captured["url"])
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer kimi-secret-key")
        self.assertEqual(captured["json"]["model"], "kimi-k2.6")

    def test_endpoint_none_falls_back_to_globals(self):
        from app.routers import agents
        from app.services.llm_service import get_model, llm_url, llm_headers

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({
                "choices": [{"message": {"content": "global"}}],
            })

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                agents._call_synthesis_llm(
                    "system", [{"role": "user", "content": "hi"}], endpoint=None
                )
            )

        self.assertEqual(result["content"], "global")
        self.assertEqual(captured["url"], llm_url())
        self.assertEqual(captured["headers"], llm_headers())
        self.assertEqual(captured["json"]["model"], get_model())


if __name__ == "__main__":
    unittest.main()
