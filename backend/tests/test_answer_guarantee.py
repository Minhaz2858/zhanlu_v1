"""Tests for Phase 2 — Answer Guarantee.

- Synthesis LLM gets enough output tokens + one retry on failure.
- Data-rows fallback renders a clean markdown table, never raw sample values.
- Degenerate-dataset guard catches all-zero measure columns so broken
  data doesn't become a "Zero-Revenue" card.
"""
import asyncio
import os
import sys
from unittest.mock import MagicMock, patch

import httpx

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


class TestSynthesisMaxTokensAndRetry:
    """_call_synthesis_llm must send max_tokens and retry once on failure."""

    def test_payload_includes_max_tokens(self):
        from app.routers import agents

        captured = {}

        async def fake_post(url, headers=None, json=None, **kwargs):
            captured["json"] = json
            return MagicMock(
                status_code=200,
                text="ok",
                raise_for_status=MagicMock(),
                json=lambda: {
                    "choices": [{"message": {"content": "report here"}}],
                },
            )

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                agents._call_synthesis_llm(
                    system_prompt="sys",
                    messages=[{"role": "user", "content": "hello"}],
                )
            )

        assert "max_tokens" in captured["json"]
        assert captured["json"]["max_tokens"] > 1536
        assert result["content"] == "report here"

    def test_retries_once_with_trimmed_rows_on_http_error(self):
        from app.routers import agents

        attempts = []

        async def fake_post(url, headers=None, json=None, **kwargs):
            attempts.append(json)
            if len(attempts) == 1:
                # First call fails
                resp = MagicMock(
                    status_code=500,
                    text="server error",
                    raise_for_status=MagicMock(
                        side_effect=httpx.HTTPStatusError(
                            "500",
                            request=MagicMock(),
                            response=MagicMock(status_code=500, text="server error"),
                        )
                    ),
                )
                return resp
            # Second call succeeds
            return MagicMock(
                status_code=200,
                text="ok",
                raise_for_status=MagicMock(),
                json=lambda: {
                    "choices": [{"message": {"content": "retry report"}}],
                },
            )

        with patch.object(httpx.AsyncClient, "post", side_effect=fake_post):
            result = asyncio.run(
                agents._call_synthesis_llm(
                    system_prompt="sys",
                    messages=[{"role": "user", "content": "hello"}],
                )
            )

        assert len(attempts) == 2
        assert result["content"] == "retry report"


class TestDataRowsFallback:
    """_data_rows_fallback must render markdown tables, not raw sample values."""

    def test_renders_markdown_table(self):
        from app.routers import agents

        tool_calls = [
            {
                "name": "ask_data_agent",
                "results": {
                    "source_name": "erp",
                    "rows": [
                        {"material": "sample product", "qty": 100, "revenue": 5000},
                        {"material": "sample product", "qty": 200, "revenue": 8000},
                    ],
                },
            }
        ]
        result = agents._data_rows_fallback(tool_calls)
        assert "| material | qty | revenue |" in result
        assert "Sample values" not in result
        assert "| sample product | 100 | 5000 |" in result

    def test_hides_system_columns(self):
        from app.routers import agents

        tool_calls = [
            {
                "name": "ask_data_agent",
                "results": {
                    "source_name": "erp",
                    "rows": [
                        {"FENTRYID": 100, "material": "sample product", "FCUSTMATID": "", "qty": 100},
                    ],
                },
            }
        ]
        result = agents._data_rows_fallback(tool_calls)
        assert "FENTRYID" not in result
        assert "FCUSTMATID" not in result
        assert "material" in result
        assert "qty" in result

    def test_hides_all_empty_columns(self):
        from app.routers import agents

        tool_calls = [
            {
                "name": "ask_data_agent",
                "results": {
                    "source_name": "erp",
                    "rows": [
                        {"material": "sample product", "empty_col": "", "qty": 100},
                        {"material": "sample product", "empty_col": None, "qty": 200},
                    ],
                },
            }
        ]
        result = agents._data_rows_fallback(tool_calls)
        assert "empty_col" not in result
        assert "material" in result
        assert "qty" in result


# 2026-10-10: TestDegenerateDatasetGuard removed with agents._is_degenerate_dataset
# itself — it judged data by a hardcoded English/finance noun regex and had zero
# production call sites (dead, fail-closed for other domains). Degenerate-result
# detection now lives in synexia/verifier._check_degenerate_result (structural:
# all-NULL / 0 rows / metadata-only), covered by test_goal_based_answer_delivery.


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
