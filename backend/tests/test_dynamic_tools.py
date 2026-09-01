"""Tests for dynamic tool loading (app.services.dynamic_tools).

Covers: mode=all passthrough, core always kept, embedding-based periphery
selection, lexical fallback, fail-open on selection errors, original-order
preservation, and the empty-pick safety (full list returned).
"""

from __future__ import annotations

import numpy as np
import pytest

from app.config import settings
from app.services import dynamic_tools


def _schema(name, description):
    return {"function": {"name": name, "description": description}}


CORE = ["memory", "read_file", "execute_code", "web_search"]
PERIPHERY = [
    ("fusion360_design", "Create and edit parametric CAD models in Fusion 360"),
    ("homeassistant_lights", "Control smart home lights and switches"),
    ("kanban_board", "Manage kanban board tasks and columns"),
    ("feishu_doc", "Read and write Feishu documents"),
    ("discord_send", "Send messages to Discord channels"),
    ("forecast_sales", "Generate a weighted sales forecast"),
]


@pytest.fixture(autouse=True)
def _tool_settings(monkeypatch):
    monkeypatch.setattr(settings, "TOOL_LOADING_MODE", "dynamic")
    monkeypatch.setattr(settings, "TOOL_LOADING_CORE", list(CORE))
    monkeypatch.setattr(settings, "TOOL_LOADING_PERIPHERY_TOP_K", 2)


def _all_schemas():
    return [_schema(n, d) for n, d in [("memory", "remember"), ("read_file", "read"), ("execute_code", "run code"), ("web_search", "search")] + PERIPHERY]


class TestModeAll:
    def test_returns_unchanged(self, monkeypatch):
        monkeypatch.setattr(settings, "TOOL_LOADING_MODE", "all")
        schemas = _all_schemas()
        out = dynamic_tools.select_tools_for_turn(schemas, "hello")
        assert out == schemas


class TestCoreAlwaysKept:
    def test_core_present_regardless_of_intent(self, monkeypatch):
        # Embeddings select CAD tool; core must still all be present.
        monkeypatch.setattr(
            dynamic_tools, "_embed_one_or_none", lambda _t: np.array([1.0, 0.0, 0.0])
        )

        def _fake_embed(texts):
            out = []
            for t in texts:
                if "fusion" in t.lower():
                    out.append([1.0, 0.0, 0.0])
                else:
                    out.append([0.0, 1.0, 0.0])
            return np.array(out, dtype=np.float32)

        monkeypatch.setattr(dynamic_tools, "_embed_or_none", _fake_embed)
        out = dynamic_tools.select_tools_for_turn(
            _all_schemas(), "design a bracket in fusion 360"
        )
        names = {s["function"]["name"] for s in out}
        assert CORE[0] in names and CORE[1] in names and CORE[2] in names
        assert "fusion360_design" in names


class TestEmbeddingSelection:
    def test_picks_intent_relevant_periphery(self, monkeypatch):
        monkeypatch.setattr(
            dynamic_tools, "_embed_one_or_none", lambda _t: np.array([1.0, 0.0, 0.0])
        )

        def _fake_embed(texts):
            return np.array(
                [
                    [1.0, 0.0, 0.0] if "fusion" in t.lower() else [0.0, 1.0, 0.0]
                    for t in texts
                ],
                dtype=np.float32,
            )

        monkeypatch.setattr(dynamic_tools, "_embed_or_none", _fake_embed)
        out = dynamic_tools.select_tools_for_turn(
            _all_schemas(), "design a bracket in fusion 360"
        )
        names = [s["function"]["name"] for s in out]
        assert "fusion360_design" in names
        assert "homeassistant_lights" not in names

    def test_preserves_original_order(self, monkeypatch):
        # Select ALL periphery (top_k == periphery count) with tied scores:
        # the returned list must equal the input exactly (core + periphery
        # both in their original relative order).
        monkeypatch.setattr(settings, "TOOL_LOADING_PERIPHERY_TOP_K", 6)
        monkeypatch.setattr(
            dynamic_tools, "_embed_one_or_none", lambda _t: np.array([1.0, 0.0])
        )
        monkeypatch.setattr(
            dynamic_tools,
            "_embed_or_none",
            lambda texts: np.array([[1.0, 0.0]] * len(texts), dtype=np.float32),
        )
        schemas = _all_schemas()
        out = dynamic_tools.select_tools_for_turn(schemas, "anything")
        assert out == schemas  # all periphery tied at 1.0 → selection == full list


class TestLexicalFallback:
    def test_selects_by_token_overlap(self, monkeypatch):
        monkeypatch.setattr(dynamic_tools, "_embed_one_or_none", lambda _t: None)
        monkeypatch.setattr(dynamic_tools, "_embed_or_none", lambda texts: None)
        out = dynamic_tools.select_tools_for_turn(
            _all_schemas(), "turn on the kitchen lights"
        )
        names = {s["function"]["name"] for s in out}
        assert "homeassistant_lights" in names
        assert "fusion360_design" not in names


class TestFailOpen:
    def test_selector_exception_returns_full_list(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("selector crash")

        monkeypatch.setattr(dynamic_tools, "_select_periphery", _boom)
        schemas = _all_schemas()
        out = dynamic_tools.select_tools_for_turn(schemas, "anything")
        assert out == schemas

    def test_empty_pick_returns_full_list(self, monkeypatch):
        monkeypatch.setattr(
            dynamic_tools, "_embed_one_or_none", lambda _t: np.array([1.0, 0.0])
        )
        # All periphery embeddings orthogonal to the query → zero scores.
        monkeypatch.setattr(
            dynamic_tools,
            "_embed_or_none",
            lambda texts: np.array([[0.0, 1.0]] * len(texts), dtype=np.float32),
        )
        schemas = _all_schemas()
        out = dynamic_tools.select_tools_for_turn(schemas, "zzz")
        assert out == schemas

    def test_no_schemas_returns_empty(self):
        assert dynamic_tools.select_tools_for_turn([], "x") == []
