"""Tests for report_ai_service.py: SSE streaming, cache, worker fan-in."""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.report_ai_service import (
    stream_ai_analysis,
    build_ai_context,
    _sse,
    _ai_cache,
    _ai_cache_lock,
)


def mock_execution_chart():
    return [
        {"product": "C5树脂", "contract_qty": 500.0, "out_qty": 380.0, "execution_rate": 76.0},
        {"product": "间戊二烯", "contract_qty": 200.0, "out_qty": 190.0, "execution_rate": 95.0},
    ]


def mock_unshipped():
    return [
        {
            "product": "C5树脂",
            "customer": "青岛橡塑",
            "contract_qty": 300,
            "unshipped_qty": 120,
            "delivery_date": "2025-08-15",
            "bill_no": "B1",
            "product_type": "resin",
        }
    ]


class TestBuildAiContext:
    def test_includes_product_data(self):
        ctx = build_ai_context(
            mock_execution_chart(),
            mock_unshipped(),
            "惠州伊斯科",
            "2025-07-01",
            "2025-07-31",
        )
        assert "惠州伊斯科" in ctx
        assert "C5树脂" in ctx
        assert "间戊二烯" in ctx
        assert "76" in ctx or "76.0" in ctx
        assert "未出库" in ctx

    def test_empty_input_produces_header(self):
        ctx = build_ai_context([], [], "惠州伊斯科", "2025-01-01", "2025-01-31")
        assert "惠州伊斯科" in ctx
        assert "2025-01-01" in ctx


class TestSseHelper:
    def test_sse_format(self):
        frame = _sse("meta", {"status": "started"})
        decoded = frame.decode("utf-8")
        assert decoded.startswith("event: meta\n")
        assert '"status": "started"' in decoded

    def test_sse_string_data(self):
        frame = _sse("delta", '{"text": "hello"}')
        decoded = frame.decode("utf-8")
        assert decoded.startswith("event: delta\n")
        assert "hello" in decoded


class TestStreamAiAnalysis:
    @pytest.mark.asyncio
    async def test_cache_hit_fast_path(self):
        """If a valid cached result exists, emit meta(cached) + done."""
        cache_key = ("huizhou", "2025-07-01", "2025-07-31", "")
        with _ai_cache_lock:
            _ai_cache[cache_key] = {
                "data": {
                    "chart_top": "cached_top",
                    "chart_bottom": "",
                    "summary_paragraph": "",
                    "summary_bullets": [],
                    "unshipped_analysis": "",
                },
                "ts": __import__("time").time(),
            }

        org = {"key": "huizhou", "label": "惠州伊斯科"}
        events: list[str] = []
        async for frame in stream_ai_analysis(
            mock_execution_chart(), mock_unshipped(),
            org, "2025-07-01", "2025-07-31",
        ):
            events.append(frame.decode("utf-8"))

        # Clean up
        with _ai_cache_lock:
            _ai_cache.pop(cache_key, None)

        assert any("event: meta" in e for e in events)
        assert any("cached" in e for e in events)
        done_events = [e for e in events if "event: done" in e]
        assert len(done_events) == 1
        assert "cached_top" in done_events[0]

    @pytest.mark.asyncio
    async def test_streaming_yields_events(self):
        """With mocked LLM, verify meta→delta→done event sequence."""
        # Remove any cached entry first
        cache_key = ("huizhou", "2025-08-01", "2025-08-31", "")
        with _ai_cache_lock:
            _ai_cache.pop(cache_key, None)

        org = {"key": "huizhou", "label": "惠州伊斯科"}

        # async generator that yields some deltas
        async def mock_gen(prompt, temperature=0.7):
            yield "分析："
            yield "合同执行良好。"
            yield "  "
            yield "建议重点关注。"

        loop = asyncio.get_event_loop()
        events: list[bytes] = []

        # Collect first few events (timeout to avoid hanging on slow mock LLM)
        async def collect():
            async for frame in stream_ai_analysis(
                mock_execution_chart(), mock_unshipped(),
                org, "2025-08-01", "2025-08-31",
            ):
                events.append(frame)
                # Stop after done
                if frame.startswith(b"event: done\n"):
                    break

        with patch(
            "app.services.report_ai_service.stream_chat_completion",
            mock_gen,
        ):
            await asyncio.wait_for(collect(), timeout=60.0)

        # Should get meta + deltas + done
        event_types = set()
        for frame in events:
            decoded = frame.decode("utf-8")
            for line in decoded.split("\n"):
                if line.startswith("event: "):
                    event_types.add(line.split("event: ")[1])

        assert "meta" in event_types
        assert "delta" in event_types or sum(1 for e in events if b"delta" in e) > 0
        assert "done" in event_types


class TestSseContract:
    """Verify SSE frame format matches EDIA protocol contract."""

    def test_frame_ends_with_double_newline(self):
        frame = _sse("done", {"chart_top": "ok"})
        assert frame.endswith(b"\n\n")

    def test_frame_utf8(self):
        frame = _sse("delta", {"field": "chart_top", "text": "合同执行情况良好"})
        decoded = frame.decode("utf-8")
        assert "合同执行情况良好" in decoded
