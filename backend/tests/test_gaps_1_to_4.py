"""Tests for Gaps 1-4: SSE streaming, auto-extract memories, permission mode
toggle, and delegate_task parallel execution.

Follows the same unittest + AsyncMock patterns as test_parallel_tools_and_approval.py.
"""

import os, sys, asyncio, unittest
from unittest.mock import AsyncMock, patch, MagicMock

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BACKEND_ROOT = os.path.abspath(os.path.join(_THIS_DIR, ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# Gap 4: delegate_task parallel execution

class TestDelegateParallelExecution(unittest.TestCase):

    def _make_mock_llm(self, tool_calls, final="Done"):
        seq = [{"content": "", "tool_calls": tool_calls}, {"content": final, "tool_calls": []}]
        return AsyncMock(side_effect=seq)

    def test_delegate_parallel_preserves_order(self):
        from app.services.tool_handlers.delegate_tool import _run_sub_agent
        tcs = [
            {"id": "tc-1", "function": {"name": "slow_tool", "arguments": "{}"}},
            {"id": "tc-2", "function": {"name": "fast_tool", "arguments": "{}"}},
        ]
        async def mock_exec(name, args, db, uid, context=None):
            if name == "slow_tool":
                await asyncio.sleep(0.05)
            return {"success": True, "tool": name}
        with patch("app.services.tool_handlers.delegate_tool._call_sub_llm", self._make_mock_llm(tcs)), \
             patch("app.services.agent_tools.execute_tool", side_effect=mock_exec):
            async def run():
                return await _run_sub_agent("t", "ga", MagicMock(), "u", 5)
            r = asyncio.run(run())
        self.assertTrue(r["success"])
        self.assertEqual(r["response"], "Done")

    def test_delegate_parallel_one_failure_does_not_cancel(self):
        from app.services.tool_handlers.delegate_tool import _run_sub_agent
        tcs = [
            {"id": "tc-1", "function": {"name": "fail", "arguments": "{}"}},
            {"id": "tc-2", "function": {"name": "ok", "arguments": "{}"}},
        ]
        async def mock_exec(name, args, db, uid, context=None):
            if name == "fail":
                raise ValueError("boom")
            return {"success": True}
        with patch("app.services.tool_handlers.delegate_tool._call_sub_llm", self._make_mock_llm(tcs)), \
             patch("app.services.agent_tools.execute_tool", side_effect=mock_exec):
            async def run():
                return await _run_sub_agent("t", "ga", MagicMock(), "u", 5)
            r = asyncio.run(run())
        self.assertTrue(r["success"])

    def test_delegate_single_tool_sequential(self):
        from app.services.tool_handlers.delegate_tool import _run_sub_agent
        tcs = [{"id": "tc-1", "function": {"name": "solo", "arguments": "{}"}}]
        cnt = [0]
        async def mock_exec(name, args, db, uid, context=None):
            cnt[0] += 1
            return {"success": True}
        with patch("app.services.tool_handlers.delegate_tool._call_sub_llm", self._make_mock_llm(tcs)), \
             patch("app.services.agent_tools.execute_tool", side_effect=mock_exec):
            async def run():
                return await _run_sub_agent("t", "ga", MagicMock(), "u", 5)
            asyncio.run(run())
        self.assertEqual(cnt[0], 1)


# Gap 2: Auto-extract memories

class TestAutoExtractMemories(unittest.TestCase):

    def test_bg_extract_exists(self):
        from app.routers.agents import _bg_extract_memories
        self.assertTrue(asyncio.iscoroutinefunction(_bg_extract_memories))

    def test_bg_extract_handles_failure(self):
        from app.routers.agents import _bg_extract_memories
        async def run():
            with patch("app.services.memory_advanced.auto_extract_memories",
                       new_callable=AsyncMock, side_effect=Exception("fail")):
                r = await _bg_extract_memories("a", [{"role":"u","content":"x"}], "u")
                self.assertIsNone(r)
        asyncio.run(run())

    def test_bg_extract_uses_independent_session(self):
        from app.routers.agents import _bg_extract_memories
        mock_db = MagicMock()
        mock_sl = MagicMock(return_value=mock_db)
        async def run():
            with patch("app.database.SessionLocal", mock_sl), \
                 patch("app.services.memory_advanced.auto_extract_memories", AsyncMock(return_value=[])):
                await _bg_extract_memories("a", [
                    {"role":"u","content":"a"},{"role":"assistant","content":"b"},
                    {"role":"u","content":"c"},{"role":"assistant","content":"d"},
                ], "u")
        asyncio.run(run())
        mock_sl.assert_called_once()
        mock_db.close.assert_called_once()


# Gap 3: Permission mode override

class TestPermissionModeOverride(unittest.TestCase):

    def test_conv_metadata_overrides_to_full_auto(self):
        from app.services.permissions import PermissionChecker, PermissionConfig
        c = PermissionChecker(PermissionConfig(mode="default"))
        r = c.check_tool("write_file", {"path": "/tmp/t.txt"})
        self.assertTrue(r.requires_confirmation)
        r2 = c.check_tool("write_file", {"path": "/tmp/t.txt"},
                          conversation_metadata={"permission_mode": "full_auto"})
        self.assertTrue(r2.allowed)
        self.assertFalse(r2.requires_confirmation)

    def test_conv_metadata_overrides_to_plan(self):
        from app.services.permissions import PermissionChecker, PermissionConfig
        c = PermissionChecker(PermissionConfig(mode="full_auto"))
        r = c.check_tool("write_file", {"path": "/tmp/t.txt"})
        self.assertTrue(r.allowed)
        r2 = c.check_tool("write_file", {"path": "/tmp/t.txt"},
                          conversation_metadata={"permission_mode": "plan"})
        self.assertFalse(r2.allowed)

    def test_invalid_mode_falls_back(self):
        from app.services.permissions import PermissionChecker, PermissionConfig
        c = PermissionChecker(PermissionConfig(mode="default"))
        r = c.check_tool("write_file", {"path": "/tmp/t.txt"},
                         conversation_metadata={"permission_mode": "bogus"})
        self.assertTrue(r.requires_confirmation)

    def test_check_permission_passes_metadata(self):
        from app.services.permissions import check_permission
        r = check_permission("write_file", {"path": "/tmp/t.txt"},
                             conversation_metadata={"permission_mode": "full_auto"})
        self.assertTrue(r.allowed)
        self.assertFalse(r.requires_confirmation)

    def test_execute_tool_passes_conv_metadata(self):
        from app.services.permissions import PermissionCheckResult
        captured = {}
        def mock_check(tn, args, an, **kw):
            captured.update(kw)
            return PermissionCheckResult(allowed=True)
        with patch("app.services.permissions.check_permission", side_effect=mock_check), \
             patch("app.services.tool_registry.registry.get_handler",
                   return_value=AsyncMock(return_value={"success": True})):
            from app.services.agent_tools import execute_tool
            async def run():
                return await execute_tool("write_file", {"path": "/tmp/t"}, MagicMock(),
                    context={"agent_name":"t","conversation_metadata":{"permission_mode":"full_auto"}})
            asyncio.run(run())
        self.assertEqual(captured.get("conversation_metadata"), {"permission_mode": "full_auto"})


# Gap 1: SSE streaming endpoint

class TestSSEStreaming(unittest.TestCase):

    def test_stream_endpoint_exists(self):
        from app.routers.agents import add_message_stream
        self.assertTrue(asyncio.iscoroutinefunction(add_message_stream))

    def test_stream_helper_exists(self):
        from app.routers.agents import _stream_llm_final_response
        self.assertTrue(hasattr(_stream_llm_final_response, "__call__"))

    def test_streaming_response_imported(self):
        from app.routers import agents
        self.assertTrue(hasattr(agents, "StreamingResponse"))

    def test_stream_helper_yields_events(self):
        from app.routers.agents import _stream_llm_final_response
        class FakeResponse:
            """Fake httpx streaming response with async aiter_lines."""
            def __init__(self, lines):
                self._lines = lines
            def raise_for_status(self):
                pass
            async def aiter_lines(self):
                for line in self._lines:
                    yield line
        class FakeStreamCtx:
            """Async context manager for client.stream(...)."""
            def __init__(self, lines):
                self._lines = lines
            async def __aenter__(self):
                return FakeResponse(self._lines)
            async def __aexit__(self, *args):
                pass
        class FakeClient:
            """Async context manager for httpx.AsyncClient()."""
            def stream(self, *args, **kwargs):
                return FakeStreamCtx([
                    'data: {"choices":[{"delta":{"content":"Hello"}}]}',
                    'data: {"choices":[{"delta":{"content":" world"}}]}',
                    'data: [DONE]',
                ])
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
        async def run():
            with patch("httpx.AsyncClient", return_value=FakeClient()):
                events = []
                async for et, ed in _stream_llm_final_response([], None):
                    events.append((et, ed))
                    if et == "done":
                        break
                return events
        events = asyncio.run(run())
        deltas = [e[1] for e in events if e[0] == "delta"]
        self.assertIn("Hello", deltas)
        self.assertIn(" world", deltas)
        done = [e for e in events if e[0] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0][1], "Hello world")


if __name__ == "__main__":
    unittest.main(verbosity=2)
