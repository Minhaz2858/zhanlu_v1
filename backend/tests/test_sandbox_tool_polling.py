"""Regression tests for run_sandbox_skill polling behavior."""

from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "app" / "services" / "tool_handlers" / "sandbox_tool.py"


def test_sandbox_poll_limit_uses_job_timeout_not_only_legacy_cap():
    source = SRC.read_text()
    assert "poll_limit = max(_MAX_POLL_SECONDS, int(job_timeout or 0) + 10)" in source
    assert "while elapsed < poll_limit" in source


def test_running_or_queued_after_poll_window_becomes_timeout():
    source = SRC.read_text()
    assert 'final_job.status in ("queued", "running")' in source
    assert 'update_job_status(job.id, "timeout"' in source
    assert "Sandbox job is still" in source
    assert '"job_status": final_status' in source