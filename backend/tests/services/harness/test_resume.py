"""Unit tests for resume_run crash recovery."""

import asyncio
import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestAgentRunResume:

    def test_resume_missing_run_returns_none(self):
        from app.services.harness.run_service import AgentRunService
        svc = AgentRunService()
        with patch.object(svc, "list_runs", return_value=[]):
            result = svc.resume_run("no-such-run")
        assert result is None

    def test_resume_without_steps_returns_none(self):
        from app.services.harness.run_service import AgentRunService
        svc = AgentRunService()
        mock_run = MagicMock(status="crashed", run_id="r1",
                             agent_name="test", task="hello")
        with (
            patch.object(svc, "list_runs", return_value=[mock_run]),
            patch.object(svc, "get_last_step", return_value=None),
        ):
            result = svc.resume_run("r1")
        assert result is None

    def test_resume_corrupted_snapshot_returns_none(self):
        from app.services.harness.run_service import AgentRunService
        svc = AgentRunService()
        mock_run = MagicMock(status="crashed", run_id="r1",
                             agent_name="test", task="hello")
        mock_step = MagicMock()
        mock_step.messages_snapshot = "not-valid-json{{{"
        with (
            patch.object(svc, "list_runs", return_value=[mock_run]),
            patch.object(svc, "get_last_step", return_value=mock_step),
        ):
            result = svc.resume_run("r1")
        assert result is None

    def test_resume_from_last_step(self):
        from app.services.harness.run_service import AgentRunService
        svc = AgentRunService()
        mock_run = MagicMock(status="crashed", run_id="r1",
                             agent_name="test", task="hello")
        msgs = [{"role": "user", "content": "hello"},
                {"role": "assistant", "content": "thinking..."}]
        mock_step = MagicMock()
        mock_step.messages_snapshot = json.dumps(msgs)
        mock_step.step_index = 2
        mock_step.step_type = "tool_call"

        expected_result = {"status": "ok", "answer": "resumed answer"}
        async_mock = AsyncMock(return_value=expected_result)

        with (
            patch.object(svc, "list_runs", return_value=[mock_run]),
            patch.object(svc, "get_last_step", return_value=mock_step),
            patch.object(svc, "_run_inline", async_mock),
        ):
            result = svc.resume_run("r1")
        assert result == expected_result
        # Verify _run_inline was called with the correct task message
        async_mock.assert_called_once()
        call_kwargs = async_mock.call_args.kwargs
        assert call_kwargs["agent_name"] == "test"
        assert call_kwargs["run_id"] == "r1"
        assert call_kwargs["messages"] == msgs

    def test_resume_non_crashed_run_returns_none(self):
        from app.services.harness.run_service import AgentRunService
        svc = AgentRunService()
        mock_run = MagicMock(status="completed", run_id="r1",
                             agent_name="test", task="hello")
        with (
            patch.object(svc, "list_runs", return_value=[mock_run]),
        ):
            result = svc.resume_run("r1")
        assert result is None
