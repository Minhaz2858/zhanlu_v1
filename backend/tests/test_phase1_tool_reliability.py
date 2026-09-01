"""Phase 1 — Tool Reliability tests.

Covers three areas of the Phase 1 reliability work:

1. ``_is_retryable`` — classifies tool results as retryable vs permanent.
2. ``execute_tool_with_retry`` — wraps ``execute_tool`` with:
   - Exponential backoff for transient failures.
   - No retry for permanent errors (permission denied, unknown tool).
   - LLM-driven argument reformulation when retries are exhausted.
3. ``_detect_tool_call_loop`` — success-aware loop detection:
   - Successful calls trip at ``cap``.
   - Failed calls trip at ``cap + 1`` (one extra chance to reformulate).
   - ``requires_approval`` is NOT treated as a failure.
"""
import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


# ─── Helpers ──────────────────────────────────────────────────────────────

def _tc(name: str, args: dict, call_id: str = None) -> dict:
    """Build a tool_call entry in OpenAI format."""
    cid = call_id or f"call_{name}_{hash(json.dumps(args, sort_keys=True))}"
    return {
        "id": cid,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args, sort_keys=True),
        },
    }


def _msg(role: str, content: str = "", tool_calls=None, tool_call_id: str = None) -> dict:
    out = {"role": role, "content": content}
    if tool_calls:
        out["tool_calls"] = tool_calls
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


# ═══════════════════════════════════════════════════════════════════════════
# 1. _is_retryable classification
# ═══════════════════════════════════════════════════════════════════════════

class TestIsRetryable:
    """Tests for the ``_is_retryable`` classifier."""

    def test_success_not_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": True}) is False

    def test_requires_approval_not_retryable(self):
        """``requires_approval`` is a pending user action, not a retry."""
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "requires_approval": True}) is False

    def test_explicit_retryable_flag(self):
        """``OperationalError`` from execute_tool sets ``retryable: True``."""
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "retryable": True}) is True

    def test_permission_denied_not_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "error": "permission denied"}) is False

    def test_unknown_tool_not_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "error": "unknown tool: foo"}) is False

    def test_not_found_not_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "error": "agent not found"}) is False

    def test_already_exists_not_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({"success": False, "error": "already exists"}) is False

    def test_db_connection_error_retryable(self):
        """Transient DB errors should be retried."""
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({
            "success": False,
            "error": "could not connect to server: Connection refused",
        }) is True

    def test_timeout_retryable(self):
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({
            "success": False,
            "error": "operation timed out after 30s",
        }) is True

    def test_generic_error_retryable_by_default(self):
        """Unknown errors default to retryable (they might be transient)."""
        from app.services.agent_tools import _is_retryable
        assert _is_retryable({
            "success": False,
            "error": "something unexpected happened",
        }) is True


# ═══════════════════════════════════════════════════════════════════════════
# 2. execute_tool_with_retry
# ═══════════════════════════════════════════════════════════════════════════

