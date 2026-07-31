"""Tests for the OHMO memory consolidation hook (P3 Task 4)."""

from __future__ import annotations

from pathlib import Path

from app.services import memory_consolidation as mc
from app.config import settings


# --- extract_facts_from_turn -----------------------------------------------


def test_extract_returns_empty_when_llm_unavailable(monkeypatch):
    monkeypatch.setattr(mc, "_llm_callable", lambda _p: None)
    out = mc.extract_facts_from_turn("hi", "hello there")
    assert out == []


def test_extract_returns_empty_on_malformed_json(monkeypatch):
    monkeypatch.setattr(mc, "_llm_callable", lambda _p: {"facts": "not-a-list"})
    out = mc.extract_facts_from_turn("hi", "hello")
    assert out == []


def test_extract_returns_facts_from_well_formed_response(monkeypatch):
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["lives in Berlin", "prefers terse answers"]},
    )
    out = mc.extract_facts_from_turn("hi", "hello")
    assert out == ["lives in Berlin", "prefers terse answers"]


def test_extract_caps_facts_at_five(monkeypatch):
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["a", "b", "c", "d", "e", "f", "g"]},
    )
    out = mc.extract_facts_from_turn("hi", "hi")
    assert len(out) == 5


def test_extract_truncates_oversized_facts(monkeypatch):
    long = "x" * 500
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": [long, "short"]},
    )
    out = mc.extract_facts_from_turn("hi", "hi")
    assert len(out) == 2
    assert len(out[0]) == 200
    assert out[1] == "short"


def test_extract_filters_empty_facts(monkeypatch):
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["good", "", "  ", "also good"]},
    )
    out = mc.extract_facts_from_turn("hi", "hi")
    assert out == ["good", "also good"]


# --- consolidate_turn_memory (flag-gated) -----------------------------------


def test_consolidate_is_noop_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", False)
    called = []
    monkeypatch.setattr(mc, "_llm_callable", lambda _p: (called.append(1) or {"facts": ["x"]}))
    mc.consolidate_turn_memory("hi", "hi", workspace_dir=str(tmp_path))
    assert called == []


def test_consolidate_merges_facts_into_workspace_when_flag_on(monkeypatch, tmp_path):
    from app.services.ohmo import OhmoWorkspace
    ws = OhmoWorkspace(workspace_dir=str(tmp_path))
    ws.init_workspace()

    monkeypatch.setattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", True)
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["lives in Berlin", "prefers terse answers"]},
    )

    result = mc.consolidate_turn_memory("I live in Berlin", "Noted.", workspace_dir=str(tmp_path))
    assert result["extracted"] == 2
    assert result["merged"] == 2

    user_md = (tmp_path / "user.md").read_text(encoding="utf-8")
    assert "lives in Berlin" in user_md
    assert "prefers terse answers" in user_md


def test_consolidate_dedups_via_append_user_fact(monkeypatch, tmp_path):
    from app.services.ohmo import OhmoWorkspace
    ws = OhmoWorkspace(workspace_dir=str(tmp_path))
    ws.init_workspace()
    ws.append_user_fact("lives in Berlin")

    monkeypatch.setattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", True)
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["lives in Berlin", "new fact"]},
    )

    mc.consolidate_turn_memory("hi", "hi", workspace_dir=str(tmp_path))

    user_md = (tmp_path / "user.md").read_text(encoding="utf-8")
    assert user_md.count("lives in Berlin") == 1
    assert "new fact" in user_md


def test_consolidate_returns_zeros_when_no_facts(monkeypatch, tmp_path):
    from app.services.ohmo import OhmoWorkspace
    ws = OhmoWorkspace(workspace_dir=str(tmp_path))
    ws.init_workspace()

    monkeypatch.setattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", True)
    monkeypatch.setattr(mc, "_llm_callable", lambda _p: {"facts": []})

    result = mc.consolidate_turn_memory("hi", "hi", workspace_dir=str(tmp_path))
    assert result == {"extracted": 0, "merged": 0}


def test_consolidate_never_raises_on_ohmo_error(monkeypatch, tmp_path):
    """An OhmoWorkspace I/O error must be swallowed (best-effort)."""
    monkeypatch.setattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", True)
    monkeypatch.setattr(
        mc, "_llm_callable",
        lambda _p: {"facts": ["a fact"]},
    )
    # Force a failure in the merge step by making append_user_fact raise.
    from app.services.ohmo import OhmoWorkspace
    def _boom(self, fact):
        raise RuntimeError("disk full")
    monkeypatch.setattr(OhmoWorkspace, "append_user_fact", _boom)

    result = mc.consolidate_turn_memory("hi", "hi", workspace_dir=str(tmp_path))
    assert result["merged"] == 0
    assert result["extracted"] == 1  # the LLM did return a fact; merge just failed


# --- v3 hook wiring (textual) ----------------------------------------------


def test_v3_event_stream_wires_memory_consolidation_hook():
    src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
    assert "consolidate_turn_memory" in src


def test_v3_hook_is_flag_gated():
    src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
    assert "OHMO_MEMORY_CONSOLIDATION_ENABLED" in src


def test_v3_hook_runs_as_create_task():
    """The hook must be fire-and-forget (create_task), not inline."""
    src = Path("/root/zhanlu/backend/app/routers/agents.py").read_text()
    # The call site uses create_task(asyncio.to_thread(consolidate_turn_memory, ...))
    # — find it by searching for the create_task call that mentions the flag.
    flag_idx = src.find("OHMO_MEMORY_CONSOLIDATION_ENABLED")
    assert flag_idx != -1
    # Find create_task within 1000 chars of the flag check (they're in the same block).
    nearby = src[flag_idx:flag_idx + 1500]
    assert "create_task" in nearby, "create_task not near the flag-gated hook"
    assert "to_thread" in nearby, "to_thread not near the flag-gated hook"
    assert "consolidate_turn_memory" in nearby, "consolidate_turn_memory call not near the flag check"


# --- prompt shape ----------------------------------------------------------


def test_extract_prompt_mentions_json_and_user_facts(monkeypatch):
    captured = []
    def _cap(prompt):
        captured.append(prompt)
        return {"facts": []}
    monkeypatch.setattr(mc, "_llm_callable", _cap)
    mc.extract_facts_from_turn("user said hi", "assistant said hi back")
    assert len(captured) == 1
    prompt = captured[0]
    assert "json" in prompt.lower() or "JSON" in prompt
    assert "user" in prompt.lower()
    assert "fact" in prompt.lower()
