"""Fallback automation conversations must carry the task agent's identity.

Root cause (2026-08-21): ``ensure_task_chat_session`` creates fallback
conversations with ``agent_name=None``. The v3 pre-FSM data-source block
(``backend/app/routers/agents.py``) gates the entire
``prepare_data_source_runtime`` call on ``if conv.agent_name:`` — a falsy
value silently skips the block, so ``_v3_data_ctx_extras = {}`` and the
agent runs with ``bound_kb_ids=[]`` → the LLM reports "This agent has no
data sources bound" even though the task pins a ``data_source_id``.

Fix: heal ``conv.agent_name`` in the reused-conversation branch of
``_run_agent_in_conversation`` (ONLY when falsy — origin sessions keep
their identity), and thread the pinned source + observability logs into
the v3 pre-FSM block.
"""
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


def test_heal_sets_agent_name_when_falsy():
    """A fallback conv (agent_name=None) is healed to the task's agent."""
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    conv = SimpleNamespace(agent_name=None)
    healed = ax._heal_conv_agent_name(conv, SimpleNamespace(name="automation_agent"))
    assert healed is True
    assert conv.agent_name == "automation_agent"


def test_heal_never_overwrites_existing_identity():
    """Origin sessions' real agent identity is never overwritten."""
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    conv = SimpleNamespace(agent_name="automation_agent")
    healed = ax._heal_conv_agent_name(conv, SimpleNamespace(name="automation_agent"))
    assert healed is False
    assert conv.agent_name == "automation_agent"


def test_heal_tolerates_none_conv():
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    assert ax._heal_conv_agent_name(None, SimpleNamespace(name="x")) is False


def test_reused_conv_branch_calls_heal_before_stream():
    """Source-level: the reused/adopted conversation branch must heal the
    agent identity BEFORE the LLM stream runs (silent skip → the agent
    reports no bound data sources)."""
    import inspect
    from app.services import automation_executor as ax

    src = inspect.getsource(ax._run_agent_in_conversation)
    reused_branch = src[src.index("else:"):src.index("def _consume")]
    assert "_heal_conv_agent_name" in reused_branch, (
        "reused-conv branch must call the heal helper before the stream"
    )


def test_pinned_inspection_preflight_error_unbound(monkeypatch):
    """A pinned agent_inspection task whose source can't be resolved must
    produce a clear, retryable error (never a silent empty-bound run)."""
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    monkeypatch.setattr(
        ax, "_resolve_bound_data_source_ids", lambda *a, **kw: []
    )
    task = SimpleNamespace(id="t1", type="agent_inspection", data_source_id="kb-missing")
    err = ax._pinned_inspection_preflight_error(None, task, object(), "proj-1")
    assert err is not None
    assert "kb-missing" in err
    assert "bind" in err.lower()


def test_pinned_inspection_preflight_ok_when_bound(monkeypatch):
    """A resolved pinned source is not an error."""
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    monkeypatch.setattr(
        ax, "_resolve_bound_data_source_ids", lambda *a, **kw: ["kb-x"]
    )
    task = SimpleNamespace(id="t1", type="agent_inspection", data_source_id="kb-x")
    assert ax._pinned_inspection_preflight_error(None, task, object(), "proj-1") is None


def test_pinned_inspection_preflight_skips_unpinned_and_other_types(monkeypatch):
    """No pin, or a non-inspection type → helper returns None without
    touching the resolver (data_sync keeps its own preflight)."""
    from types import SimpleNamespace
    from app.services import automation_executor as ax

    called = {"n": 0}

    def _resolve(*a, **kw):
        called["n"] += 1
        return []

    monkeypatch.setattr(ax, "_resolve_bound_data_source_ids", _resolve)
    t1 = SimpleNamespace(id="t1", type="agent_inspection", data_source_id=None)
    assert ax._pinned_inspection_preflight_error(None, t1, object(), "p") is None
    t2 = SimpleNamespace(id="t2", type="data_sync", data_source_id="kb-x")
    assert ax._pinned_inspection_preflight_error(None, t2, object(), "p") is None
    assert called["n"] == 0, "resolver must not run for skipped tasks"


def test_gate_covers_pinned_agent_inspection_before_invoke():
    """Source-level: execute_automation must run the pinned agent_inspection
    preflight before the LLM call; the data_sync preflight stays intact."""
    import inspect
    from app.services import automation_executor as ax

    src = inspect.getsource(ax.execute_automation)
    gate = src.index("_pinned_inspection_preflight_error")
    invoke = src.index("pool.submit(_run_agent_in_conversation")
    assert gate < invoke, "pinned-inspection preflight must run before the LLM call"
    assert src.index("_resolve_bound_data_source_ids") < invoke


def test_v3_prefsm_block_threads_pinned_source_and_logs():
    """Source-level: the v3 pre-FSM data-source block must pass the conv's
    pinned data_source_id into prepare_data_source_runtime and add
    INFO/WARNING observability (Path C kill + Path B visibility)."""
    import inspect
    from app.routers import agents as ar

    src = inspect.getsource(ar.add_message_stream)
    region = src[
        src.index("_v3_data_ctx_extras"):
        src.index("except Exception as _v3_dsr_err")
    ]
    assert "pinned_data_source_id=" in region, (
        "prepare_data_source_runtime must receive the conv's pinned source"
    )
    assert "conv.metadata_" in region, (
        "the pin must be read from conv.metadata_"
    )
    assert "v3 pre-FSM data ctx" in region, (
        "INFO observability line must be present"
    )
    assert "logger.warning" in region, (
        "WARNING must fire when a pinned source resolves to empty"
    )