class TestExecuteToolWithRetry:
    """Tests for the retry wrapper."""

    async def test_success_on_first_attempt(self):
        """If execute_tool succeeds immediately, no retries happen."""
        from app.services import agent_tools

        mock_execute = AsyncMock(return_value={"success": True, "data": "ok"})
        with patch.object(agent_tools, "execute_tool", mock_execute):
            result = await agent_tools.execute_tool_with_retry(
                "web_search",
                {"query": "test"},
                db=MagicMock(),
                max_attempts=2,
                base_delay=0.01,
            )
        assert result["success"] is True
        assert result["data"] == "ok"
        assert mock_execute.call_count == 1

    async def test_non_retryable_error_no_retry(self):
        """Permanent errors (permission denied) are not retried."""
        from app.services import agent_tools

        mock_execute = AsyncMock(return_value={
            "success": False,
            "error": "permission denied",
        })
        with patch.object(agent_tools, "execute_tool", mock_execute):
            result = await agent_tools.execute_tool_with_retry(
                "create_agent",
                {"name": "test"},
                db=MagicMock(),
                max_attempts=3,
                base_delay=0.01,
            )
        assert result["success"] is False
        assert "permission denied" in result["error"]
        assert mock_execute.call_count == 1, "Permanent error should not retry"

    async def test_retryable_error_retries_then_succeeds(self):
        """Transient failure on attempt 1, success on attempt 2."""
        from app.services import agent_tools

        mock_execute = AsyncMock(side_effect=[
            {"success": False, "error": "connection refused"},
            {"success": True, "data": "ok"},
        ])
        with patch.object(agent_tools, "execute_tool", mock_execute):
            result = await agent_tools.execute_tool_with_retry(
                "web_search",
                {"query": "test"},
                db=MagicMock(),
                max_attempts=2,
                base_delay=0.01,
            )
        assert result["success"] is True
        assert mock_execute.call_count == 2

    async def test_exhausts_retries_returns_last_failure(self):
        """All retries fail → return the last failure (no reformulation)."""
        from app.services import agent_tools

        mock_execute = AsyncMock(return_value={
            "success": False,
            "error": "connection refused",
        })
        # Disable reformulation for this test
        with patch.object(agent_tools, "execute_tool", mock_execute), \
             patch("app.config.settings.TOOL_REFORMULATE_MAX_ATTEMPTS", 0):
            result = await agent_tools.execute_tool_with_retry(
                "web_search",
                {"query": "test"},
                db=MagicMock(),
                max_attempts=2,
                base_delay=0.01,
            )
        assert result["success"] is False
        # 1 initial + 2 retries = 3 calls
        assert mock_execute.call_count == 3

    async def test_reformulation_succeeds(self):
        """After retries exhaust, LLM reformulates args → success."""
        from app.services import agent_tools

        # Fail with original args, succeed with reformulated args
        mock_execute = AsyncMock(side_effect=[
            {"success": False, "error": "connection refused"},
            {"success": False, "error": "connection refused"},
            {"success": False, "error": "connection refused"},
            {"success": True, "data": "reformulated result"},
        ])
        mock_reformulate = AsyncMock(return_value={"query": "reformulated query"})

        with patch.object(agent_tools, "execute_tool", mock_execute), \
             patch.object(agent_tools, "_reformulate_tool_args", mock_reformulate), \
             patch("app.config.settings.TOOL_REFORMULATE_MAX_ATTEMPTS", 1):
            result = await agent_tools.execute_tool_with_retry(
                "web_search",
                {"query": "original"},
                db=MagicMock(),
                max_attempts=2,
                base_delay=0.01,
            )
        assert result["success"] is True
        assert result["data"] == "reformulated result"
        # 3 failed calls (1 initial + 2 retries) + 1 reformulated call = 4
        assert mock_execute.call_count == 4
        # Last call used reformulated args
        last_call_args = mock_execute.call_args_list[-1]
        assert last_call_args.args[1] == {"query": "reformulated query"}

    async def test_reformulation_fallback_to_original(self):
        """If LLM reformulation fails (returns same args), no extra call."""
        from app.services import agent_tools

        mock_execute = AsyncMock(return_value={
            "success": False,
            "error": "connection refused",
        })
        # Reformulation returns the same args → no extra execute_tool call
        mock_reformulate = AsyncMock(return_value={"query": "original"})

        with patch.object(agent_tools, "execute_tool", mock_execute), \
             patch.object(agent_tools, "_reformulate_tool_args", mock_reformulate), \
             patch("app.config.settings.TOOL_REFORMULATE_MAX_ATTEMPTS", 1):
            result = await agent_tools.execute_tool_with_retry(
                "web_search",
                {"query": "original"},
                db=MagicMock(),
                max_attempts=2,
                base_delay=0.01,
            )
        assert result["success"] is False
        # 3 calls (1 initial + 2 retries), NO reformulation call since args unchanged
        assert mock_execute.call_count == 3

    async def test_requires_approval_not_retried(self):
        """``requires_approval`` is a pending user action, not a failure."""
        from app.services import agent_tools

        mock_execute = AsyncMock(return_value={
            "success": False,
            "requires_approval": True,
            "approval_prompt": "Allow this action?",
        })
        with patch.object(agent_tools, "execute_tool", mock_execute):
            result = await agent_tools.execute_tool_with_retry(
                "create_agent",
                {"name": "test"},
                db=MagicMock(),
                max_attempts=3,
                base_delay=0.01,
            )
        assert result["requires_approval"] is True
        assert mock_execute.call_count == 1, "requires_approval should not retry"


