"""Regression (2026-08-05): the agent's "MEMORY" snapshot (target='memory'
rows in agent_memories) was recalled across every project the user had
ever visited, so a note like "Q2 2026 sales report" taken in one
project leaked into the system prompt of convs in every other project.

Fix: AgentMemory now has a project_id column. load_memory_snapshot and
_get_or_create_memory both filter by it on the target='memory' path;
target='user' (user profile) stays cross-project.
"""
import os
import sys
from unittest.mock import MagicMock, patch

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)

from app.services.tool_handlers.memory_tool import (
    _get_or_create_memory,
    load_memory_snapshot,
)


def _make_query_chain():
    """Build a SQLAlchemy-like query chain: .filter(...) returns self,
    .first() returns None (no existing row)."""
    q = MagicMock()
    q.first.return_value = None
    q.filter.return_value = q
    return q


def _filter_strs(chain):
    """Flatten every .filter(...) call's args into strings so we can
    grep for column names. SQLAlchemy ``BinaryExpression`` /
    ``BooleanClauseList`` stringify to the underlying SQL fragment
    (e.g. ``"agent_memories.project_id = :project_id_1"``) which is
    what we want to assert on."""
    out = []
    for call in chain.filter.call_args_list:
        for arg in call.args:
            out.append(str(arg))
    return out


def test_load_memory_snapshot_scopes_target_memory_to_active_project():
    """load_memory_snapshot(target='memory', project_id='proj-A') must
    apply a project_id filter on the memory chain so notes tagged
    with project-B do not leak in."""
    chains = {"memory": _make_query_chain(), "user": _make_query_chain()}
    n = [0]
    def _q_for(model):
        target = "memory" if n[0] == 0 else "user"
        n[0] += 1
        return chains[target]
    db = MagicMock()
    db.query.side_effect = _q_for

    load_memory_snapshot(db, agent_app_id="test", user_id="u1", project_id="proj-A")

    # The target='memory' chain must have a project_id filter.
    mem_filter_strs = _filter_strs(chains["memory"])
    assert any("project_id" in s for s in mem_filter_strs), (
        f"expected project_id filter on target='memory' chain; got: {mem_filter_strs}"
    )


def test_load_memory_snapshot_user_target_is_cross_project():
    """target='user' (the user profile) is always cross-project — the
    project_id filter must NOT be applied on the user chain."""
    chains = {"memory": _make_query_chain(), "user": _make_query_chain()}
    n = [0]
    def _q_for(model):
        target = "memory" if n[0] == 0 else "user"
        n[0] += 1
        return chains[target]
    db = MagicMock()
    db.query.side_effect = _q_for

    load_memory_snapshot(db, agent_app_id="test", user_id="u1", project_id="proj-A")

    user_filter_strs = _filter_strs(chains["user"])
    assert not any("project_id" in s for s in user_filter_strs), (
        f"target='user' should be cross-project; got: {user_filter_strs}"
    )


def test_load_memory_snapshot_no_active_project_skips_memory_target():
    """When project_id is None (the 'Ungrouped' chat), the target='memory'
    chain must be SKIPPED entirely so legacy pre-project-scoping notes
    (which all have project_id=NULL) do not leak into every ungrouped chat."""
    chains = {"memory": _make_query_chain(), "user": _make_query_chain()}
    def _q_for(model):
        # In the new code only one query is issued (for user) because memory is skipped.
        return chains["user"]
    db = MagicMock()
    db.query.side_effect = _q_for

    result = load_memory_snapshot(db, agent_app_id="test", user_id="u1", project_id=None)

    # target='memory' must be skipped — result['memory'] empty.
    assert result["memory"] == "", (
        f"expected empty memory block in ungrouped chat; got: {result['memory']!r}"
    )
    # target='user' should still be queried (cross-project).
    assert chains["user"].filter.called, (
        "target='user' query should still be executed in ungrouped chat"
    )


def test_load_memory_snapshot_strict_match_no_null_fallback():
    """When project_id is set, the memory chain must match ONLY the
    active project — no NULL fallback. This is the fix for the Q2
    2026 sales report leak: a legacy NULL-bucket note must NOT show
    up in any specific project (it stays visible only in Ungrouped
    until the user re-saves it in the right project)."""
    chains = {"memory": _make_query_chain(), "user": _make_query_chain()}
    n = [0]
    def _q_for(model):
        target = "memory" if n[0] == 0 else "user"
        n[0] += 1
        return chains[target]
    db = MagicMock()
    db.query.side_effect = _q_for

    load_memory_snapshot(db, agent_app_id="test", user_id="u1", project_id="proj-A")

    mem_filter_strs = _filter_strs(chains["memory"])
    # Must reference project_id…
    assert any("project_id" in s for s in mem_filter_strs), (
        f"expected project_id filter on target='memory' chain; got: {mem_filter_strs}"
    )
    # …but must NOT include the IS NULL clause when an active
    # project is set — that was the leak.
    assert not any("IS NULL" in s for s in mem_filter_strs), (
        f"expected strict match (no IS NULL fallback) when an active "
        f"project is set; got: {mem_filter_strs}"
    )


