"""Source/AST checks for SSE tail hardening (Fix 4).

Asserts that:
1. Artifact/dashboard build tools are registered in ``_LONG_RUNNING_TOOLS`` so
   the batch tool wrapper emits ``tool_progress`` heartbeats while they run.
2. The v3 stream's final conversation persistence offloads ``db.commit()``
   via ``asyncio.to_thread`` (event loop stays free for heartbeats).
3. The final-commit failure path logs + rolls back but NEVER re-raises, so
   the stream always reaches the ``done`` event.
"""
import re
from pathlib import Path

_AGENTS = Path(__file__).resolve().parents[1] / "app/routers/agents.py"
_SRC = _AGENTS.read_text(encoding="utf-8")

# Tools that generate large artifacts / dashboards / run sandboxed skills and
# can take tens of seconds to a few minutes.
_REQUIRED_LONG_RUNNING_TOOLS = {
    "create_artifact",
    "create_fullstack_dashboard",
    "update_fullstack_dashboard",
    "create_dashboard",
    "revert_fullstack_dashboard",
    "run_sandbox_skill",
}


def _long_running_tools_body() -> str:
    m = re.search(r"_LONG_RUNNING_TOOLS\s*=\s*frozenset\(\{(.*?)\}\)", _SRC, re.S)
    assert m, "_LONG_RUNNING_TOOLS frozenset literal not found"
    return m.group(1)


def test_long_running_tools_include_artifact_and_dashboard():
    body = _long_running_tools_body()
    for tool in sorted(_REQUIRED_LONG_RUNNING_TOOLS):
        assert tool in body, (
            f"'{tool}' missing from _LONG_RUNNING_TOOLS — it will run without "
            "tool_progress heartbeats and long builds can idle-kill the SSE "
            "connection"
        )


def _v3_final_commit_tail() -> str:
    marker = '"v3 stream final commit failed (non-fatal): %s"'
    idx = _SRC.index(marker)
    # Grab ~1800 chars before the marker (the commit site) plus enough
    # after to cover the post-commit refresh block.
    start = max(0, idx - 1800)
    return _SRC[start : idx + 800]


def test_final_commit_offloaded_via_to_thread():
    tail = _v3_final_commit_tail()
    assert "conv.messages = list(messages)" in tail
    assert "asyncio.to_thread" in tail, (
        "final db.commit() is not offloaded via asyncio.to_thread — a slow "
        "commit blocks the event loop and starves SSE heartbeats"
    )


def test_final_commit_failure_never_raises():
    tail = _v3_final_commit_tail()
    # The failure branch must roll back but must not re-raise (a raise here
    # aborts the SSE generator before the `done` event is emitted).
    assert "db.rollback()" in tail
    # Any `raise` between the try block and the post-commit refresh is fatal.
    try_idx = tail.index("db.commit")
    post_commit = tail[try_idx:]
    assert not re.search(r"^\s*raise\b", post_commit, re.M), (
        "final-commit failure path re-raises — the stream can die before "
        "emitting `done`"
    )


def test_post_commit_refresh_exception_safe():
    tail = _v3_final_commit_tail()
    assert "final refresh failed" in tail, (
        "post-commit re-query of the conversation must be wrapped so a "
        "refresh failure cannot abort the stream"
    )
