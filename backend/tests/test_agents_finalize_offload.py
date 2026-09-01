"""Fix: prevent SSE heartbeat starvation during finalize_into_artifact.

``finalize_into_artifact`` calls ``run_sandbox_skill_sync`` which can block
the event loop for 30-120s while a Docker container renders PPTX/DOCX.
The v3 loop was calling it directly inside the SSE generator, so the 5s
heartbeat ping never fired and the browser showed "connection interrupted".

The fix offloads ``finalize_into_artifact`` to a thread with its own
``SessionLocal`` and emits ``tool_progress`` heartbeats while waiting.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from app.routers import agents


def test_start_finalize_offloaded_runs_in_thread_with_own_session():
    """The helper commits the caller's DB and runs finalize in a new session."""
    fake_db = MagicMock()
    fake_thread_db = MagicMock()
    fake_artifact = MagicMock()
    fake_exports = {"pptx": {"artifact_id": "a1"}}

    def fake_finalize(db, **kwargs):
        # The thread must receive a fresh SessionLocal, not the caller's db.
        assert db is fake_thread_db
        assert kwargs["conversation_id"] == "conv-1"
        return (fake_artifact, fake_exports)

    with patch("app.routers.agents.finalize_into_artifact", side_effect=fake_finalize):
        with patch("app.database.SessionLocal", return_value=fake_thread_db):

            async def _run():
                task = agents._start_finalize_offloaded(
                    fake_db, {"conversation_id": "conv-1"}
                )
                return await task

            result = asyncio.run(_run())

    assert result == (fake_artifact, fake_exports)
    fake_db.commit.assert_called_once()
    fake_thread_db.commit.assert_called_once()
    fake_thread_db.close.assert_called_once()


def test_threaded_finalize_disables_expire_on_commit_for_detached_reads():
    """FIX B (2026-08-22): the Artifact row must stay readable after the
    thread session closes.

    Default ``expire_on_commit=True`` expires every attribute at commit, so
    reading e.g. ``artifact.id`` on the returned (now detached) row raises
    DetachedInstanceError in the caller.  The thread must disable expiry
    before committing.
    """

    class SpySession:
        expire_on_commit = True

        def __init__(self):
            self.committed = False

        def commit(self):
            assert (
                self.expire_on_commit is False
            ), "FIX B: expire_on_commit must be disabled before commit so the detached row stays readable"
            self.committed = True

        def rollback(self):
            pass

        def close(self):
            pass

    fake_db = MagicMock()
    spy = SpySession()
    fake_artifact = MagicMock()
    fake_exports = {"pptx": {"artifact_id": "a1"}}

    with patch("app.routers.agents.finalize_into_artifact", return_value=(fake_artifact, fake_exports)):
        with patch("app.database.SessionLocal", return_value=spy):

            async def _run():
                task = agents._start_finalize_offloaded(fake_db, {})
                return await task

            result = asyncio.run(_run())

    assert result == (fake_artifact, fake_exports)
    assert spy.expire_on_commit is False
    assert spy.committed


def test_start_finalize_offloaded_rolls_back_on_error():
    """If finalize raises, the thread's session is rolled back and closed."""
    fake_db = MagicMock()
    fake_thread_db = MagicMock()

    with patch("app.routers.agents.finalize_into_artifact", side_effect=RuntimeError("boom")):
        with patch("app.database.SessionLocal", return_value=fake_thread_db):

            async def _run():
                task = agents._start_finalize_offloaded(fake_db, {})
                return await task

            with pytest.raises(RuntimeError, match="boom"):
                asyncio.run(_run())

    fake_db.commit.assert_called_once()
    fake_thread_db.rollback.assert_called_once()
    fake_thread_db.close.assert_called_once()


@pytest.mark.asyncio
async def test_emit_tool_progress_while_waiting_yields_heartbeats():
    """Heartbeat frames are emitted while a task is still running."""

    async def never_done():
        await asyncio.Event().wait()

    task = asyncio.ensure_future(never_done())
    heartbeats = []
    try:
        async for hb in agents._emit_tool_progress_while_waiting(
            task,
            [
                {
                    "tool_call_id": "finalize-artifact",
                    "tool_name": "create_artifact",
                    "args_str": "",
                    "args": {},
                }
            ],
        ):
            heartbeats.append(hb)
            if len(heartbeats) >= 2:
                break
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert len(heartbeats) >= 1
    data = json.loads(heartbeats[0].split("data: ", 1)[1])
    assert data["type"] == "tool_progress"
    assert data["tool_calls"][0]["name"] == "create_artifact"
    assert data["tool_calls"][0]["status"] == "running"