def test_get_or_create_memory_stamps_project_id_on_new_rows():
    """When the memory row doesn't exist yet, the create branch must
    stamp project_id so the note is project-scoped going forward."""
    with patch("app.services.tool_handlers.memory_tool.AgentMemory") as FakeAM:
        db = MagicMock()
        db.query.return_value = _make_query_chain()  # no existing row

        _get_or_create_memory(
            db,
            agent_app_id="test",
            user_id="u1",
            target="memory",
            project_id="proj-A",
        )

        assert FakeAM.call_count == 1
        kwargs = FakeAM.call_args.kwargs
        assert kwargs.get("project_id") == "proj-A", (
            f"expected project_id='proj-A' on new memory row; got kwargs={kwargs}"
        )


def test_get_or_create_memory_user_target_stays_cross_project():
    """target='user' rows never get a project_id stamped on creation
    (the user profile is always cross-project)."""
    with patch("app.services.tool_handlers.memory_tool.AgentMemory") as FakeAM:
        db = MagicMock()
        db.query.return_value = _make_query_chain()

        _get_or_create_memory(
            db,
            agent_app_id="test",
            user_id="u1",
            target="user",
            project_id="proj-A",  # caller is in a project
        )

        assert FakeAM.call_count == 1
        kwargs = FakeAM.call_args.kwargs
        # User profile row must be created with project_id=None
        # (cross-project), regardless of the caller's project.
        assert kwargs.get("project_id") is None, (
            f"target='user' should never stamp project_id; got kwargs={kwargs}"
        )


# ─────────────────────────────────────────────────────────────────
# save_memory / auto-extract project scoping (2026-08-27)
#
# The automatic post-chat memory extraction path used to drop the
# conversation's project_id, so auto-extracted memories landed in the
# legacy NULL bucket and NEVER appeared in any project's Shared
# Memory panel. Fix: thread project_id through
#   _bg_extract_memories → auto_extract_memories → save_memory.
# ─────────────────────────────────────────────────────────────────

def test_save_memory_stamps_project_id_on_new_row():
    """save_memory(project_id='proj-A') must create the AgentMemory row
    with project_id='proj-A' so the extracted fact surfaces in that
    project's Shared Memory panel."""
    with patch("app.services.memory_advanced.AgentMemory") as FakeAM:
        db = MagicMock()
        # No existing row (dedup miss) → create branch.
        db.query.return_value.first.return_value = None
        # Make filter() chainable (returns self) — used on the dedup query.
        db.query.return_value.filter.return_value = db.query.return_value

        from app.services.memory_advanced import save_memory
        result = save_memory(
            db, agent_app_id="test", content="fact", project_id="proj-A",
        )

        assert result.get("duplicate") is False
        kwargs = FakeAM.call_args.kwargs
        assert kwargs.get("project_id") == "proj-A", (
            f"expected project_id='proj-A' stamped on new memory; got kwargs={kwargs}"
        )


def test_save_memory_user_target_never_stamps_project_id():
    """save_memory(target='user', project_id='proj-A') must force
    project_id to None — the user profile is always cross-project."""
    with patch("app.services.memory_advanced.AgentMemory") as FakeAM:
        db = MagicMock()
        db.query.return_value.first.return_value = None
        db.query.return_value.filter.return_value = db.query.return_value

        from app.services.memory_advanced import save_memory
        save_memory(
            db, agent_app_id="test", content="user pref",
            target="user", project_id="proj-A",
        )

        kwargs = FakeAM.call_args.kwargs
        assert kwargs.get("project_id") is None, (
            f"target='user' must stay cross-project; got kwargs={kwargs}"
        )


def test_auto_extract_forwards_project_id_to_save_memory():
    """auto_extract_memories(project_id='proj-A') must pass project_id
    down to every save_memory call for extracted facts."""
    import app.services.memory_advanced as ma

    # Patch save_memory so the LLM extraction result actually persists
    # without touching the DB, then assert the forwarded project_id.
    # call_llm is imported inside auto_extract_memories from llm_service
    # and awaited, so patch it with an AsyncMock.
    from unittest.mock import AsyncMock as _AsyncMock
    with patch.object(ma, "save_memory", return_value={"success": True, "duplicate": False, "id": "x"}) as fake_save, \
         patch("app.services.llm_service.call_llm", new=_AsyncMock()) as fake_llm:
        # call_llm returns {"response": "[{\"content\": \"fact\", ...}]"}
        fake_llm.return_value = {
            "response": '[{"content": "fact", "target": "memory", "importance": 3}]'
        }
        db = MagicMock()

        import asyncio
        result = asyncio.run(
            ma.auto_extract_memories(
                db, agent_app_id="test",
                messages=[{"role": "user", "content": "a"}] * 5,
                user_id="u1",
                project_id="proj-A",
            )
        )

        assert result, "expected extracted memory to be saved"
        call_kwargs = fake_save.call_args.kwargs
        assert call_kwargs.get("project_id") == "proj-A", (
            f"auto-extract must forward project_id to save_memory; got {call_kwargs}"
        )


def test_all_bg_extract_call_sites_forward_conv_project_id():
    """Every _bg_extract_memories trigger (add_message, resume_conversation,
    add_message_stream — the v3 SSE path the UI actually uses) must forward
    conv.project_id so auto-extracted memories land in the project bucket."""
    import re
    with open("app/routers/agents.py", "r") as f:
        src = f.read()
    calls = re.findall(
        r"asyncio\.create_task\(_bg_extract_memories\([\s\S]*?\)\)",
        src,
    )
    assert len(calls) >= 3, f"expected ≥3 _bg_extract_memories call sites; got {len(calls)}"
    for call in calls:
        assert "project_id=getattr(conv, \"project_id\", None)" in call, (
            f"every call site must forward conv.project_id; got:\n{call}"
        )
