"""Tests for the shared ContextBus (IBM context-management convergence).

The bus is the single context model shared by the FSM path and the ReAct
main loop. These tests cover publish/retrieve roundtrips, both retrieval
modes, the FSM-manifest shape, ReAct prompt rendering, and registry
lifecycle. Embedding-dependent paths are exercised in both real-failure
(monkeypatched embedder raising) and lexical-fallback modes so the suite
never requires the MiniLM model.
"""
import pytest

from app.services.context_bus import (
    ContextBus,
    drop_context_bus,
    get_context_bus,
)


# ---------------------------------------------------------------- basics

def test_publish_get_roundtrip():
    bus = ContextBus("c1")
    bus.publish("system", "agent: analyst; tools: sql, files", {"tools": ["sql"]})
    section = bus.get("system")
    assert section is not None
    assert section.key == "system"
    assert "analyst" in section.text
    assert section.meta["tools"] == ["sql"]


def test_publish_truncates_long_text():
    bus = ContextBus("c1")
    bus.publish("memory", "x" * 10000)
    assert len(bus.get("memory").text) <= 2000


def test_get_missing_returns_none():
    assert ContextBus("c1").get("nope") is None


def test_publish_ignores_empty_key():
    bus = ContextBus("c1")
    bus.publish("", "text")
    assert bus.keys() == []


# ---------------------------------------------------------------- retrieval

def _seed(bus):
    bus.publish("system", "agent config: data analyst with sql tools")
    bus.publish("task_state", "plan: verify row counts for lotmaster table")
    bus.publish("memory", "user prefers concise tables over prose")


def test_retrieve_lexical_ranks_relevant_first():
    bus = ContextBus("c1")
    _seed(bus)
    results = bus.retrieve("lotmaster row counts", k=3)
    assert results, "expected at least one section"
    assert results[0].key == "task_state"


def test_retrieve_semantic_failure_falls_back_lexical(monkeypatch):
    bus = ContextBus("c1")
    _seed(bus)

    def boom(*a, **k):
        raise RuntimeError("embedder down")

    monkeypatch.setattr(
        "app.services.dynamic_tools._embed_one_or_none", boom
    )
    results = bus.retrieve("lotmaster row counts", k=3)
    assert results
    assert results[0].key == "task_state"


def test_retrieve_empty_query_returns_empty():
    bus = ContextBus("c1")
    _seed(bus)
    assert bus.retrieve("", k=3) == []
    assert ContextBus("c2").retrieve("anything") == []


def test_retrieve_respects_char_budget():
    bus = ContextBus("c1")
    for i in range(10):
        bus.publish(f"tool_digest:t{i}", "row data " * 200)
    results = bus.retrieve("row data", k=20)
    total = sum(len(r.text) for r in results)
    assert total <= 3500 + 2000  # budget + at most one oversize section


# ---------------------------------------------------------------- digests

def test_digest_tool_result_and_aggregate():
    bus = ContextBus("c1")
    bus.digest_tool_result("execute_query", {"rows": [[1], [2]], "summary": "2 rows"})
    bus.digest_tool_result("describe_schema", {"status": "ok"})
    assert bus.get("tool_digest:execute_query") is not None
    agg = bus.get("tool_digest")
    assert "execute_query" in agg.meta["tools"]
    assert "describe_schema" in agg.meta["tools"]


def test_digest_none_result_is_placeholder():
    bus = ContextBus("c1")
    bus.digest_tool_result("execute_query", None)
    assert "(no result)" in bus.get("tool_digest:execute_query").text


# ---------------------------------------------------------------- consumers

def test_to_fsm_manifest_shape():
    bus = ContextBus("c1")
    bus.publish("system", "analyst", {"tools": ["sql", "files"], "skills": ["etl"]})
    bus.publish("task_state", "plan ready")
    manifest = bus.to_fsm_manifest()
    # keys mirror synexia.context_assembler.ContextManifest contract
    assert set(manifest) >= {"items", "token_estimate", "available_tools",
                             "available_skills", "user_attachments"}
    assert manifest["available_tools"] == ["sql", "files"]
    assert manifest["available_skills"] == ["etl"]
    assert any(i["type"] == "task_state" for i in manifest["items"])
    assert manifest["token_estimate"] > 0


def test_format_for_react_includes_sections_and_skips():
    bus = ContextBus("c1")
    bus.publish("task_state", "plan: count lots")
    bus.publish("transcript", "earlier conversation text")
    bus.digest_tool_result("execute_query", {"rows": [[1]]})
    rendered = bus.format_for_react()
    assert "[TASK STATE]" in rendered
    assert "count lots" in rendered
    assert "[TOOL RESULT: execute_query]" in rendered
    assert "earlier conversation" in rendered

    skipped = bus.format_for_react(skip={"transcript"})
    assert "earlier conversation" not in skipped
    assert "[TASK STATE]" in skipped


# ---------------------------------------------------------------- registry

def test_get_context_bus_is_singleton_per_conversation():
    b1 = get_context_bus("conv-x")
    b2 = get_context_bus("conv-x")
    assert b1 is b2
    b3 = get_context_bus("conv-y")
    assert b3 is not b1
    drop_context_bus("conv-x")
    b4 = get_context_bus("conv-x")
    assert b4 is not b1
    drop_context_bus("conv-y")
