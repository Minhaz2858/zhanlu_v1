"""Tests for LLMEndpoint threading in NLAnswerService fast path.

Verify that:
1. ``_chat`` targets the endpoint's base_url + api_key + model_id when an
   endpoint is provided.
2. ``_chat`` falls back to globals when endpoint is None.
3. ``NLAnswerService.answer`` threads the endpoint down to ``_chat``.
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


class TestChatEndpoint(unittest.TestCase):
    def test_chat_uses_endpoint(self):
        from app.services.db import nl_answer_service
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
            return _fake_response({"choices": [{"message": {"content": "hi"}}]})

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(nl_answer_service._chat(
                [{"role": "user", "content": "q"}], temperature=0.0, endpoint=endpoint
            ))

        self.assertEqual(result, "hi")
        self.assertIn("api.moonshot.cn", captured["url"])
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["headers"]["Authorization"], "Bearer kimi-secret-key")
        self.assertEqual(captured["json"]["model"], "kimi-k2.6")

    def test_chat_none_falls_back_to_globals(self):
        from app.services.db import nl_answer_service
        from app.services.llm_service import get_model, llm_url, llm_headers

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _fake_response({"choices": [{"message": {"content": "g"}}]})

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            asyncio.run(nl_answer_service._chat(
                [{"role": "user", "content": "q"}], endpoint=None
            ))

        self.assertEqual(captured["url"], llm_url())
        self.assertEqual(captured["headers"], llm_headers())
        self.assertEqual(captured["json"]["model"], get_model())


class TestAnswerThreadsEndpoint(unittest.TestCase):
    def test_answer_forwards_endpoint_to_chat(self):
        from app.services.db import nl_answer_service
        from app.services.llm_router import LLMEndpoint

        endpoint = LLMEndpoint(
            base_url="https://api.moonshot.cn/v1",
            api_key="kimi-secret-key",
            model_id="kimi-k2.6",
            provider="moonshot",
        )

        seen = {}

        async def fake_chat(messages, temperature=0.0, endpoint=None):
            seen["endpoint"] = endpoint
            return "SELECT 1"

        svc = nl_answer_service.NLAnswerService(db=MagicMock())
        with patch.object(nl_answer_service, "_chat", side_effect=fake_chat):
            asyncio.run(svc._text_to_sql(
                "q",
                {"source": {"id": "kb1", "name": "kb", "db_type": "postgresql"}, "tables": []},
                endpoint=endpoint,
            ))

        self.assertIs(seen["endpoint"], endpoint)


if __name__ == "__main__":
    unittest.main()