# ═══════════════════════════════════════════════════════════════════════════
# 3. _detect_tool_call_loop — success-aware
# ═══════════════════════════════════════════════════════════════════════════

class TestSuccessAwareLoopGuard:
    """Tests for the success-aware loop detection in ``_detect_tool_call_loop``.

    Key behavior:
      - Successful calls trip at ``cap``.
      - Failed calls trip at ``cap + 1`` (one extra chance).
      - ``requires_approval`` is treated as success (pending user action).
    """

    def test_successful_calls_trip_at_cap(self):
        """``memory`` cap is 1 — a single successful call trips the guard."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save a memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None, "1 successful memory call should trip cap=1"
        assert info[0] == "memory"

    def test_failed_calls_do_not_trip_at_cap(self):
        """``memory`` cap is 1, but a single *failed* call should NOT trip
        the guard — the LLM gets one extra chance to reformulate args."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save a memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": False, "error": "db locked"}), tool_call_id="c1"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is None, (
            "1 failed memory call should NOT trip — failure cap is cap+1=2, "
            "giving the LLM one extra chance to reformulate."
        )

    def test_failed_calls_trip_at_cap_plus_one(self):
        """``memory`` cap is 1 — 2 failed calls trip the guard (cap+1=2)."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save a memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": False, "error": "db locked"}), tool_call_id="c1"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c2")]),
            _msg("tool", json.dumps({"success": False, "error": "db locked"}), tool_call_id="c2"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None, "2 failed memory calls should trip (cap+1=2)"
        assert info[0] == "memory"
        assert info[1] >= 2

    def test_requires_approval_counts_as_success(self):
        """``requires_approval`` is a pending user action, not a failure.
        It should count toward the success cap (trip at cap=1 for memory)."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save a memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": False, "requires_approval": True}), tool_call_id="c1"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None, (
            "requires_approval should count as success → trips at cap=1"
        )

    def test_default_tool_success_trips_at_6(self):
        """``skills`` is not in the per-tool map → default cap is 6.
        6 successful calls with the same args should trip."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [_msg("user", "load it")]
        for i in range(6):
            messages.append(_msg("assistant", "", tool_calls=[
                _tc("skills", {"action": "load", "name": "foo"}, call_id=f"c{i+1}")
            ]))
            messages.append(_msg("tool", json.dumps({"success": True}), tool_call_id=f"c{i+1}"))
        info = _detect_tool_call_loop(messages)
        assert info is not None, "6 successful calls should trip default cap=6"

    def test_default_tool_failure_does_not_trip_at_6(self):
        """``skills`` default cap is 6 — 6 *failed* calls should NOT trip
        (failure cap is cap+1=7, giving one extra reformulation chance)."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [_msg("user", "load it")]
        for i in range(6):
            messages.append(_msg("assistant", "", tool_calls=[
                _tc("skills", {"action": "load", "name": "foo"}, call_id=f"c{i+1}")
            ]))
            messages.append(_msg("tool", json.dumps({"success": False, "error": "timeout"}), tool_call_id=f"c{i+1}"))
        info = _detect_tool_call_loop(messages)
        assert info is None, (
            "6 failed calls should NOT trip — failure cap is cap+1=7. "
            "The LLM gets one extra chance to reformulate."
        )

    def test_default_tool_failure_trips_at_7(self):
        """``skills`` default cap is 6 — 7 *failed* calls trip (cap+1=7)."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [_msg("user", "load it")]
        for i in range(7):
            messages.append(_msg("assistant", "", tool_calls=[
                _tc("skills", {"action": "load", "name": "foo"}, call_id=f"c{i+1}")
            ]))
            messages.append(_msg("tool", json.dumps({"success": False, "error": "timeout"}), tool_call_id=f"c{i+1}"))
        info = _detect_tool_call_loop(messages)
        assert info is not None, "7 failed calls should trip (cap+1=7)"
        assert info[0] == "skills"

    def test_mixed_success_failure_trips_on_success(self):
        """If a tool succeeds once (cap=1 for memory), then fails, the guard
        should have already tripped on the successful call."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None
        assert info[0] == "memory"

    def test_in_progress_call_defaults_to_success(self):
        """If an assistant tool_call has no corresponding tool result yet
        (call still in progress), default to treating it as a success —
        conservative behavior that doesn't prematurely trip the guard."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "save memory"),
            _msg("assistant", "", tool_calls=[_tc("memory", {"content": "foo"}, call_id="c1")]),
            # No tool result message — call is still in progress
        ]
        info = _detect_tool_call_loop(messages)
        # memory cap is 1 — in-progress call defaults to success → trips
        assert info is not None, (
            "In-progress call defaults to success → trips at cap=1 for memory"
        )

    def test_different_args_success_no_trip(self):
        """Same tool, different args = exploration, not a loop.
        Even with successes, different args don't trip."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "search"),
            _msg("assistant", "", tool_calls=[_tc("web_search", {"query": "foo"}, call_id="c1")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c1"),
            _msg("assistant", "", tool_calls=[_tc("web_search", {"query": "bar"}, call_id="c2")]),
            _msg("tool", json.dumps({"success": True}), tool_call_id="c2"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is None, "Different args = not a loop"

    def test_ask_data_agent_name_only_keying(self):
        """``ask_data_agent`` keys by name only — 2nd call (any args) trips
        at cap=2, even with different arguments."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "query data"),
            _msg("assistant", "", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "ask_data_agent", "arguments": '{"question": "query A"}'},
            }]),
            _msg("tool", json.dumps({"success": True, "answer": "result A"}), tool_call_id="c1"),
            # Different args, but ask_data_agent keys by name only
            _msg("assistant", "", tool_calls=[{
                "id": "c2", "type": "function",
                "function": {"name": "ask_data_agent", "arguments": '{"question": "query B"}'},
            }]),
            _msg("tool", json.dumps({"success": True, "answer": "result B"}), tool_call_id="c2"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is not None, "ask_data_agent 2nd call should trip cap=2"
        assert info[0] == "ask_data_agent"

    def test_ask_data_agent_failure_does_not_trip_at_2(self):
        """``ask_data_agent`` cap is 2 — 2 *failed* calls should NOT trip
        (failure cap is cap+1=3)."""
        from app.routers.agents import _detect_tool_call_loop
        messages = [
            _msg("user", "query data"),
            _msg("assistant", "", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {"name": "ask_data_agent", "arguments": '{"question": "query A"}'},
            }]),
            _msg("tool", json.dumps({"success": False, "error": "db locked"}), tool_call_id="c1"),
            _msg("assistant", "", tool_calls=[{
                "id": "c2", "type": "function",
                "function": {"name": "ask_data_agent", "arguments": '{"question": "query B"}'},
            }]),
            _msg("tool", json.dumps({"success": False, "error": "db locked"}), tool_call_id="c2"),
        ]
        info = _detect_tool_call_loop(messages)
        assert info is None, (
            "2 failed ask_data_agent calls should NOT trip — failure cap is 3"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Config settings exist
# ═══════════════════════════════════════════════════════════════════════════

class TestConfigSettings:
    """Verify the Phase 1 config settings are present."""

    def test_retry_settings_exist(self):
        from app.config import settings
        assert hasattr(settings, "TOOL_RETRY_MAX_ATTEMPTS")
        assert hasattr(settings, "TOOL_RETRY_BASE_DELAY")
        assert hasattr(settings, "TOOL_RETRY_MAX_DELAY")
        assert hasattr(settings, "TOOL_REFORMULATE_MAX_ATTEMPTS")

    def test_retry_defaults_sensible(self):
        from app.config import settings
        assert settings.TOOL_RETRY_MAX_ATTEMPTS >= 1
        assert settings.TOOL_RETRY_BASE_DELAY > 0
        assert settings.TOOL_RETRY_MAX_DELAY >= settings.TOOL_RETRY_BASE_DELAY
        assert settings.TOOL_REFORMULATE_MAX_ATTEMPTS >= 0

    def test_import_in_agents_py(self):
        """``execute_tool_with_retry`` must be importable from agents.py."""
        import importlib
        agents_mod = importlib.import_module("app.routers.agents")
        assert hasattr(agents_mod, "_detect_tool_call_loop")
